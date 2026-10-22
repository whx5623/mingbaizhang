# ai_agent.py
import base64
import json
import logging
import math
import os
import re
import tomllib
from datetime import datetime

import requests
from supabase import create_client, Client

logger = logging.getLogger(__name__)


def get_secret(key: str):
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val is not None:
            return val
    except Exception:
        pass
    try:
        secret_path = ".streamlit/secrets.toml"
        if os.path.exists(secret_path):
            with open(secret_path, "rb") as f:
                secrets_data = tomllib.load(f)
                val = secrets_data.get(key)
                if val is not None:
                    return val
    except Exception:
        pass
    return os.environ.get(key)


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ZHIPU_EMBED_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"


def get_embedding(text: str, zhipu_api_key: str):
    headers = {
        "Authorization": f"Bearer {zhipu_api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "embedding-2",
        "input": text
    }
    resp = requests.post(ZHIPU_EMBED_URL, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    embedding = resp.json()["data"][0]["embedding"]
    return embedding


def search_knowledge_base(query_embedding, top_k=3):
    res = supabase.rpc(
        "match_knowledge",
        {
            "query_embedding": query_embedding,
            "match_count": top_k
        }
    ).execute()
    return res.data


def build_system_prompt(total_income, total_expense, balance, recent_bills_text, retrieved_knowledge):
    knowledge_text = "\n".join([item["content"] for item in retrieved_knowledge])
    prompt = f"""你是大学生AI金融导师，支持自动记账工具调用。
参考知识：
{knowledge_text}
用户财务背景：
总收入：{total_income:.2f}元
总支出：{total_expense:.2f}元
结余：{balance:.2f}元
最近5笔账单：
{recent_bills_text}
结合参考知识与账单数据，给出适合大学生的财务建议。
如果用户提到一笔消费或收入，请调用add_bill工具自动记录账单。

工具使用规则（必须严格遵守）：
1. 查询类问题（某段时间/某分类花了多少、有哪些账单）→ 必须调用 query_bills，不要凭记忆或不完整的“最近5笔账单”作答；拿到结果后用自然语言总结（总笔数、总金额、Top 商户/分类），不要编造数字。
2. 删除类请求 → 直接调用 delete_bills（只调用工具，不要自己编造删除结果）。调用后系统会向用户展示「确认卡片」，用户回复「确认」才会真正执行删除；因此你**不要**在文字里再问一遍确认，也不要在没有用户删除意图时调用它。
3. 如果用户没说清日期区间，先追问清楚再调用工具，不要自行猜测。
4. 不要在一次回复里同时调用 add_bill 和 delete_bills。
5. 用户明确提到消费时的心情（emo/焦虑/开心/累/激动/压力大…）时，把对应情绪填进 add_bill 的 mood 字段（可选值：开心/Emo/焦虑/疲惫/激动）；没提就不要猜、不要编造。
"""
    return prompt


def get_ai_response(messages, api_key, zhipu_api_key, total_income, total_expense, balance, recent_bills_text):
    last_user_msg = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            last_user_msg = msg["content"]
            break
    emb = get_embedding(last_user_msg, zhipu_api_key)
    kb_results = search_knowledge_base(emb, top_k=3)
    sys_prompt = build_system_prompt(total_income, total_expense, balance, recent_bills_text, kb_results)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "add_bill",
                "description": "新增一笔账单记录",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "日期，格式YYYY-MM-DD"},
                        "merchant": {"type": "string", "description": "商户名称"},
                        "amount": {"type": "number", "description": "金额"},
                        "payment_method": {"type": "string", "description": "支付方式"},
                        "type": {"type": "string", "description": "收入/支出"},
                        "mood": {"type": "string", "description": "消费时的情绪，可选值：开心/Emo/焦虑/疲惫/激动；只有用户明确提到心情时才填，没提就不填"}
                    },
                    "required": ["date", "merchant", "amount", "payment_method", "type"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "query_bills",
                "description": "按日期区间/分类/关键词查询用户的账单，返回明细；用户问“上个月在食堂花了多少”这类问题时必须调用",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start_date": {"type": "string", "description": "开始日期，格式YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "结束日期，格式YYYY-MM-DD（含当天）"},
                        "category": {"type": "string", "description": "分类，可选，如 餐饮/奶茶咖啡/购物。不填表示不限分类"},
                        "keyword": {"type": "string", "description": "商户或分类关键词，可选，如 食堂/咖啡。不填表示不限关键词"}
                    },
                    "required": ["start_date", "end_date"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_bills",
                "description": "危险操作：按日期区间（可选分类）删除用户的账单，删除不可恢复，且区间内的收入与支出会被一起删除。用户明确表达删除意图时直接调用本工具：调用后系统会向用户展示确认卡片，用户回复「确认」后系统才会真正执行；因此不要在文字里自行询问确认，也不要编造删除结果",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start_date": {"type": "string", "description": "开始日期，格式YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "结束日期，格式YYYY-MM-DD（含当天）"},
                        "category": {"type": "string", "description": "分类，可选，如 餐饮/购物；不填表示该区间内全部类别"}
                    },
                    "required": ["start_date", "end_date"]
                }
            }
        }
    ]

    new_messages = [{"role": "system", "content": sys_prompt}] + messages
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": new_messages,
        "tools": tools,
        "temperature": 0.7
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    return resp


# ============================================================================
# 账单图片识别（智谱 GLM-4V 视觉模型，OpenAI 兼容接口）
# ----------------------------------------------------------------------------
# 设计约定：
# · 所有“面向用户”的失败统一走 BillImageError(code, message)：
#     - code    供页面做分支处理（not_bill / blurry / ambiguous / no_api_key /
#               network / http / parse）；
#     - message 是可直接渲染到页面的中文，必须带可操作建议；
# · 绝不把 api key（或含 key 的响应体片段）写进异常 message / 日志；
# · 日期解析失败**绝不**静默回退成“今天”，一律 ambiguous 让用户手填。
# ============================================================================

ZHIPU_VISION_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
# 默认 glm-4v-flash：低价/免费档，日常账单截图足够；
# 识别不准时用 secrets 覆盖成 "glm-4v"（识别更准、单价更高），无需改代码：
#   ZHIPU_VISION_MODEL = "glm-4v"
DEFAULT_VISION_MODEL = "glm-4v-flash"

MAX_BILL_IMAGE_BYTES = 5 * 1024 * 1024          # 5MB：base64 后约 6.7MB，接口上限内
MAX_BILL_AMOUNT = 9_999_999_999.99              # 单笔金额上限，防止小数点/多单误识别
ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp"}
)

VISION_SYSTEM_PROMPT = """你是严格的账单/小票信息抽取器，只做信息抽取，不做解读、不寒暄、不解释。
请从用户提供的图片中抽取账单信息，并**只输出一个 JSON 对象**，不要输出 Markdown 代码块标记，不要输出 JSON 之外的任何文字。

JSON 必须包含以下字段（字段名不可改、不可少）：
{
  "is_bill": true/false,      // 图片是否为账单/小票/支付记录（含金额与消费信息）
  "blurry": true/false,       // 是否因模糊、过暗、反光、遮挡、截断导致关键信息（金额/日期/商户）看不清
  "merchant": "商户名称",      // 识别不到给空串 ""
  "amount": 0.00,             // 金额，必须是正数，不带货币符号、不加千分位、不做汇率换算
  "paid_at": "YYYY-MM-DD",    // 支付/消费日期；识别不到给空串 ""
  "type": "支出" 或 "收入",     // 退款/红包/入账/收益等为"收入"，其余为"支出"
  "confidence": 0.0,          // 0~1 之间的整体置信度
  "reason": "一句话说明"        // 判断依据，或看不清的原因
}

规则：
1. 图片不是账单/小票/支付记录（例如风景、人像、聊天截图、普通文档）→ is_bill=false，其余字段可留空/空串。
2. 任何关键信息因模糊/反光/遮挡/裁剪无法确认 → blurry=true，并在 reason 里写明看不清的部分。
3. 金额一律取正数（支出也不要给负数）；只取本单应付/实付金额，不要相加多张单，不要自行换算。
4. 日期统一成 YYYY-MM-DD；图片里只有“今天/昨天”等相对描述、没有绝对日期时，给空串 ""，不要猜。
5. 不要编造商户、金额、日期；不确定就留空，并降低 confidence。
6. 输出必须能被 json.loads 直接解析：不要中文引号、不要尾随逗号、不要在数字里带单位。"""

VISION_USER_PROMPT = "请从这张账单截图里提取信息，严格按要求的 JSON 输出。"


class BillImageError(Exception):
    """账单图片识别失败。

    code 取值：
      not_bill   图片不是账单
      blurry     图片关键信息看不清
      ambiguous  信息缺失/不合规（商户、金额、日期等）
      no_api_key 未配置智谱 API Key
      network    网络异常（超时/连接失败）
      http       接口返回非 200
      parse      模型输出无法解析成约定 JSON

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"BillImageError(code={self.code!r}, message={self.message!r})"


def _normalize_image_mime(mime_type) -> str:
    """把 'image/PNG; charset=binary' 归一化成 'image/png'；非字符串返回空串。"""
    if not isinstance(mime_type, str):
        return ""
    return mime_type.split(";", 1)[0].strip().lower()


def _sniff_image_mime(image_bytes: bytes):
    """magic bytes 粗校验；只用于日志提醒，不参与失败判定。"""
    head = image_bytes[:12]
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _format_size(num_bytes: int) -> str:
    return f"{num_bytes / 1024 / 1024:.2f}MB"


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是", "真"}:
            return True
        if text in {"false", "0", "no", "n", "否", "假", ""}:
            return False
    return default


def _parse_amount(value):
    """把 25.5 / "25.50" / "￥25.50元" / "1,200" 归一成 float；无法解析返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value)  # 去掉 ￥ ¥ $ , 元 空格 等
        if not cleaned or cleaned in {"-", ".", "-."} or cleaned.count("-") > 1:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


_CN_DATE_RE = re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*$")


def _normalize_bill_date(value):
    """严格归一化日期为 YYYY-MM-DD；无法确定返回 None（绝不回退成今天）。

    支持："%Y-%m-%d"、"2026/10/1"，以及中文 "2026年10月1日"（先正则归一化）。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    cn = _CN_DATE_RE.match(text)
    if cn:
        try:
            return datetime.strptime(
                f"{cn.group(1)}-{cn.group(2)}-{cn.group(3)}", "%Y-%m-%d"
            ).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _normalize_bill_type(value):
    """归一化收入/支出；无法识别返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in {"支出", "expense", "out", "pay", "支付", "付款"}:
        return "支出"
    if text in {"收入", "income", "in", "receive", "入账", "退款"}:
        return "收入"
    return None


def _parse_bill_json(content: str) -> dict:
    """先整体 json.loads；失败再抠 ```json ... ``` 代码块；仍失败 → parse。"""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    fenced = re.search(
        r"```(?:json)?\s*(.+?)```", content, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        try:
            data = json.loads(fenced.group(1).strip())
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    raise BillImageError(
        "parse",
        "模型没有按约定的 JSON 格式返回结果，账单信息无法解析。"
        "请先裁剪图片、只保留账单主体（商户/金额/日期）再上传，或重试一次。",
    )


def _extract_error_snippet(resp, api_key: str) -> str:
    """截断响应体用于报错；先脱敏 api key，再压空白，最多 80 字。"""
    try:
        text = resp.text or ""
    except Exception:  # pragma: no cover - 响应对象异常时不影响主流程
        text = ""
    text = str(text)
    if api_key:
        text = text.replace(api_key, "***")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80]


def extract_bill_from_image(image_bytes: bytes, mime_type: str, zhipu_api_key: str) -> dict:
    """调用智谱 GLM-4V 从账单截图中抽取结构化信息。

    返回::

        {"merchant": str, "amount": float, "occurred_at": "YYYY-MM-DD",
         "bill_type": "支出" | "收入", "confidence": float, "raw": str}

    失败统一抛 BillImageError(code, message)，message 为可展示的中文。
    raw 是模型原始输出，便于排查与页面“查看原始结果”。
    """
    # ---- 0) api key 检查：空 → no_api_key（且绝不回显 key 内容） ----
    if not isinstance(zhipu_api_key, str) or not zhipu_api_key.strip():
        raise BillImageError(
            "no_api_key",
            "未配置智谱 API Key。请在 .streamlit/secrets.toml 或环境变量里设置 "
            "ZHIPU_API_KEY（形如 xxxxxxxx.xxxxxxxx，首尾不要带空格/引号），然后重试。",
        )
    api_key = zhipu_api_key.strip()

    # ---- 1) 图片预处理：空/超大/mime 不合规 → ambiguous ----
    if not isinstance(image_bytes, (bytes, bytearray)):
        raise BillImageError(
            "ambiguous",
            "没有读取到图片内容。请重新选择图片后再次上传。",
        )
    if not image_bytes:
        raise BillImageError(
            "ambiguous",
            "上传的图片是空的（0 字节）。请重新上传一张完整的账单截图或小票照片。",
        )
    if len(image_bytes) > MAX_BILL_IMAGE_BYTES:
        raise BillImageError(
            "ambiguous",
            f"图片超过 5MB（当前 {_format_size(len(image_bytes))}），接口可能拒绝或超时。"
            "请压缩图片，或裁剪成只含商户、金额、日期的区域后再上传。",
        )
    normalized_mime = _normalize_image_mime(mime_type)
    if normalized_mime not in ALLOWED_IMAGE_MIME_TYPES:
        raise BillImageError(
            "ambiguous",
            f"不支持的图片格式（{mime_type!r}）。目前只支持 PNG、JPEG/JPG、WEBP，"
            "请把截图另存为其中一种格式后重试。",
        )

    sniffed_mime = _sniff_image_mime(bytes(image_bytes))
    if sniffed_mime is None:
        logger.warning("账单图片 magic bytes 未识别，按 mime=%s 继续", normalized_mime)
    elif sniffed_mime != normalized_mime and not (
        {sniffed_mime, normalized_mime} <= {"image/jpeg", "image/jpg"}
    ):
        logger.warning(
            "账单图片 mime 与实际内容不一致: 声明=%s 实际=%s", normalized_mime, sniffed_mime
        )

    # ---- 2) 组装请求（OpenAI 兼容，image 走 base64 data URL） ----
    vision_model = get_secret("ZHIPU_VISION_MODEL") or DEFAULT_VISION_MODEL
    b64 = base64.b64encode(bytes(image_bytes)).decode("ascii")
    payload = {
        "model": vision_model,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{normalized_mime};base64,{b64}"},
                    },
                    {"type": "text", "text": VISION_USER_PROMPT},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ---- 3) 发请求：超时/连接失败 → network ----
    try:
        resp = requests.post(
            ZHIPU_VISION_URL, headers=headers, json=payload, timeout=30
        )
    except requests.Timeout:
        raise BillImageError(
            "network",
            "调用账单识别接口超时（30 秒）。请检查网络后重试；图片较大时先压缩/裁剪。",
        ) from None
    except requests.ConnectionError:
        raise BillImageError(
            "network",
            "无法连接账单识别接口。请检查网络或代理设置，确认能访问 open.bigmodel.cn 后重试。",
        ) from None
    except requests.RequestException:
        raise BillImageError(
            "network",
            "账单识别请求发送失败。请稍后重试；若持续失败，请检查网络或代理设置。",
        ) from None

    # ---- 4) 非 200 → http（带状态码 + 截断 body，且脱敏 key） ----
    if resp.status_code != 200:
        snippet = _extract_error_snippet(resp, api_key)
        tail = f" 返回内容：{snippet}" if snippet else ""
        raise BillImageError(
            "http",
            f"账单识别接口返回异常（HTTP {resp.status_code}）。"
            f"请稍后重试；若为 401/403，请检查 ZHIPU_API_KEY 是否正确、是否开通视觉模型权限；"
            f"若为 429/余额相关，请检查账户额度。{tail}",
        )

    # ---- 5) 取出模型文本并解析 JSON ----
    try:
        resp_data = resp.json()
    except (ValueError, TypeError):
        raise BillImageError(
            "parse",
            "账单识别接口返回的不是合法 JSON，无法解析。请稍后重试。",
        ) from None
    try:
        content = resp_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BillImageError(
            "parse",
            "账单识别接口没有返回预期的内容结构，无法解析。请稍后重试。",
        ) from None
    if not isinstance(content, str) or not content.strip():
        raise BillImageError(
            "parse",
            "模型没有返回任何内容，账单信息无法解析。请重试，或换一张更清晰的账单截图。",
        )
    content = content.strip()
    data = _parse_bill_json(content)

    # ---- 6) 业务校验（顺序固定：先 not_bill，再 blurry，最后字段级 ambiguous） ----
    if not _coerce_bool(data.get("is_bill"), default=True):
        raise BillImageError(
            "not_bill",
            "这张图片看起来不是账单/小票/支付截图。请上传包含商户、金额和日期的账单截图，"
            "或直接手动记账。",
        )
    if _coerce_bool(data.get("blurry"), default=False):
        reason = data.get("reason")
        detail = (
            f"（{str(reason).strip()[:60]}）"
            if isinstance(reason, str) and reason.strip()
            else ""
        )
        raise BillImageError(
            "blurry",
            f"图片关键信息看不清{detail}。请靠近账单重拍，保证金额、日期、商户名清晰无遮挡，"
            "或裁剪到账单主体后再上传。",
        )

    merchant_raw = data.get("merchant")
    merchant = merchant_raw.strip() if isinstance(merchant_raw, str) else ""
    if not merchant:
        raise BillImageError(
            "ambiguous",
            "没能识别出商户名称。请换一张更清晰的账单截图，或手动填写商户后保存。",
        )

    amount = _parse_amount(data.get("amount"))
    if amount is None or amount <= 0:
        raise BillImageError(
            "ambiguous",
            "没能识别出有效金额（金额缺失、为 0 或为负数）。请重拍账单，或手动填写金额后保存。",
        )
    if amount > MAX_BILL_AMOUNT:
        raise BillImageError(
            "ambiguous",
            "识别到的金额超出合理范围（可能把多张单相加或小数点识别错了）。请核对后手动填写金额。",
        )

    paid_at_raw = data.get("paid_at")
    if not isinstance(paid_at_raw, str) or not paid_at_raw.strip():
        raise BillImageError(
            "ambiguous",
            "没能识别出消费日期。请手动选择日期后保存（系统不会自动用今天代替）。",
        )
    occurred_at = _normalize_bill_date(paid_at_raw)
    if occurred_at is None:
        raise BillImageError(
            "ambiguous",
            f"识别到的日期无法确定为 YYYY-MM-DD（原始值：「{paid_at_raw.strip()[:20]}」）。"
            "请手动选择日期后保存。",
        )

    type_raw = data.get("type")
    bill_type = _normalize_bill_type(type_raw)
    if bill_type is None:
        if "type" in data and str(type_raw).strip():
            raise BillImageError(
                "ambiguous",
                "无法判断这笔是收入还是支出。请手动选择类型后保存。",
            )
        # 字段整体缺失时按常见场景（消费）兜底，并在 raw/confidence 中保留原样
        bill_type = "支出"

    confidence = _parse_amount(data.get("confidence"))
    if confidence is None:
        confidence = 0.0
    confidence = max(0.0, min(1.0, float(confidence)))

    return {
        "merchant": merchant,
        "amount": round(float(amount), 2),
        "occurred_at": occurred_at,
        "bill_type": bill_type,
        "confidence": round(confidence, 4),
        "raw": content,
    }


# ============================================================================
# 月度复盘（DeepSeek Chat，纯文本点评，无工具调用）
# ----------------------------------------------------------------------------
# 与 get_embedding / get_ai_response 的区别：这里不查知识库、不带 tools，
# 只做「把 app.py 算好的月度统计交给模型，换回一段 100 字左右的点评」。
# 所有面向用户的失败统一走 MonthReviewError(message)，message 是可直接展示的中文；
# app.py 捕获后 st.warning/st.error 显示即可，不需要再看异常类型分支。
# ⚠ 日志绝不记录 api_key：本文件只在 headers 里用它，任何 logger 调用都不带它。
# ============================================================================

# 系统提示词必须**逐字**保持（产品口径，改一个字点评风格就跑偏）。
MONTH_REVIEW_SYSTEM_PROMPT = (
    "你是一个懂大学生、说话幽默略带毒舌的理财学长。请根据我的账单，用 100 字左右点评我的本月消费。"
    "如果超支了，请无情吐槽我（比如拿奶茶、外卖开刀）；如果省钱了，请狠狠夸我。不要废话，直接输出点评内容。"
)

DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
MONTH_REVIEW_MODEL = "deepseek-chat"


class MonthReviewError(Exception):
    """月度复盘失败。message 是面向用户的中文提示，可直接展示。"""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _review_amount(value) -> float:
    """把 stats 里的金额宽松转成 float（None / 坏值 / nan / inf → 0.0）。

    为什么宽容而不是抛错：stats 由 app.py 汇总而来，某个键缺失/为 None 只说明
    "这项没有数据"（例如本月没有收入），不该让整段复盘失败；而**总支出**这个
    关键值另有一条硬校验（<= 0 直接 MonthReviewError），不会被这里悄悄放过。
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount):
        return 0.0
    return amount


def _build_month_review_user_content(stats: dict) -> str:
    """把 app.py 汇总的 stats 排成中文条目（金额一律两位小数）。"""
    month = str(stats.get("month") or "本月")
    total_expense = _review_amount(stats.get("total_expense"))
    total_income = _review_amount(stats.get("total_income"))
    balance = _review_amount(stats.get("balance"))
    budget = _review_amount(stats.get("budget"))
    over_budget = _coerce_bool(stats.get("over_budget"), default=False)
    save_target = stats.get("save_target")

    # 前三大消费商户：stats 里已按金额降序，这里再截前 3 条兜底；
    # 结构不对的条目直接跳过（宁可少列一个，也不要 KeyError 把复盘搞挂）。
    merchant_lines = []
    top_merchants = stats.get("top_merchants") or []
    if isinstance(top_merchants, (list, tuple)):
        for item in top_merchants[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip() or "未知商户"
            merchant_lines.append(
                f"  {len(merchant_lines) + 1}. {name} {_review_amount(item.get('amount')):.2f} 元"
            )
    if not merchant_lines:
        merchant_lines.append("  无")

    # 最高单笔：可能为 None（本月没有支出时 app.py 会给 None）。
    max_single = stats.get("max_single")
    if isinstance(max_single, dict) and max_single:
        max_merchant = str(max_single.get("merchant") or "").strip() or "未知商户"
        max_date = str(max_single.get("date") or "").strip() or "日期未知"
        max_single_text = f"{max_merchant} / {_review_amount(max_single.get('amount')):.2f} 元 / {max_date}"
    else:
        max_single_text = "无"

    if budget <= 0:
        budget_text = "未设置"
    else:
        budget_text = f"{budget:.2f} 元"

    # 存钱目标：None / 0 都按"未设置"处理（0 元目标对学生没有意义，页面也不会让填 0）。
    target_amount = _review_amount(save_target)
    save_target_text = "未设置" if target_amount <= 0 else f"{target_amount:.2f} 元"

    lines = [
        f"月份：{month}",
        f"总支出：{total_expense:.2f} 元",
        f"总收入：{total_income:.2f} 元",
        f"结余：{balance:.2f} 元",
        f"预算：{budget_text}",
        f"是否超支：{'是' if over_budget else '否'}",
        "前三大消费商户：",
        *merchant_lines,
        f"最高单笔：{max_single_text}",
        f"存钱目标：{save_target_text}",
    ]
    return "这是我的本月消费数据，请按系统要求点评：\n" + "\n".join(lines)


def generate_month_review(stats: dict, api_key: str) -> str:
    """生成一段约 100 字的本月消费点评，返回去除首尾空白后的文本。

    失败一律抛 MonthReviewError（中文 message，可直接上页面）：
      · api_key 为空          → 未配置提示（且**不发请求**）；
      · 总支出 <= 0           → "本月还没有消费记录"（且**不发请求**）；
      · 超时 / 连接失败       → 对应中文提示；
      · 非 200                → 带状态码的提示；
      · 响应不是 JSON / 没有有效 content → "AI 没有返回有效点评"。
    日志只记月份与字数，**绝不**记 api_key 或请求头。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise MonthReviewError("未配置 DEEPSEEK_API_KEY，AI 复盘暂不可用")
    api_key = api_key.strip()

    # ② 本月没有任何支出：让用户先记几笔，而不是让模型对着一堆 0 硬编点评。
    #    stats.get 缺失/不是数字时 _review_amount 给 0.0，同样落到这条分支。
    if not isinstance(stats, dict):
        raise MonthReviewError("本月复盘数据不完整，请刷新后重试")
    if _review_amount(stats.get("total_expense")) <= 0:
        raise MonthReviewError("本月还没有消费记录，先记几笔再来复盘吧")

    month = str(stats.get("month") or "本月")
    user_content = _build_month_review_user_content(stats)
    payload = {
        "model": MONTH_REVIEW_MODEL,
        "messages": [
            {"role": "system", "content": MONTH_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.9：点评要有"人味"，不宜太保守；max_tokens 400 足够 100 字 + 余量。
        "temperature": 0.9,
        "max_tokens": 400,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=30)
    except requests.Timeout:
        logger.warning("月度复盘请求超时（月份=%s）", month)
        raise MonthReviewError("生成点评超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("月度复盘请求连接失败（月份=%s）", month)
        raise MonthReviewError("网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：对用户统一成网络问题；
        # 日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("月度复盘请求失败（月份=%s，异常=%s）", month, type(exc).__name__)
        raise MonthReviewError("网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("月度复盘接口返回 HTTP %s（月份=%s）", resp.status_code, month)
        raise MonthReviewError(f"AI 服务返回异常（HTTP {resp.status_code}）")

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效点评"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("月度复盘响应不是合法 JSON（月份=%s）", month)
        raise MonthReviewError("AI 没有返回有效点评，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("月度复盘响应结构不符合预期（月份=%s）", month)
        raise MonthReviewError("AI 没有返回有效点评，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("月度复盘响应 content 为空（月份=%s）", month)
        raise MonthReviewError("AI 没有返回有效点评，请重试")

    content = content.strip()
    logger.info("月度复盘生成成功（月份=%s，字数=%d）", month, len(content))
    return content


# ============================================================================
# 账单自动分类（DeepSeek Chat，只返回一个分类标签）
# ----------------------------------------------------------------------------
# 与月度复盘的区别：这里要的是**可程序消费的短标签**，不是自然语言。所以：
#   · temperature=0：同一商户每次应得到同一个分类，分类统计才稳定；
#   · max_tokens=32：只够输出 {"category": "..."}，从成本上堵住模型"解释一番"；
#   · 只接受调用方传入的候选分类：模型编出新分类时抛 CategoryError，
#     由 db.auto_categorize 回落「其他」—— 写路径不会因为模型胡说来失败；
#   · api_key 为空时**不发请求**（先检查，再花网络往返）。
# 失败一律 CategoryError（message 是可直接展示的中文）；日志绝不记录 api_key。
# ============================================================================

CATEGORY_MODEL = "deepseek-chat"

CATEGORY_SYSTEM_PROMPT = (
    "你是账单分类器。只能从用户给出的候选分类中选择一个，不能新增分类、不能解释。"
    '只输出 JSON，格式为 {"category": "候选分类之一"}。'
)


class CategoryError(Exception):
    """AI 分类失败。message 是面向用户的中文提示，可直接展示。"""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _build_category_user_content(merchant, description, categories) -> str:
    """把商户 / 描述 / 候选分类排成 user 消息（模型只需要这几项信息）。"""
    merchant_text = "" if merchant is None else str(merchant).strip()
    description_text = "" if description is None else str(description).strip()
    return (
        f"商户：{merchant_text or '未知商户'}\n"
        f"描述：{description_text or '无'}\n"
        f"候选分类：{'、'.join(categories)}\n"
        '请只输出 JSON：{"category": "候选分类之一"}'
    )


def _extract_category(content: str):
    """从模型回复里抠出分类名；抠不出返回 None（由调用方抛 CategoryError）。

    正常情况下 content 就是 {"category": "餐饮"}。兜底两层，因为模型偶尔会裹
    markdown 代码块或加一句前缀：
      ① 直接 json.loads；
      ② 正则截第一个 {...} 再 json.loads；
      ③ 直接正则抠 "category": "..." 的值。
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("category"), str):
        return data["category"].strip()

    match = re.search(r"\{.*\}", content, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("category"), str):
            return data["category"].strip()

    match = re.search(r'"category"\s*:\s*"([^"]*)"', content)
    if match:
        return match.group(1).strip()
    return None


def classify_category(merchant: str, description: str, api_key: str, categories: list[str]) -> str:
    """让 DeepSeek 从 categories 里选一个分类，返回该分类字符串。

    失败一律抛 CategoryError（中文 message）：
      · api_key 为空            → "未配置 DEEPSEEK_API_KEY..."（且**不发请求**）；
      · 候选分类为空            → 无法分类（且不发请求）；
      · 超时 / 连接失败         → 对应中文提示；
      · 非 200                  → 带状态码的提示；
      · 响应不是 JSON / 没有有效 content / 分类不在候选列表 → 对应中文提示。
    日志只记商户与分类（成功）/ 异常类型（失败），**绝不**记 api_key 或请求头。
    """
    # ① api_key：先查它，没 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise CategoryError("未配置 DEEPSEEK_API_KEY，AI 分类不可用")
    api_key = api_key.strip()

    # ② 候选分类：空列表时模型无从选择，直接拒绝（同样不发请求）。
    if not isinstance(categories, (list, tuple)) or not categories:
        raise CategoryError("候选分类列表为空，无法进行 AI 分类")
    category_list = [str(item).strip() for item in categories if str(item).strip()]
    if not category_list:
        raise CategoryError("候选分类列表为空，无法进行 AI 分类")

    merchant_text = "" if merchant is None else str(merchant).strip()
    description_text = "" if description is None else str(description).strip()
    payload = {
        "model": CATEGORY_MODEL,
        "messages": [
            {"role": "system", "content": CATEGORY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_category_user_content(merchant_text, description_text, category_list),
            },
        ],
        # temperature=0 保分类稳定；max_tokens=32 只够 JSON，不给"解释"留空间。
        "temperature": 0,
        "max_tokens": 32,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=15)
    except requests.Timeout:
        logger.warning("AI 分类请求超时（商户=%s）", merchant_text)
        raise CategoryError("AI 分类超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("AI 分类请求连接失败（商户=%s）", merchant_text)
        raise CategoryError("网络连接失败，AI 分类暂不可用") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("AI 分类请求失败（商户=%s，异常=%s）", merchant_text, type(exc).__name__)
        raise CategoryError("网络连接失败，AI 分类暂不可用") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("AI 分类接口返回 HTTP %s（商户=%s）", resp.status_code, merchant_text)
        raise CategoryError(f"AI 分类服务返回异常（HTTP {resp.status_code}）")

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效分类结果"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("AI 分类响应不是合法 JSON（商户=%s）", merchant_text)
        raise CategoryError("AI 没有返回有效的分类结果") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("AI 分类响应结构不符合预期（商户=%s）", merchant_text)
        raise CategoryError("AI 没有返回有效的分类结果") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("AI 分类响应 content 为空（商户=%s）", merchant_text)
        raise CategoryError("AI 没有返回有效的分类结果")

    # ⑥ 分类必须在候选列表内：模型编一个新分类会让分类统计出现"计划外"的组，
    #    宁可报错让上层回落 "其他"，也不静默接受。
    category = _extract_category(content.strip())
    if category is None:
        logger.warning("AI 分类响应无法解析出 category（商户=%s）", merchant_text)
        raise CategoryError("AI 没有返回有效的分类结果")
    if category not in category_list:
        logger.warning("AI 分类结果不在候选列表（商户=%s，结果=%s）", merchant_text, category)
        raise CategoryError(f"AI 返回的分类「{category}」不在候选列表中")

    logger.info("AI 分类成功（商户=%s，分类=%s）", merchant_text, category)
    return category


# ============================================================================
# 健康分点评（DeepSeek Chat，纯文本一句话）
# ----------------------------------------------------------------------------
# 输入是 health_score.compute_health_score 已经算好的分数/等级/称号/扣分项，
# 本函数只负责"翻译成人话"，不做任何评分逻辑（评分必须留在纯函数里可单测）。
# 失败一律 HealthCommentError（message 是可直接展示的中文）；日志绝不记录 api_key。
# ============================================================================

# 系统提示词必须**逐字**保持（产品口径，改一个字点评风格就跑偏）。
HEALTH_COMMENT_SYSTEM_PROMPT = (
    "你是一个说话犀利、懂大学生的理财教练。请根据用户的财务健康分和扣分项，"
    "用不超过 30 个字写一句点评：先点出分数，再给一条最关键的改进建议，"
    "不要寒暄、不要解释、不要分点。"
)

HEALTH_COMMENT_MAX_CHARS = 40  # 返回文本的硬上限（30 字要求 + 余量，超了截断）


class HealthCommentError(Exception):
    """健康分点评失败。message 是面向用户的中文提示，可直接展示。"""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _build_health_comment_user_content(score, grade, title, deductions) -> str:
    """把分数/等级/称号/扣分项排成 user 消息（空扣分项写成「无」）。"""
    score_text = "" if score is None else str(score).strip()
    grade_text = "" if grade is None else str(grade).strip()
    title_text = "" if title is None else str(title).strip()

    items = []
    if isinstance(deductions, (list, tuple)):
        for item in deductions:
            text = "" if item is None else str(item).strip()
            if text:
                items.append(text)
    deductions_text = "；".join(items) if items else "无"

    return (
        f"得分：{score_text or '未知'}\n"
        f"等级：{grade_text or '未知'}\n"
        f"称号：{title_text or '未知'}\n"
        f"扣分项：{deductions_text}\n"
        "请按要求写一句点评。"
    )


def generate_health_comment(score: int, grade: str, title: str, deductions: list[str], api_key: str) -> str:
    """根据健康分生成一句不超过 30 字的点评，返回去除首尾空白后的文本。

    失败一律抛 HealthCommentError（中文 message，可直接上页面）：
      · api_key 为空          → 未配置提示（且**不发请求**）；
      · 超时 / 连接失败       → 对应中文提示；
      · 非 200                → 带状态码的提示；
      · 响应不是 JSON / 没有有效 content → "AI 没有返回有效点评"。
    返回文本超过 40 字时截断到 40 字并补「…」（防止模型不听话写成长篇）。
    日志只记分数/等级与字数，**绝不**记 api_key 或请求头。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise HealthCommentError("未配置 DEEPSEEK_API_KEY，AI 点评暂不可用")
    api_key = api_key.strip()

    user_content = _build_health_comment_user_content(score, grade, title, deductions)
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": HEALTH_COMMENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.8：点评要有点"人味"但不至于跑题；max_tokens 80 够 30 字。
        "temperature": 0.8,
        "max_tokens": 80,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ② 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=20)
    except requests.Timeout:
        logger.warning("健康分点评请求超时（分数=%s）", score)
        raise HealthCommentError("生成点评超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("健康分点评请求连接失败（分数=%s）", score)
        raise HealthCommentError("网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("健康分点评请求失败（分数=%s，异常=%s）", score, type(exc).__name__)
        raise HealthCommentError("网络连接失败，请检查网络") from None

    # ③ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("健康分点评接口返回 HTTP %s（分数=%s）", resp.status_code, score)
        raise HealthCommentError(f"AI 服务返回异常（HTTP {resp.status_code}）")

    # ④ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效点评"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("健康分点评响应不是合法 JSON（分数=%s）", score)
        raise HealthCommentError("AI 没有返回有效点评，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("健康分点评响应结构不符合预期（分数=%s）", score)
        raise HealthCommentError("AI 没有返回有效点评，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("健康分点评响应 content 为空（分数=%s）", score)
        raise HealthCommentError("AI 没有返回有效点评，请重试")

    content = content.strip()
    if len(content) > HEALTH_COMMENT_MAX_CHARS:
        content = content[:HEALTH_COMMENT_MAX_CHARS] + "…"
    logger.info("健康分点评生成成功（分数=%s，等级=%s，字数=%d）", score, grade, len(content))
    return content


# ============================================================================
# 晚间点评（DeepSeek Chat，纯文本一句话，供 send_daily_report.py 批量调用）
# ----------------------------------------------------------------------------
# 与健康分点评同构：本函数只负责把"今天的账"翻译成人话，不做任何统计逻辑。
# 失败一律 EveningCommentError（message 为可展示的中文）；日志绝不记录 api_key。
# 批量任务对每个用户单独调用，单用户失败由调用方兜底，不得中断整批。
# ============================================================================

# 系统提示词必须**逐字**保持（产品口径，改一个字点评风格就跑偏）。
EVENING_COMMENT_SYSTEM_PROMPT = (
    '你是一个陪大学生记账的贴心又有点毒舌的财务搭子。请根据这名用户今天的消费情况，'
    '写一句不超过 25 个字的晚间消费点评（例如"今天花得有点多哦"）。'
    '不要寒暄、不要解释、不要分点，直接给一句话。'
)

EVENING_COMMENT_MAX_CHARS = 40  # 返回文本硬上限（25 字要求 + 余量，超了截断）


class EveningCommentError(Exception):
    """晚间点评失败。message 是面向用户的中文提示，可直接展示。"""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _evening_money(value) -> float:
    """把 stats 里的金额安全转成 float（None/空串/非法值 → 0.0）。

    批量任务的数据来自库里的聚合结果，脏数据不应让单个用户拖垮整批。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_evening_comment_user_content(stats: dict) -> str:
    """把 stats 排成 user 消息：本月支出/收入/结余、今日支出、是否比平时花得多。"""
    stats = stats or {}
    username = str(stats.get("username") or "同学").strip() or "同学"
    month_expense = _evening_money(stats.get("month_expense"))
    month_income = _evening_money(stats.get("month_income"))
    month_balance = stats.get("month_balance")
    if month_balance is None:
        # 契约里有 month_balance；缺省时用 收入-支出 兜底，避免把 None 塞进提示词。
        month_balance = month_income - month_expense
    month_balance = _evening_money(month_balance)
    today_expense = _evening_money(stats.get("today_expense"))
    overspend = bool(stats.get("today_overspend"))
    return (
        f"用户：{username}\n"
        f"本月支出：{month_expense:.2f} 元\n"
        f"本月收入：{month_income:.2f} 元\n"
        f"本月结余：{month_balance:.2f} 元\n"
        f"今日支出：{today_expense:.2f} 元\n"
        f"今天是否比平时花得多：{'是' if overspend else '否'}\n"
        "请按要求写一句点评。"
    )


def generate_evening_comment(stats: dict, api_key: str) -> str:
    """根据用户今天的消费情况生成一句不超过 25 字的晚间点评。

    失败一律抛 EveningCommentError（中文 message，可直接展示）：
      · api_key 为空 → 未配置提示（且**不发请求**）；
      · 超时 / 连接失败 → 对应中文提示；
      · 非 200 → 带状态码的提示；
      · 响应不是 JSON / 没有有效 content → "AI 没有返回有效点评"。
    返回文本去除首尾空白；超过 40 字时截断到 40 字并补「…」（防止模型不听话）。
    日志只记字数，**绝不**记 api_key、请求头或真实用户名。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise EveningCommentError("未配置 DEEPSEEK_API_KEY，AI 晚间点评暂不可用")
    api_key = api_key.strip()

    user_content = _build_evening_comment_user_content(stats)
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": EVENING_COMMENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.9：晚间点评要有"人味"（毒舌/俏皮），不必保守；
        # max_tokens 60 对 25 字一句话足够，同时从成本上限制它写成小作文。
        "temperature": 0.9,
        "max_tokens": 60,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ② 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=20)
    except requests.Timeout:
        logger.warning("晚间点评请求超时")
        raise EveningCommentError("生成晚间点评超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("晚间点评请求连接失败")
        raise EveningCommentError("网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("晚间点评请求失败（异常=%s）", type(exc).__name__)
        raise EveningCommentError("网络连接失败，请检查网络") from None

    # ③ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("晚间点评接口返回 HTTP %s", resp.status_code)
        raise EveningCommentError(f"AI 服务返回异常（HTTP {resp.status_code}）")

    # ④ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效点评"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("晚间点评响应不是合法 JSON")
        raise EveningCommentError("AI 没有返回有效点评，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("晚间点评响应结构不符合预期")
        raise EveningCommentError("AI 没有返回有效点评，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("晚间点评响应 content 为空")
        raise EveningCommentError("AI 没有返回有效点评，请重试")

    content = content.strip()
    if len(content) > EVENING_COMMENT_MAX_CHARS:
        content = content[:EVENING_COMMENT_MAX_CHARS] + "…"
    logger.info("晚间点评生成成功（字数=%d）", len(content))
    return content


# ============================================================================
# AI 智能预算（DeepSeek Chat，输出严格 JSON：类别 → 预算金额）
# ----------------------------------------------------------------------------
# 输入是 app.py 汇总的上月消费分类，输出是本月各类别的建议预算。
# 与其它 AI 能力的区别：结果是**可程序消费的 JSON**，所以解析/校验都比点评严格：
#   · 先整体 json.loads，失败再抠 ```json 代码块，仍失败统一报“不是合法 JSON”；
#   · 类别必须是非空字符串；金额必须是正有限数（"800" 这类数字字符串可转）；
#   · 模型给多了（合计 > 总预算）不让用户背锅：按比例缩放到总预算，末项吸收误差；
#   · 返回 {类别: float}，不含 total —— 元数据由 db.generate_smart_budget 组装。
# 所有面向用户的失败统一走 BudgetPlanError(message)，message 是可直接展示的中文。
# ⚠ 日志绝不记录 api_key：本文件只在 headers 里用它，任何 logger 调用都不带它。
# ============================================================================

# 系统提示词必须**逐字**保持（产品口径，改一个字输出格式就可能跑偏）。
BUDGET_PLAN_SYSTEM_PROMPT = (
    "你是一个贴心的理财学长，请根据我上个月的消费分类，帮我制定本月各项（餐饮、购物、娱乐、学习等）"
    "的具体预算金额，总和不能超过我设定的总预算。只输出 JSON，格式为 "
    '{"餐饮": 800, "购物": 300, "其他": 400}，不要输出 JSON 之外的任何文字。'
)

BUDGET_PLAN_MODEL = "deepseek-chat"
BUDGET_PLAN_TEMPERATURE = 0.3
BUDGET_PLAN_MAX_TOKENS = 300
BUDGET_PLAN_TIMEOUT = 25


class BudgetPlanError(Exception):
    """AI 预算方案失败。message 是面向用户的中文提示，可直接展示。"""

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _normalize_total_budget(total_budget) -> float:
    """把总预算转成正的有限 float；非法（≤0 / NaN / 非数字）抛 BudgetPlanError。"""
    try:
        value = float(total_budget)
    except (TypeError, ValueError):
        raise BudgetPlanError("总预算必须是大于 0 的数字") from None
    if not math.isfinite(value) or value <= 0:
        raise BudgetPlanError("总预算必须是大于 0 的数字")
    return value


def _build_budget_plan_user_content(stats: dict, total_budget: float) -> str:
    """把上月统计排成 user 消息：月份、总支出、各类别金额与占比、本月总预算上限。"""
    month = str(stats.get("month") or "上月")
    total = _review_amount(stats.get("total"))

    # 分类金额按降序排列，占比基于上月总支出；脏值/非正数直接跳过（不影响其余类别）。
    parsed = []
    categories = stats.get("category_totals")
    if isinstance(categories, dict):
        for name, amount in categories.items():
            cat_name = str(name).strip()
            if not cat_name:
                continue
            value = _review_amount(amount)
            if value <= 0:
                continue
            parsed.append((cat_name, value))
    parsed.sort(key=lambda item: item[1], reverse=True)

    category_lines = []
    for cat_name, value in parsed:
        share = value / total * 100 if total > 0 else 0.0
        category_lines.append(f"  - {cat_name}：{value:.2f} 元（占比 {share:.1f}%）")
    if not category_lines:
        category_lines.append("  - 无分类数据")

    return (
        f"上月月份：{month}\n"
        f"上月总支出：{total:.2f} 元\n"
        "上月各消费类别金额与占比：\n"
        + "\n".join(category_lines)
        + f"\n本月总预算上限：{total_budget:.2f} 元\n"
        "请按系统要求只输出 JSON。"
    )


def _parse_budget_plan_json(content: str) -> dict:
    """解析模型返回的预算 JSON：整体 json.loads → ```json 代码块 → 统一报错。"""
    text = content.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        fenced = re.search(
            r"```(?:json)?\s*(.+?)```", text, flags=re.IGNORECASE | re.DOTALL
        )
        if not fenced:
            raise BudgetPlanError("AI 返回的预算不是合法 JSON，请重试") from None
        try:
            data = json.loads(fenced.group(1).strip())
        except (ValueError, TypeError):
            raise BudgetPlanError("AI 返回的预算不是合法 JSON，请重试") from None

    if not isinstance(data, dict) or not data:
        raise BudgetPlanError("AI 没有返回有效的预算分类，请重试")

    items = {}
    for raw_name, raw_amount in data.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise BudgetPlanError("AI 返回的预算类别不合法，请重试")
        cat_name = raw_name.strip()
        # 允许 "800" 这类数字字符串；bool 是 int 子类，单独排除，避免 True → 1.0。
        if isinstance(raw_amount, bool):
            raise BudgetPlanError("AI 返回的预算金额不合法，请重试")
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            raise BudgetPlanError("AI 返回的预算金额不合法，请重试") from None
        if not math.isfinite(amount) or amount <= 0:
            raise BudgetPlanError("AI 返回的预算金额不合法，请重试")
        items[cat_name] = amount

    return items


def _scale_budget_items(items: dict, total_budget: float) -> dict:
    """合计超预算时按比例缩放到总预算（两位小数，末项吸收四舍五入误差）。"""
    total = sum(items.values())
    if total <= total_budget:
        return {name: float(value) for name, value in items.items()}

    factor = total_budget / total
    names = list(items)
    scaled = {}
    running = 0.0
    for index, name in enumerate(names):
        if index == len(names) - 1:
            # 末项 = 预算 - 已分配，保证合计精确等于预算（不会因逐项四舍五入多出 1 分）。
            amount = round(max(0.0, total_budget - running), 2)
        else:
            amount = round(items[name] * factor, 2)
            running = round(running + amount, 2)
        scaled[name] = float(amount)
    return scaled


def generate_budget_plan(stats: dict, total_budget: float, api_key: str) -> dict:
    """根据上月消费分类生成本月分类预算，返回 {类别: float}（不含 total）。

    失败一律抛 BudgetPlanError（中文 message，可直接上页面）：
      · api_key 为空                → 未配置提示（且**不发请求**）；
      · 总预算 ≤ 0 / NaN / 非数字    → "总预算必须是大于 0 的数字"（且**不发请求**）；
      · 超时 / 连接失败              → 对应中文提示；
      · 非 200                       → 带状态码的提示；
      · 响应不是 JSON / 没有 content → "AI 没有返回有效预算"；
      · 模型输出不是合法 JSON        → "AI 返回的预算不是合法 JSON，请重试"。
    合计超过总预算时按比例缩放（保留 2 位，末项吸收误差）。
    日志只记月份与类别数，**绝不**记 api_key 或请求头。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise BudgetPlanError("未配置 DEEPSEEK_API_KEY，AI 智能预算暂不可用")
    api_key = api_key.strip()

    # ② 总预算：非法值不发请求，直接给用户一句可操作的提示。
    budget = _normalize_total_budget(total_budget)

    if not isinstance(stats, dict):
        raise BudgetPlanError("预算数据不完整，请刷新后重试")
    month = str(stats.get("month") or "上月")
    user_content = _build_budget_plan_user_content(stats, budget)
    payload = {
        "model": BUDGET_PLAN_MODEL,
        "messages": [
            {"role": "system", "content": BUDGET_PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.3：预算要稳定、可复现；max_tokens 300 足够十来个类别。
        "temperature": BUDGET_PLAN_TEMPERATURE,
        "max_tokens": BUDGET_PLAN_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=BUDGET_PLAN_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("AI 预算请求超时（月份=%s）", month)
        raise BudgetPlanError("生成预算超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("AI 预算请求连接失败（月份=%s）", month)
        raise BudgetPlanError("网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("AI 预算请求失败（月份=%s，异常=%s）", month, type(exc).__name__)
        raise BudgetPlanError("网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("AI 预算接口返回 HTTP %s（月份=%s）", resp.status_code, month)
        raise BudgetPlanError(f"AI 服务返回异常（HTTP {resp.status_code}）")

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效预算"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("AI 预算响应不是合法 JSON（月份=%s）", month)
        raise BudgetPlanError("AI 没有返回有效预算，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("AI 预算响应结构不符合预期（月份=%s）", month)
        raise BudgetPlanError("AI 没有返回有效预算，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("AI 预算响应 content 为空（月份=%s）", month)
        raise BudgetPlanError("AI 没有返回有效预算，请重试")

    # ⑥ 严格解析 + 校验 + 超预算缩放。
    items = _parse_budget_plan_json(content)
    scaled = sum(items.values()) > budget
    plan = _scale_budget_items(items, budget)
    logger.info(
        "AI 预算生成成功（月份=%s，类别数=%d，缩放=%s）",
        month,
        len(plan),
        "是" if scaled else "否",
    )
    return plan


# ============================================================================
# 语音记账 ①：录音 → 文字（智谱 GLM-ASR 文件级转写）
# ----------------------------------------------------------------------------
# 设计约定（与上方 BillImageError 区块保持一致）：
# · 所有“面向用户”的失败统一走 AudioTranscribeError(code, message)：
#     - code    供页面做分支处理（no_api_key / empty_audio / too_big / too_long /
#               network / http / empty_text）；
#     - message 是可直接渲染到页面的中文，必须带可操作建议；
# · 前置校验全部在**发请求之前**完成：没 key、没音频、太大、太长都不该白跑一次网络；
# · 日志只记「大小 / 时长 / 状态码 / 字数」，绝不记 api key，也绝不记音频内容；
# · WAV 时长解析失败（webm/ogg/损坏头）**不拦截**，交给服务端判（服务端也有 30 秒硬限制）。
# ============================================================================

ZHIPU_ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"
ASR_MODEL = "glm-asr-2512"
MAX_AUDIO_BYTES = 25 * 1024 * 1024      # 25MB：接口单文件上限
MAX_AUDIO_SECONDS = 30.0                # 30 秒：接口单段音频上限
ASR_TIMEOUT = 30                        # 转写比纯文本接口慢，给 30 秒
ASR_UPLOAD_FILENAME = "voice.wav"


class AudioTranscribeError(Exception):
    """语音转写失败。

    code 取值：
      no_api_key   未配置智谱 API Key（**未发请求**）
      empty_audio  音频为空 / 不是 bytes（**未发请求**）
      too_big      音频文件超过 25MB（**未发请求**）
      too_long     WAV 时长超过 30 秒（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200
      empty_text   接口返回的 text 缺失或空白（等于“没听清”）

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"AudioTranscribeError(code={self.code!r}, message={self.message!r})"


def _wav_duration_seconds(audio_bytes: bytes):
    """解析 WAV 头算时长：时长 = data_size / byte_rate。

    返回秒数（float）；头不合法 / 不是 WAV（例如 webm）/ 解析失败一律返回 None，
    由调用方决定“不拦截、交给服务端判”。
    """
    data = bytes(audio_bytes)
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None

    pos = 12
    byte_rate = 0
    data_size = None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt ":
            # fmt 结构：audio_format(2) channels(2) sample_rate(4) byte_rate(4) ...
            if chunk_size < 16 or body + 16 > len(data):
                return None
            byte_rate = int.from_bytes(data[body + 8:body + 12], "little")
        elif chunk_id == b"data":
            if body > len(data):
                return None
            # 声明大小可能大于实际剩余长度（截断/伪造头）：以实际可读长度为准
            data_size = min(chunk_size, len(data) - body)
            break
        # chunk 按偶数字节对齐（chunk_size 为奇数时补 1 字节）
        pos = body + chunk_size + (chunk_size & 1)

    if data_size is None or byte_rate <= 0:
        return None
    return data_size / float(byte_rate)


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.wav", zhipu_api_key: str = "") -> str:
    """把一段录音转成文字（智谱文件级 ASR：POST /paas/v4/audio/transcriptions）。

    参数顺序与默认值不可改（app.py 会按位置/关键字调用）：
        transcribe_audio(audio_bytes, filename="voice.wav", zhipu_api_key="")

    filename 只用于调用方语义兼容，**不参与**上传命名：服务端按扩展名判断音频容器格式，
    浏览器 MediaRecorder 常给 "voice.webm"，原样透传会被按扩展名直接拒掉，
    因此上传名固定为 voice.wav（见下方第 ⑤ 步注释）。

    返回：识别文本（已 strip）。
    失败：抛 AudioTranscribeError(code, message)，所有 code 见类文档。
    """
    # ---- ① key：空/纯空白直接拒绝，**不发请求** ----
    if not isinstance(zhipu_api_key, str) or not zhipu_api_key.strip():
        raise AudioTranscribeError(
            "no_api_key",
            "未配置 ZHIPU_API_KEY，语音记账暂不可用。请在 .streamlit/secrets.toml 里配置 "
            "ZHIPU_API_KEY（形如 xxxxxxxx.xxxxxxxx，首尾不要带空格/引号）后重试。",
        )
    api_key = zhipu_api_key.strip()

    # ---- ② 音频字节：类型 / 空值，**不发请求** ----
    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise AudioTranscribeError(
            "empty_audio",
            "没有收到录音数据，请按住录音按钮、说完再松开，然后重试。",
        )
    raw = bytes(audio_bytes)
    if not raw:
        raise AudioTranscribeError(
            "empty_audio",
            "录音是空的（0 字节），请再说一遍，尽量靠近麦克风。",
        )

    # ---- ③ 体积：> 25MB 直接拒绝（接口硬限制），**不发请求** ----
    if len(raw) > MAX_AUDIO_BYTES:
        raise AudioTranscribeError(
            "too_big",
            f"录音文件太大（{_format_size(len(raw))}，接口上限 25MB），"
            "请缩短录音到 30 秒以内再试。",
        )

    # ---- ④ 时长：能解析 WAV 头就按 30 秒拦；解析不了（webm 等）不拦，交给服务端判 ----
    duration = _wav_duration_seconds(raw)
    if duration is None:
        # 不是标准 WAV（浏览器录的 webm/ogg、或头损坏）：本地算不出时长，
        # 只记日志、继续上传，由服务端按它自己的 30 秒硬限制判定。
        logger.info(
            "音频头不是可解析的 WAV，跳过长时长前置校验（大小=%.2fKB）", len(raw) / 1024
        )
    elif duration > MAX_AUDIO_SECONDS:
        raise AudioTranscribeError(
            "too_long",
            f"录音太长了（约 {duration:.1f} 秒，接口上限 30 秒），"
            "请缩短到 30 秒以内再说一遍。",
        )

    # ---- ⑤ 上传文件名固定 voice.wav ----
    # 注意：无论调用方传什么 filename（哪怕没有 .wav 后缀，或传的是 voice.webm），
    # 上传名都统一成 voice.wav —— 服务端会按扩展名判断音频容器格式，
    # 浏览器录制的 webm/ogg 若按原名上传会被直接拒绝（4xx），改名后由服务端解码。
    upload_filename = ASR_UPLOAD_FILENAME
    duration_text = f"{duration:.1f}s" if duration is not None else "未知"

    # ---- ⑥ 发请求：multipart，文件级转写 ----
    logger.info("语音转写请求（大小=%.2fKB，时长=%s）", len(raw) / 1024, duration_text)
    try:
        resp = requests.post(
            ZHIPU_ASR_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (upload_filename, raw, "audio/wav")},
            data={"model": ASR_MODEL, "stream": "false"},
            timeout=ASR_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("语音转写超时（大小=%.2fKB，时长=%s）", len(raw) / 1024, duration_text)
        raise AudioTranscribeError("network", "语音识别超时，请检查网络后重试。") from None
    except requests.ConnectionError:
        logger.warning("语音转写连接失败（大小=%.2fKB）", len(raw) / 1024)
        raise AudioTranscribeError("network", "网络连接失败，请检查网络或代理设置后重试。") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型，避免把 headers / key 带出去
        logger.warning("语音转写请求失败（大小=%.2fKB，异常=%s）", len(raw) / 1024, type(exc).__name__)
        raise AudioTranscribeError("network", "语音识别请求发送失败，请稍后重试。") from None

    # ---- ⑦ 非 200 → http（状态码 + 截断 body，body 先脱敏 key 再截断 80 字） ----
    if resp.status_code != 200:
        snippet = _extract_error_snippet(resp, api_key)
        tail = f" 返回内容：{snippet}" if snippet else ""
        logger.warning("语音转写接口返回 HTTP %s（大小=%.2fKB）", resp.status_code, len(raw) / 1024)
        raise AudioTranscribeError(
            "http",
            f"语音识别接口返回异常（HTTP {resp.status_code}）。请稍后重试；"
            f"若为 401/403，请检查 ZHIPU_API_KEY 是否正确、是否开通语音模型权限；"
            f"若为 429/余额相关，请检查账户额度。{tail}",
        )

    # ---- ⑧ 解析：非 JSON / text 缺失或空白 → empty_text（等于“没听清”） ----
    try:
        resp_data = resp.json()
    except (ValueError, TypeError):
        logger.warning("语音转写响应不是合法 JSON（大小=%.2fKB）", len(raw) / 1024)
        raise AudioTranscribeError(
            "empty_text", "没听清，请再说一遍，尽量靠近麦克风。"
        ) from None
    text_value = resp_data.get("text") if isinstance(resp_data, dict) else None
    if not isinstance(text_value, str) or not text_value.strip():
        logger.warning(
            "语音转写返回空文本（大小=%.2fKB，状态码=%s）", len(raw) / 1024, resp.status_code
        )
        raise AudioTranscribeError(
            "empty_text", "没听清，请再说一遍，尽量靠近麦克风。"
        )
    text = text_value.strip()
    logger.info(
        "语音转写成功（大小=%.2fKB，时长=%s，字数=%d）",
        len(raw) / 1024,
        duration_text,
        len(text),
    )
    return text


# ============================================================================
# 语音记账 ②：口述文字 → 结构化账单（DeepSeek Chat，只输出 JSON）
# ----------------------------------------------------------------------------
# · 失败统一走 TextBillError(code, message)，code ∈
#     no_api_key / empty_text / network / http / parse / invalid_bill；
# · 前置校验（key 空、text 空）在发请求之前，绝不白跑网络；
# · 日期：模型可以说空（→ 用今天），但只要给了，就必须是真实存在的 YYYY-MM-DD，
#   绝不静默改成今天（2026-02-30 这类必须报 invalid_bill）；
# · 日志只记字数/状态码，绝不记 key，也绝不记用户原话（可能是个人消费信息）。
# ============================================================================

# 追加区块需要 date.today()：为保持“只追加、不改文件头部 import”，这里就地导入。
from datetime import date as _date

TEXT_BILL_MODEL = "deepseek-chat"
TEXT_BILL_TEMPERATURE = 0        # 抽取信息要可复现，不要发散
TEXT_BILL_MAX_TOKENS = 200       # 一行 JSON 足够
TEXT_BILL_TIMEOUT = 20

TEXT_BILL_SYSTEM_PROMPT = """你是一个账单信息抽取器。请从用户的一句中文口述里抽取：商户/项目名称、金额（正数数字）、交易日期、收支类型。只输出一个 JSON 对象，不要任何解释。日期必须是 YYYY-MM-DD；如果用户说“今天/昨天/前天”，请按我给你的今天日期换算成绝对日期；如果完全没提日期，paid_at 给空串。金额取正数，不要说“元”。收入类（工资/红包/退款/收款）type 为“收入”，其余为“支出”。"""


class TextBillError(Exception):
    """口述文字 → 账单结构化失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_text   输入文字为空/纯空白（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200
      parse        模型输出不是可解析的 JSON（抠 ```json 代码块后仍失败）
      invalid_bill 解析成功但字段不合规（商户空、金额<=0/超上限、日期非法）

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"TextBillError(code={self.code!r}, message={self.message!r})"


def _loads_json_object(content: str):
    """先整体 json.loads；失败再抠 ```json ... ``` 代码块；都不行返回 None（不抛）。"""
    candidates = [content]
    fenced = re.search(
        r"```(?:json)?\s*(.+?)```", content, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        candidates.append(fenced.group(1))
    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _parse_text_bill_date(value):
    """严格按 %Y-%m-%d 解析真实日历日；其它格式 / 不存在的日期一律返回 None。

    与 _normalize_bill_date 的区别：这条链路吃的是模型的**结构化输出**，
    格式不合规就是违约（或用户没说清楚），按 invalid_bill 让用户重说一句，
    不做“2026/10/1”“2026年10月1日”之类的宽松兜底。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def extract_bill_from_text(text: str, api_key: str = "") -> dict:
    """把一句中文口述（已转写好的文字）抽成一条账单。

    返回：
        {"merchant": str, "amount": float, "occurred_at": "YYYY-MM-DD",
         "bill_type": "支出"|"收入", "raw": str}
    raw 是模型原始输出（JSON 字符串），仅用于排查，不直接展示给用户。

    失败：抛 TextBillError(code, message)，code 见类文档。
    """
    # ---- ① 前置校验：key 空 / text 空，都**不发请求** ----
    if not isinstance(api_key, str) or not api_key.strip():
        raise TextBillError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，语音记账暂不可用。请在 .streamlit/secrets.toml 里配置 "
            "DEEPSEEK_API_KEY 后重试。",
        )
    if not isinstance(text, str) or not text.strip():
        raise TextBillError("empty_text", "没有听到内容，请再说一遍。")
    api_key = api_key.strip()
    utterance = text.strip()
    today = _date.today()

    # 把“今天”告诉模型，它才能把“昨天/前天”换算成绝对日期。
    # 用户原话只作为待抽取文本，system prompt 已限定它不执行任何指令。
    user_content = f"今天是 {today.isoformat()}。\n用户原话：{utterance}"
    payload = {
        "model": TEXT_BILL_MODEL,
        "messages": [
            {"role": "system", "content": TEXT_BILL_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": TEXT_BILL_TEMPERATURE,
        "max_tokens": TEXT_BILL_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ---- ② 发请求 ----
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=TEXT_BILL_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("口述账单抽取超时（字数=%d）", len(utterance))
        raise TextBillError("network", "账单识别超时，请稍后重试。") from None
    except requests.ConnectionError:
        logger.warning("口述账单抽取连接失败（字数=%d）", len(utterance))
        raise TextBillError("network", "网络连接失败，请检查网络后重试。") from None
    except requests.RequestException as exc:
        logger.warning(
            "口述账单抽取请求失败（字数=%d，异常=%s）", len(utterance), type(exc).__name__
        )
        raise TextBillError("network", "账单识别请求发送失败，请稍后重试。") from None

    # ---- ③ 非 200 → http（带状态码；body 脱敏 key 后截断 80 字） ----
    if resp.status_code != 200:
        snippet = _extract_error_snippet(resp, api_key)
        tail = f" 返回内容：{snippet}" if snippet else ""
        logger.warning("口述账单抽取接口返回 HTTP %s（字数=%d）", resp.status_code, len(utterance))
        raise TextBillError(
            "http",
            f"账单识别服务返回异常（HTTP {resp.status_code}）。请稍后重试；"
            f"若为 401/403，请检查 DEEPSEEK_API_KEY 是否正确；"
            f"若为 429/余额相关，请检查账户额度。{tail}",
        )

    # ---- ④ 取出模型文本 ----
    try:
        resp_data = resp.json()
    except (ValueError, TypeError):
        logger.warning("口述账单抽取响应不是合法 JSON（字数=%d）", len(utterance))
        raise TextBillError("parse", "AI 没有返回有效结果，请重试。") from None
    try:
        content = resp_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("口述账单抽取响应结构不符合预期（字数=%d）", len(utterance))
        raise TextBillError("parse", "AI 没有返回有效结果，请重试。") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("口述账单抽取响应 content 为空（字数=%d）", len(utterance))
        raise TextBillError("parse", "AI 没有返回有效结果，请重试。")
    content = content.strip()

    # ---- ⑤ 解析 JSON：整体 → ```json 代码块 → parse ----
    data = _loads_json_object(content)
    if data is None:
        logger.warning("口述账单抽取输出不是合法 JSON（字数=%d）", len(utterance))
        raise TextBillError("parse", "AI 返回的结果无法解析成账单 JSON，请再说一遍。")

    # ---- ⑥ 字段校验（顺序：商户 → 金额 → 日期 → 类型） ----
    merchant_raw = data.get("merchant")
    merchant = merchant_raw.strip() if isinstance(merchant_raw, str) else ""
    if not merchant:
        logger.warning("口述账单抽取缺少商户（字数=%d）", len(utterance))
        raise TextBillError("invalid_bill", "没听清是哪一笔消费，请再说一遍。")

    # amount：必须是“能直接转 float”的正数（bool 不算数字，字符串不做去符号兜底）
    amount_raw = data.get("amount")
    amount = None
    if isinstance(amount_raw, bool) or amount_raw is None:
        amount = None
    elif isinstance(amount_raw, (int, float)):
        amount = float(amount_raw)
    elif isinstance(amount_raw, str):
        try:
            amount = float(amount_raw.strip())
        except ValueError:
            amount = None
    if amount is None or not math.isfinite(amount) or amount <= 0:
        logger.warning("口述账单抽取金额非法（字数=%d）", len(utterance))
        raise TextBillError(
            "invalid_bill",
            "没听清这笔花了多少钱，请说出一个大于 0 的金额后再重复一遍。",
        )
    if amount > MAX_BILL_AMOUNT:
        logger.warning("口述账单抽取金额超出上限（字数=%d）", len(utterance))
        raise TextBillError(
            "invalid_bill", "金额超出合理范围，请核对后再说一遍，或手动填写。"
        )

    paid_at_raw = data.get("paid_at")
    if not isinstance(paid_at_raw, str) or not paid_at_raw.strip():
        # 完全没提日期 → 用今天（system prompt 要求这种情况给空串）
        occurred_at = today.isoformat()
    else:
        occurred_at = _parse_text_bill_date(paid_at_raw)
        if occurred_at is None:
            # 给了日期但不是真实日历日（如 2026-02-30）→ 绝不静默改成今天
            logger.warning("口述账单抽取日期非法（字数=%d）", len(utterance))
            raise TextBillError(
                "invalid_bill",
                f"没听清是哪一天的花费（原始值：「{paid_at_raw.strip()[:20]}」），"
                "请说具体日期（例如“10 月 1 日”）后再重复一遍。",
            )

    # 其它值统一按支出（只有明确的收入类词汇才判为收入）
    bill_type = _normalize_bill_type(data.get("type")) or "支出"

    logger.info(
        "口述账单抽取成功（字数=%d，类型=%s，日期=%s）",
        len(utterance),
        bill_type,
        "显式" if isinstance(paid_at_raw, str) and paid_at_raw.strip() else "空→今天",
    )
    return {
        "merchant": merchant,
        "amount": round(float(amount), 2),
        "occurred_at": occurred_at,
        "bill_type": bill_type,
        "raw": content,
    }


# ============================================================================
# 图表洞察（DeepSeek Chat，纯文本一段话，供 app.py 图表区展示）
# ----------------------------------------------------------------------------
# 输入是 app.py 已经算好的图表统计（stats），本函数只负责"翻译成人话"，
# 不做任何统计/聚合计算；输出裁剪到 120 字，防止模型不听话写成长篇。
# · 失败统一走 ChartInsightError(code, message)，code ∈
#     no_api_key / empty_stats / network / http / empty_text；
# · 前置校验（key 空、stats 非 dict、总支出 <= 0）全部在**发请求之前**完成；
# · 日志只记 month、字数、状态码、异常类型，绝不记 api_key 或用户数据原文。
# ============================================================================

# 系统提示词必须**逐字**保持（产品口径，改一个字点评风格就跑偏）。
CHART_INSIGHT_SYSTEM_PROMPT = "你是一个懂大学生、说话犀利但不刻薄的理财分析师。请根据我提供的本月消费图表数据，用 50 字左右写一段洞察点评：必须提到本月消费最多的类别和它的金额，如果给了最高单笔就提一句，如果有上月对比就说清是变节制还是变铺张，最后给一条具体建议。不要寒暄、不要分点、不要解释，直接输出一段话。"

CHART_INSIGHT_MODEL = "deepseek-chat"
CHART_INSIGHT_TEMPERATURE = 0.7
CHART_INSIGHT_MAX_TOKENS = 200
CHART_INSIGHT_TIMEOUT = 20
CHART_INSIGHT_MAX_CHARS = 120  # 返回文本硬上限：超过截断到 120 字并补「…」


class ChartInsightError(Exception):
    """图表洞察生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  本月没有消费数据 / stats 不是 dict（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      empty_text   响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"ChartInsightError(code={self.code!r}, message={self.message!r})"


def _chart_amount(value):
    """金额宽松转 float：None / bool / 非数字 / nan / inf → None（表示"没有这项数据"）。"""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _chart_count(value) -> int:
    """笔数转 int：非法/负数 → 0（图表统计里笔数只用于文案，不值得为此报错）。"""
    amount = _chart_amount(value)
    if amount is None or amount < 0:
        return 0
    return int(amount)


def _format_chart_max_single(max_single) -> str:
    """最高单笔文案：结构不符/为空 → "无"；字段缺失时用占位词，绝不 KeyError。"""
    if not isinstance(max_single, dict) or not max_single:
        return "无"
    merchant = str(max_single.get("merchant") or "").strip() or "未知商户"
    amount = _chart_amount(max_single.get("amount"))
    amount_text = f"{amount:.2f} 元" if amount is not None else "金额未知"
    occurred = str(max_single.get("date") or "").strip() or "日期未知"
    return f"{merchant} / {amount_text} / {occurred}"


def _format_chart_change(change_pct) -> str:
    """环比文案：无数据 → "上月无数据"；否则说清是变铺张（涨）还是变节制（降）。"""
    change = _chart_amount(change_pct)
    if change is None:
        return "上月无数据"
    if change > 0:
        return f"较上月上升 {change:.1f}%（变铺张）"
    if change < 0:
        return f"较上月下降 {abs(change):.1f}%（变节制）"
    return "与上月持平（0.0%）"


def _build_chart_insight_user_content(stats: dict) -> str:
    """把 app.py 汇总的图表 stats 排成中文条目（金额一律两位小数）。"""
    month = str(stats.get("month") or "本月")
    total_expense = _chart_amount(stats.get("total_expense")) or 0.0
    tx_count = _chart_count(stats.get("tx_count"))

    # Top1 类别：缺失时写占位词，不带出任何原始数据结构。
    top_category = str(stats.get("top_category") or "").strip() or "无"
    top_amount = _chart_amount(stats.get("top_amount"))
    top_amount_text = f"{top_amount:.2f} 元" if top_amount is not None else "金额未知"

    prev_expense = _chart_amount(stats.get("prev_month_expense"))
    prev_expense_text = f"{prev_expense:.2f} 元" if prev_expense is not None else "无"

    lines = [
        "这是我的本月消费图表数据，请按系统要求写一段洞察点评：",
        f"月份：{month}",
        f"本月总支出：{total_expense:.2f} 元",
        f"消费笔数：{tx_count} 笔",
        f"Top1 类别：{top_category} / {top_amount_text}",
        f"最高单笔：{_format_chart_max_single(stats.get('max_single'))}",
        f"上月总支出：{prev_expense_text}",
        f"环比：{_format_chart_change(stats.get('change_pct'))}",
    ]
    return "\n".join(lines)


def generate_chart_insight(stats: dict, api_key: str) -> str:
    """根据图表统计生成一段约 50 字的消费洞察，返回去除首尾空白后的文本。

    失败一律抛 ChartInsightError（中文 message，可直接上页面）：
      · api_key 为空                     → 未配置提示（且**不发请求**）；
      · stats 不是 dict                  → "数据不完整"（且**不发请求**）；
      · 总支出 <= 0 / 缺失 / 非数字       → "本月还没有消费记录"（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常  → 对应中文提示；
      · 非 200                           → 带状态码的提示（不拼响应体）；
      · 响应不是 JSON / 没有有效 content → "AI 没有返回有效洞察"。
    返回文本超过 120 字时截断到 120 字并补「…」。
    日志只记月份、字数、状态码与异常类型，**绝不**记 api_key 或用户数据原文。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise ChartInsightError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，AI 图表洞察暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：stats 结构不对 / 本月没有任何支出，都不该让模型对着 0 硬编。
    if not isinstance(stats, dict):
        raise ChartInsightError(
            "empty_stats",
            "本月消费数据不完整，暂时无法生成图表洞察，请刷新后重试",
        )
    total_expense = _chart_amount(stats.get("total_expense"))
    if total_expense is None or total_expense <= 0:
        raise ChartInsightError(
            "empty_stats",
            "本月还没有消费记录，先记几笔再来查看图表洞察吧",
        )
    month = str(stats.get("month") or "本月")

    user_content = _build_chart_insight_user_content(stats)
    payload = {
        "model": CHART_INSIGHT_MODEL,
        "messages": [
            {"role": "system", "content": CHART_INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.7：点评要有点"人味"但不至于跑题；max_tokens 200 够 50 字。
        "temperature": CHART_INSIGHT_TEMPERATURE,
        "max_tokens": CHART_INSIGHT_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=CHART_INSIGHT_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("图表洞察请求超时（月份=%s）", month)
        raise ChartInsightError("network", "生成图表洞察超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("图表洞察请求连接失败（月份=%s）", month)
        raise ChartInsightError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("图表洞察请求失败（月份=%s，异常=%s）", month, type(exc).__name__)
        raise ChartInsightError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("图表洞察接口返回 HTTP %s（月份=%s）", resp.status_code, month)
        raise ChartInsightError("http", f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试")

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效洞察"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("图表洞察响应不是合法 JSON（月份=%s）", month)
        raise ChartInsightError("empty_text", "AI 没有返回有效洞察，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("图表洞察响应结构不符合预期（月份=%s）", month)
        raise ChartInsightError("empty_text", "AI 没有返回有效洞察，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("图表洞察响应 content 为空（月份=%s）", month)
        raise ChartInsightError("empty_text", "AI 没有返回有效洞察，请重试")

    content = content.strip()
    if len(content) > CHART_INSIGHT_MAX_CHARS:
        content = content[:CHART_INSIGHT_MAX_CHARS] + "…"
    logger.info("图表洞察生成成功（月份=%s，字数=%d）", month, len(content))
    return content


# ===========================================================================
# 疑似连续包月订阅 · 取消/保留建议（deepseek-chat）
# ---------------------------------------------------------------------------
# 输入是 db.detect_subscriptions() 的检测结果，输出 {商户: 建议}。
# 设计边界：
#   · 只做"文案生成"，不做检测、不写库、不碰账单表；
#   · 没有 key / 列表为空时**一个请求都不发**（省一次网络往返，也避免把空列表
#     当成"用户没有订阅"去让模型硬编）；
#   · 返回的 key 只可能是输入里出现过的商户（大小写不敏感匹配，返回输入的原写法），
#     模型编出来的陌生商户一律丢弃；缺失的条目由调用方兜底展示。
# ===========================================================================

SUBSCRIPTION_ADVICE_SYSTEM_PROMPT = "你是一个懂大学生、说话直接但不刻薄的理财助手。用户被检测出若干疑似连续包月订阅。请针对每一条，用不超过 40 个字写一句取消/保留建议：说明它已经连续扣了几个月、每月多少钱，并给一个具体动作（如去支付宝/微信关闭自动续费、或保留）。只输出 JSON 数组，格式为 [{\"merchant\": \"腾讯视频\", \"advice\": \"…\"}]，不要输出任何解释。"

SUBSCRIPTION_ADVICE_MODEL = "deepseek-chat"
SUBSCRIPTION_ADVICE_TEMPERATURE = 0.6
SUBSCRIPTION_ADVICE_MAX_TOKENS = 500
SUBSCRIPTION_ADVICE_TIMEOUT = 25
SUBSCRIPTION_ADVICE_MAX_CHARS = 60  # advice 硬上限：超过截断到 60 字并补「…」

# ```json ... ``` 代码块（也接受裸 ``` ... ```）；DOTALL 让块内换行不被吃掉。
_SUBSCRIPTION_ADVICE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class SubscriptionAdviceError(Exception):
    """订阅取消建议生成失败。

    code 取值：
      no_api_key    未配置 DeepSeek API Key（**未发请求**）
      empty_items   订阅列表为空 / 没有任何合法条目（**未发请求**）
      network       网络异常（超时 / 连接失败 / 其它请求层异常）
      http          接口返回非 200（message 只带状态码，不带响应体）
      parse         响应不是合法 JSON / 不是数组 / 没有一条合法 advice / content 为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"SubscriptionAdviceError(code={self.code!r}, message={self.message!r})"


def _advice_amount(value):
    """月均金额宽松转 float：None / bool / 非数字 / nan / inf → None（文案里写"未知"）。"""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _advice_occurrences(value):
    """已扣次数宽松转 int：None / bool / 非整数 / 负数 → None（不静默把 True 当 1 次）。"""
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    if count < 0:
        return None
    return count


def _normalize_subscription_advice_items(subscriptions):
    """把调用方传入的订阅列表收敛成 [(商户, 月均|None, 次数|None, 分类), ...]。

    · 非 list → []（由调用方翻译成 empty_items，**不发请求**）；
    · 只保留 dict 且 merchant 为非空字符串的条目：检测层已经保证字段完整，
      这里丢弃残缺条目是防止一条脏数据让整批建议都失败；
    · 同一商户（strip + casefold 后）重复出现只取第一条，避免模型对同一商户写两段；
    · monthly_amount / occurrences 转不动就留 None，文案里写"未知"，绝不因此报错；
    · **merchant 保留输入原写法**（含首尾空白）：调用方按返回的 key 去回查展示，
      这里若擅自 strip，带空格的商户会查不到建议、静默退成本地兜底。
    """
    if not isinstance(subscriptions, list):
        return []
    items = []
    seen: set[str] = set()
    for entry in subscriptions:
        if not isinstance(entry, dict):
            continue
        merchant = entry.get("merchant")
        if not isinstance(merchant, str) or not merchant.strip():
            continue
        merchant_key = merchant.strip().casefold()
        if merchant_key in seen:
            continue
        seen.add(merchant_key)
        category = entry.get("category")
        category = str(category).strip() if category is not None else ""
        items.append(
            (
                merchant,
                _advice_amount(entry.get("monthly_amount")),
                _advice_occurrences(entry.get("occurrences")),
                category or "其他",
            )
        )
    return items


def _build_subscription_advice_user_content(items) -> str:
    """逐条排成「商户 / 月均 / 已扣次数 / 分类」，一条一行，编号便于模型对齐。"""
    lines = ["请按系统要求，为下面每条疑似连续包月订阅各写一句取消/保留建议："]
    for index, (merchant, monthly_amount, occurrences, category) in enumerate(items, 1):
        monthly_text = (
            f"{monthly_amount:.2f} 元" if monthly_amount is not None else "月均金额未知"
        )
        occurrences_text = (
            f"已扣 {occurrences} 次" if occurrences is not None else "已扣次数未知"
        )
        # 展示用 strip 过的商户名：首尾空白对模型没有信息量，还可能让 JSON 对不齐。
        lines.append(
            f"{index}. {merchant.strip()} / {monthly_text} / {occurrences_text} / {category}"
        )
    return "\n".join(lines)


def _parse_subscription_advice(content: str) -> list:
    """把模型输出解析成 JSON 数组：先整体 json.loads，失败再抠 ```json 代码块。

    解析失败 / 不是 list 一律抛 SubscriptionAdviceError("parse")；
    返回 []（合法空数组）也交给调用方按 parse 处理（对非空输入而言等于没生成）。
    """
    raw = content.strip()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return parsed
    # 整体解析不出数组时：依次尝试每个 ```json 代码块，仍不行就 parse。
    for block in _SUBSCRIPTION_ADVICE_FENCE_RE.findall(raw):
        block = block.strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    raise SubscriptionAdviceError("parse", "AI 没有返回有效的订阅建议，请重试")


def _subscription_advice_entries(parsed) -> list[tuple[str, str]]:
    """从模型数组里挑出合法条目 [(商户, 建议), ...]，并做 60 字截断。

    元素不是 dict / merchant 或 advice 不是字符串 / 去空白后为空 → 跳过该元素；
    advice 超过 60 字 → 截断到 60 字并补「…」（与图表洞察的截断口径一致）。
    """
    entries: list[tuple[str, str]] = []
    for element in parsed:
        if not isinstance(element, dict):
            continue
        merchant = element.get("merchant")
        advice = element.get("advice")
        if not isinstance(merchant, str) or not isinstance(advice, str):
            continue
        merchant = merchant.strip()
        advice = advice.strip()
        if not merchant or not advice:
            continue
        if len(advice) > SUBSCRIPTION_ADVICE_MAX_CHARS:
            advice = advice[:SUBSCRIPTION_ADVICE_MAX_CHARS] + "…"
        entries.append((merchant, advice))
    return entries


def generate_subscription_advice(subscriptions: list[dict], api_key: str) -> dict[str, str]:
    """为检测出的疑似订阅逐条生成一句取消/保留建议，返回 {商户: 建议}。

    失败一律抛 SubscriptionAdviceError（中文 message，可直接上页面）：
      · api_key 为空                        → no_api_key（且**不发请求**）；
      · subscriptions 不是非空 list /      → empty_items（且**不发请求**）
        没有任何合法条目；
      · 超时 / 连接失败 / 其它请求层异常    → network；
      · 非 200                             → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / 不是数组 / content   → parse。
        为空 / 没有一条合法 advice；

    返回的 key 只可能是输入里出现过的商户（按 casefold 匹配，key 用输入原写法），
    模型编造出来的陌生商户会被丢弃；一句话都没命中时返回 {}，由调用方兜底展示。
    日志只记条数与状态码，**绝不**记 api_key、请求头或金额/商户明细。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise SubscriptionAdviceError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，订阅建议暂不可用",
        )
    api_key = api_key.strip()

    # ② 输入：没有订阅可分析时不发请求（避免让模型对着空列表硬编）。
    items = _normalize_subscription_advice_items(subscriptions)
    if not items:
        raise SubscriptionAdviceError(
            "empty_items",
            "没有检测到可分析的订阅，暂时没有建议",
        )

    user_content = _build_subscription_advice_user_content(items)
    payload = {
        "model": SUBSCRIPTION_ADVICE_MODEL,
        "messages": [
            {"role": "system", "content": SUBSCRIPTION_ADVICE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.6：建议要有点"人味"但不至于跑题；max_tokens 500 够一批条目。
        "temperature": SUBSCRIPTION_ADVICE_TEMPERATURE,
        "max_tokens": SUBSCRIPTION_ADVICE_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=SUBSCRIPTION_ADVICE_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("订阅建议请求超时（条数=%d）", len(items))
        raise SubscriptionAdviceError("network", "生成订阅建议超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("订阅建议请求连接失败（条数=%d）", len(items))
        raise SubscriptionAdviceError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("订阅建议请求失败（条数=%d，异常=%s）", len(items), type(exc).__name__)
        raise SubscriptionAdviceError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("订阅建议接口返回 HTTP %s（条数=%d）", resp.status_code, len(items))
        raise SubscriptionAdviceError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 响应结构：非 JSON / 缺 choices / content 为空 → 同一条"没有有效建议"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("订阅建议响应不是合法 JSON（条数=%d）", len(items))
        raise SubscriptionAdviceError("parse", "AI 没有返回有效的订阅建议，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("订阅建议响应结构不符合预期（条数=%d）", len(items))
        raise SubscriptionAdviceError("parse", "AI 没有返回有效的订阅建议，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("订阅建议响应 content 为空（条数=%d）", len(items))
        raise SubscriptionAdviceError("parse", "AI 没有返回有效的订阅建议，请重试")

    parsed = _parse_subscription_advice(content)
    entries = _subscription_advice_entries(parsed)
    if not entries:
        logger.warning("订阅建议响应没有合法条目（条数=%d）", len(items))
        raise SubscriptionAdviceError("parse", "AI 没有返回有效的订阅建议，请重试")

    # 只保留输入里出现过的商户：key 用**输入原写法**（含首尾空白），调用方才查得到；
    # 匹配时两侧都 strip + casefold，模型把大小写写反或多加空格也能对上。
    wanted: dict[str, str] = {}
    for merchant, *_ in items:
        wanted.setdefault(merchant.strip().casefold(), merchant)
    result: dict[str, str] = {}
    for merchant, advice in entries:
        original = wanted.get(merchant.casefold())
        if original is None or original in result:
            continue
        result[original] = advice

    # 只记请求条数 / 命中条数；商户名与金额一律不进日志。
    logger.info(
        "订阅建议生成成功（请求=%d 条，命中=%d 条）", len(items), len(result)
    )
    return result


# ===========================================================================
# 每周省钱挑战 · AI 生成（deepseek-chat）
# ---------------------------------------------------------------------------
# 输入是 app.py 组装的本月消费 stats，输出一条可展示、可领积分的挑战。
# 设计边界：
#   · 只做"文案生成"，不写库、不发积分（发/去重完全由 db.award_challenge_points 负责）；
#   · 没有 key / 本月支出 <= 0 时**一个请求都不发**（省一次网络往返，
#     也避免让模型对着 0 元硬编一个"省钱挑战"）；
#   · 模型的任何越界都收敛到可展示的值（title/detail 截断，points 夹到 10~50），
#     只有"结构不可用"才抛 parse 让 app 走本地兜底；
#   · 日志只记周次、状态码与异常类型，**绝不**记 api_key / 请求头 / 消费明细原文。
# ===========================================================================

WEEKLY_CHALLENGE_SYSTEM_PROMPT = "你是一个懂大学生、说话像游戏 NPC 的省钱教练。请根据这名用户的本月消费数据，设计一个本周可执行的省钱小挑战：必须具体、可量化、一天内能开始做（例如“少喝 3 杯奶茶，预计省下 45 元”）。只输出 JSON 对象，格式为 {\"title\": \"少喝 3 杯奶茶\", \"detail\": \"本周把奶茶控制在 2 杯以内，想喝时先喝一杯水\", \"estimated_saving\": 45, \"points\": 20}，不要输出 JSON 之外的任何文字。title 不超过 20 字，estimated_saving 为整数元，points 为 10~50 的整数。"

WEEKLY_CHALLENGE_MODEL = "deepseek-chat"
WEEKLY_CHALLENGE_TEMPERATURE = 0.8
WEEKLY_CHALLENGE_MAX_TOKENS = 300
WEEKLY_CHALLENGE_TIMEOUT = 20
WEEKLY_CHALLENGE_TITLE_MAX_CHARS = 20  # title 硬上限（超过直接截断，不补省略号）
WEEKLY_CHALLENGE_DETAIL_MAX_CHARS = 80  # detail 硬上限（同上）
WEEKLY_CHALLENGE_POINTS_MIN = 10
WEEKLY_CHALLENGE_POINTS_MAX = 50

# ```json ... ``` 代码块（也接受裸 ``` ... ```）；DOTALL 让块内换行不被吃掉。
_WEEKLY_CHALLENGE_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE
)


class ChallengeError(Exception):
    """每周省钱挑战生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / 本月支出 <= 0（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / 不是对象 / 字段缺失或不合规 / content 为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"ChallengeError(code={self.code!r}, message={self.message!r})"


def _challenge_amount(value):
    """金额宽松转 float：None / bool / 非数字 / nan / inf → None（表示"没有这项数据"）。"""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _challenge_count(value) -> int:
    """笔数转 int：非法/负数 → 0（笔数只用于提示词文案，不值得为此报错）。"""
    amount = _challenge_amount(value)
    if amount is None or amount < 0:
        return 0
    return int(amount)


def _build_weekly_challenge_user_content(stats: dict) -> str:
    """把 app.py 汇总的 stats 排成中文条目（金额两位小数，使用率一位小数）。"""
    week = str(stats.get("week") or "").strip() or "本周"
    month = str(stats.get("month") or "").strip() or "本月"
    month_expense = _challenge_amount(stats.get("month_expense")) or 0.0
    budget = _challenge_amount(stats.get("budget")) or 0.0
    budget_used_pct = _challenge_amount(stats.get("budget_used_pct"))
    if budget_used_pct is None:
        # stats 没带使用率时按支出/预算现算；预算为 0 时记 0%（不除零）。
        budget_used_pct = (month_expense / budget * 100.0) if budget > 0 else 0.0

    # Top1 类别：缺失时写占位词，不带出任何原始数据结构。
    top_category = str(stats.get("top_category") or "").strip() or "无"
    top_amount = _challenge_amount(stats.get("top_amount"))
    top_amount_text = f"{top_amount:.2f} 元" if top_amount is not None else "金额未知"
    tx_count = _challenge_count(stats.get("tx_count"))

    lines = [
        "这是我的消费数据，请按系统要求设计本周省钱挑战：",
        f"周次：{week}",
        f"月份：{month}",
        f"本月支出：{month_expense:.2f} 元 / 预算：{budget:.2f} 元（已使用 {budget_used_pct:.1f}%）",
        f"消费最多的类别：{top_category} / {top_amount_text}",
        f"本月消费笔数：{tx_count} 笔",
    ]
    return "\n".join(lines)


def _parse_weekly_challenge(content: str) -> dict:
    """把模型输出解析成挑战字段：先整体 json.loads，失败再抠 ```json 代码块。

    校验规则（任何一条不过都抛 ChallengeError("parse")）：
      · 解析结果必须是 dict；
      · title / detail 必须是非空字符串 → 去空白后分别截断到 20 / 80 字；
      · estimated_saving 必须是正数 → 转 float；
      · points 必须是数字 → int，越界夹到 [10, 50]。
    返回 {"title", "detail", "estimated_saving", "points"}，week / raw 由调用方补。
    """
    raw = content.strip()
    parsed = None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        # 整体解析不出对象时：依次尝试每个 ```json 代码块，仍不行就 parse。
        parsed = None
        for block in _WEEKLY_CHALLENGE_FENCE_RE.findall(raw):
            block = block.strip()
            if not block:
                continue
            try:
                candidate = json.loads(block)
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if not isinstance(parsed, dict):
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试")

    title = parsed.get("title")
    detail = parsed.get("detail")
    if not isinstance(title, str) or not title.strip():
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试")
    if not isinstance(detail, str) or not detail.strip():
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试")
    title = title.strip()
    detail = detail.strip()
    if len(title) > WEEKLY_CHALLENGE_TITLE_MAX_CHARS:
        title = title[:WEEKLY_CHALLENGE_TITLE_MAX_CHARS]
    if len(detail) > WEEKLY_CHALLENGE_DETAIL_MAX_CHARS:
        detail = detail[:WEEKLY_CHALLENGE_DETAIL_MAX_CHARS]

    estimated_saving = _challenge_amount(parsed.get("estimated_saving"))
    if estimated_saving is None or estimated_saving <= 0:
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试")

    points_value = parsed.get("points")
    if isinstance(points_value, bool):
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试")
    try:
        points = int(round(float(points_value)))
    except (TypeError, ValueError):
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试") from None
    points = max(WEEKLY_CHALLENGE_POINTS_MIN, min(WEEKLY_CHALLENGE_POINTS_MAX, points))

    return {
        "title": title,
        "detail": detail,
        "estimated_saving": float(estimated_saving),
        "points": points,
    }


def generate_weekly_challenge(stats: dict, api_key: str) -> dict:
    """根据本月消费 stats 生成一条本周省钱挑战。

    返回：
        {"week": str, "title": str, "detail": str,
         "estimated_saving": float, "points": int, "raw": str}
    raw 是模型原始输出（JSON 字符串），仅用于排查，不直接展示给用户。

    失败一律抛 ChallengeError（中文 message，可直接上页面）：
      · api_key 为空                     → no_api_key（且**不发请求**）；
      · stats 不是 dict                  → empty_stats（且**不发请求**）；
      · 本月支出 <= 0 / 缺失 / 非数字     → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常  → network；
      · 非 200                           → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / 结构不符 / content 为空 / 字段不合规 → parse。
    日志只记周次、状态码与异常类型，**绝不**记 api_key 或用户消费明细原文。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise ChallengeError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，AI 省钱挑战暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：stats 结构不对 / 本月没有支出，都不该让模型对着 0 硬编。
    if not isinstance(stats, dict):
        raise ChallengeError("empty_stats", "消费数据不完整，暂时无法生成省钱挑战")
    month_expense = _challenge_amount(stats.get("month_expense"))
    if month_expense is None or month_expense <= 0:
        raise ChallengeError(
            "empty_stats",
            "本月还没有消费记录，先记几笔再来生成省钱挑战吧",
        )
    week = str(stats.get("week") or "").strip()

    user_content = _build_weekly_challenge_user_content(stats)
    payload = {
        "model": WEEKLY_CHALLENGE_MODEL,
        "messages": [
            {"role": "system", "content": WEEKLY_CHALLENGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.8：挑战要有点游戏 NPC 的"花样"，但 JSON 结构由提示词锁死。
        "temperature": WEEKLY_CHALLENGE_TEMPERATURE,
        "max_tokens": WEEKLY_CHALLENGE_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=WEEKLY_CHALLENGE_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("省钱挑战请求超时（周次=%s）", week)
        raise ChallengeError("network", "生成省钱挑战超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("省钱挑战请求连接失败（周次=%s）", week)
        raise ChallengeError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "省钱挑战请求失败（周次=%s，异常=%s）", week, type(exc).__name__
        )
        raise ChallengeError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("省钱挑战接口返回 HTTP %s（周次=%s）", resp.status_code, week)
        raise ChallengeError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 响应结构：非 JSON / 缺 choices / content 为空 → 同一条"没有有效挑战"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("省钱挑战响应不是合法 JSON（周次=%s）", week)
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("省钱挑战响应结构不符合预期（周次=%s）", week)
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("省钱挑战响应 content 为空（周次=%s）", week)
        raise ChallengeError("parse", "AI 没有返回有效的省钱挑战，请重试")

    parsed = _parse_weekly_challenge(content)
    logger.info(
        "省钱挑战生成成功（周次=%s，积分=%d）", week, parsed["points"]
    )
    return {
        "week": week,
        "title": parsed["title"],
        "detail": parsed["detail"],
        "estimated_saving": parsed["estimated_saving"],
        "points": parsed["points"],
        "raw": content.strip(),
    }


# ============================================================================
# 负债压力预警（DeepSeek Chat，纯文本一句话，供 app.py 首页/负债卡片展示）
# ----------------------------------------------------------------------------
# 输入是 app.py 已经算好的 stats（由 db.forecast_repayments 的结果拼成），
# 本函数只负责「把数字翻译成一句不超过 60 字的预警」，不做任何统计/聚合；
# 返回文本硬上限 80 字（模型偶尔不听话写成小作文，必须截断）。
# · 失败统一走 DebtWarningError(code, message)，code ∈
#     no_api_key / empty_stats / network / http / parse；
# · 前置校验（key 空、预估还款额 <= 0、结余 <= 0）全部在**发请求之前**完成；
# · 日志只记月份、字数、状态码与异常类型，绝不记 api_key、请求头或金额明细。
# ============================================================================

# 系统提示词必须**逐字**保持（产品口径，改一个字预警语气就跑偏）。
DEBT_WARNING_SYSTEM_PROMPT = (
    "你是一个懂大学生、说话直接但不刻薄的财务教练。用户下个月有一笔预估还款，"
    "请写一条不超过 60 字的预警：必须包含“下个月要还 X 元”“占结余 Y%”两个信息，"
    "并给一条这个月就能执行的具体省钱建议。不要寒暄、不要分点、不要解释，直接输出一句话。"
)

DEBT_WARNING_MODEL = "deepseek-chat"
DEBT_WARNING_TEMPERATURE = 0.7
DEBT_WARNING_MAX_TOKENS = 200
DEBT_WARNING_TIMEOUT = 20
DEBT_WARNING_MAX_CHARS = 80  # 返回文本硬上限：超过截断到 80 字并补「…」


class DebtWarningError(Exception):
    """还款压力预警生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  预估还款额 <= 0 / 当前结余 <= 0 / stats 不是 dict（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"DebtWarningError(code={self.code!r}, message={self.message!r})"


def _debt_amount(value) -> float:
    """金额宽松转 float：None / bool / 非数字 / nan / inf → 0.0。

    宽容的理由：stats 由 app.py 汇总，某个键缺失只说明"这项没有数据"；
    但 next_month_amount / balance 这两个关键值另有硬校验（<= 0 直接
    DebtWarningError），不会被这里悄悄放过。
    """
    if isinstance(value, bool):
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount):
        return 0.0
    return amount


def _debt_ratio(value, next_month_amount: float, balance: float) -> float:
    """还款占结余的比例（0~1）：优先用 app 传的 ratio，缺失/非法时用还款额/结余兜底。"""
    ratio = _debt_amount(value)
    if ratio <= 0 and next_month_amount > 0 and balance > 0:
        return next_month_amount / balance
    return ratio


def _format_debt_forecast(forecast) -> str:
    """未来三个月预测排成 "2026-11 1200.00 元；…"；结构不符的条目跳过，空 → "无"。"""
    if not isinstance(forecast, (list, tuple)):
        return "无"
    items = []
    for item in forecast:
        if not isinstance(item, dict):
            continue
        month = str(item.get("month") or "").strip() or "未知月份"
        amount = _debt_amount(item.get("amount"))
        items.append(f"{month} {amount:.2f} 元")
    return "；".join(items) if items else "无"


def _debt_next_month(stats: dict) -> str:
    """下月月份：优先 stats["next_month"]；调用方只传 forecast 时取 forecast[0]["month"]。"""
    label = str(stats.get("next_month") or "").strip()
    if label:
        return label
    forecast = stats.get("forecast")
    if isinstance(forecast, (list, tuple)):
        for item in forecast:
            if isinstance(item, dict):
                month = str(item.get("month") or "").strip()
                if month:
                    return month
    return "下月"


def _debt_top_merchant(stats: dict) -> str:
    """最大负债商户：优先 stats["top_merchant"]；只传 matched_merchants（已按金额降序）时取首项。"""
    label = str(stats.get("top_merchant") or "").strip()
    if label:
        return label
    merchants = stats.get("matched_merchants")
    if isinstance(merchants, (list, tuple)):
        for item in merchants:
            if isinstance(item, dict):
                name = str(item.get("merchant") or "").strip()
                if name:
                    return name
    return "无"


def _build_debt_warning_user_content(stats: dict) -> str:
    """把 app.py 汇总的还款压力 stats 排成中文条目（金额一律两位小数）。"""
    month = str(stats.get("month") or "本月")
    next_month = _debt_next_month(stats)
    next_month_amount = _debt_amount(stats.get("next_month_amount"))
    balance = _debt_amount(stats.get("balance"))
    ratio = _debt_ratio(stats.get("ratio"), next_month_amount, balance)
    top_merchant = _debt_top_merchant(stats)

    lines = [
        "这是我的下月还款压力数据，请按系统要求写一条预警：",
        f"本月：{month}",
        f"下月：{next_month}",
        f"预估还款额：{next_month_amount:.2f} 元",
        f"当前结余：{balance:.2f} 元",
        f"预估还款占结余：{ratio * 100:.1f}%",
        f"未来三个月预测：{_format_debt_forecast(stats.get('forecast'))}",
        f"最大负债商户：{top_merchant}",
    ]
    return "\n".join(lines)


def generate_debt_warning(stats: dict, api_key: str) -> str:
    """根据还款压力统计生成一条不超过 60 字的预警，返回去除首尾空白后的文本。

    失败一律抛 DebtWarningError（中文 message，可直接上页面）：
      · api_key 为空                        → no_api_key（且**不发请求**）；
      · stats 不是 dict / 预估还款额 <= 0 / 结余 <= 0 → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常      → network；
      · 非 200                              → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空   → parse。
    返回文本超过 80 字时截断到 80 字并补「…」。
    日志只记月份、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise DebtWarningError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，还款预警暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：没有预估还款 / 结余已为 0，都不该让模型对着 0 硬编预警。
    if not isinstance(stats, dict):
        raise DebtWarningError(
            "empty_stats",
            "还款数据不完整，暂时无法生成预警，请刷新后重试",
        )
    next_month_amount = _debt_amount(stats.get("next_month_amount"))
    balance = _debt_amount(stats.get("balance"))
    if next_month_amount <= 0 or balance <= 0:
        raise DebtWarningError(
            "empty_stats",
            "下个月没有预估还款，暂时不需要还款预警",
        )
    month = str(stats.get("month") or "未知")

    user_content = _build_debt_warning_user_content(stats)
    payload = {
        "model": DEBT_WARNING_MODEL,
        "messages": [
            {"role": "system", "content": DEBT_WARNING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        # temperature 0.7：预警要直接但不刻薄；max_tokens 200 够 60 字 + 余量。
        "temperature": DEBT_WARNING_TEMPERATURE,
        "max_tokens": DEBT_WARNING_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=DEBT_WARNING_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("还款预警请求超时（月份=%s）", month)
        raise DebtWarningError("network", "生成还款预警超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("还款预警请求连接失败（月份=%s）", month)
        raise DebtWarningError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("还款预警请求失败（月份=%s，异常=%s）", month, type(exc).__name__)
        raise DebtWarningError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("还款预警接口返回 HTTP %s（月份=%s）", resp.status_code, month)
        raise DebtWarningError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效预警"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("还款预警响应不是合法 JSON（月份=%s）", month)
        raise DebtWarningError("parse", "AI 没有返回有效预警，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("还款预警响应结构不符合预期（月份=%s）", month)
        raise DebtWarningError("parse", "AI 没有返回有效预警，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("还款预警响应 content 为空（月份=%s）", month)
        raise DebtWarningError("parse", "AI 没有返回有效预警，请重试")

    content = content.strip()
    if len(content) > DEBT_WARNING_MAX_CHARS:
        content = content[:DEBT_WARNING_MAX_CHARS] + "…"
    logger.info("还款预警生成成功（月份=%s，字数=%d）", month, len(content))
    return content


# ===========================================================================
# 拿铁因子 · AI 灵魂拷问（deepseek-chat）
# ---------------------------------------------------------------------------
# 输入是 db.get_latte_factor() 的统计 + db.project_latte_compound() 的复利结果，
# 输出一段约 50 字的「灵魂拷问」文案。设计边界：
#   · 只做"文案生成"，不做统计、不做复利计算、不写库、不碰账单表；
#   · 没有 key / 日均非必要消费 <= 0 时**一个请求都不发**（省一次网络往返，
#     也避免让模型对着 0 硬编"你每天都在浪费钱"）；
#   · 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）；
#   · 日志只记统计月数、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
# ===========================================================================

# 系统提示词必须**逐字**保持（产品口径）。
LATTE_ROAST_SYSTEM_PROMPT = "你是一个犀利又懂大学生的理财教练。根据我的日均非必要消费和复利计算结果，用 50 字左右写一段“灵魂拷问”：要具体到每天多少钱、多年后本可以拥有多少钱，并给一个扎心但真实的对比（例如一台顶配 MacBook Pro）。不要寒暄、不要分点、不要解释，直接输出一段话。"

LATTE_ROAST_MODEL = "deepseek-chat"
LATTE_ROAST_TEMPERATURE = 0.8   # 拷问要"人味"、要扎心，0.8 足够发散但不跑题
LATTE_ROAST_MAX_TOKENS = 200    # 50 字 + 余量
LATTE_ROAST_TIMEOUT = 20
LATTE_ROAST_MAX_CHARS = 100     # 返回文本硬上限：超过截断到 100 字并补「…」


class LatteRoastError(Exception):
    """拿铁因子灵魂拷问生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / 日均非必要消费缺失或 <= 0（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"LatteRoastError(code={self.code!r}, message={self.message!r})"


def _latte_amount(value):
    """金额宽松转 float：None / bool / 非数字 / nan / inf → None（表示"没有这项数据"）。"""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _latte_int(value, default: int = 0) -> int:
    """年限 / 月数转 int：非法 / 负数 → default（只用于文案，不值得为此报错）。"""
    amount = _latte_amount(value)
    if amount is None or amount < 0:
        return default
    return int(amount)


def _format_latte_projection(projection) -> list[str]:
    """各年限复利结果排成缩进条目；结构不对的条目直接跳过，绝不 KeyError。"""
    lines = []
    if isinstance(projection, (list, tuple)):
        for item in projection:
            if not isinstance(item, dict):
                continue
            years = _latte_int(item.get("years"))
            value = _latte_amount(item.get("future_value"))
            if years <= 0 or value is None:
                continue
            lines.append(f"  {years} 年后：{value:.2f} 元")
    if not lines:
        lines.append("  无")
    return lines


def _build_latte_roast_user_content(stats: dict) -> str:
    """把拿铁因子统计 + 复利结果排成中文条目（金额一律两位小数、年化转百分比）。"""
    daily_avg = _latte_amount(stats.get("daily_avg")) or 0.0
    months = _latte_int(stats.get("months"))
    top_category = str(stats.get("top_category") or "").strip() or "无"

    annual_rate = _latte_amount(stats.get("annual_rate"))
    rate_text = f"{annual_rate * 100:.1f}%" if annual_rate is not None else "未提供"

    ten_year = _latte_amount(stats.get("ten_year_value"))
    ten_year_text = f"{ten_year:.2f} 元" if ten_year is not None else "未提供"

    lines = [
        "这是我的非必要消费数据和复利测算结果，请按系统要求写一段灵魂拷问：",
        f"日均非必要消费：{daily_avg:.2f} 元",
        f"统计月数：{months} 个月",
        f"Top 分类：{top_category}",
        f"年化收益率：{rate_text}",
        f"10 年后可拥有：{ten_year_text}",
        "各年限复利结果：",
        *_format_latte_projection(stats.get("projection")),
    ]
    return "\n".join(lines)


def generate_latte_roast(stats: dict, api_key: str) -> str:
    """根据拿铁因子统计 + 复利结果生成一段约 50 字的「灵魂拷问」，返回 strip 后文本。

    stats 约定（缺失字段按"无 / 未提供 / 0"处理，绝不 KeyError）：
        {"daily_avg": 15.0, "total": 1350.0, "months": 3,
         "top_category": "奶茶咖啡", "annual_rate": 0.05,
         "ten_year_value": 25000.0,
         "projection": [{"years": 1, "future_value": ...}, ...]}

    失败一律抛 LatteRoastError（中文 message，可直接上页面）：
      · api_key 为空                            → no_api_key（且**不发请求**）；
      · stats 不是 dict / daily_avg 缺失或 <= 0  → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常          → network；
      · 非 200                                  → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空        → parse。
    返回文本超过 100 字时截断到 100 字并补「…」。
    日志只记统计月数、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise LatteRoastError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，拿铁因子拷问暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：日均非必要消费 <= 0 时让模型硬编只会产出尴尬文案。
    if not isinstance(stats, dict):
        raise LatteRoastError(
            "empty_stats",
            "拿铁因子数据不完整，暂时无法生成拷问，请刷新后重试",
        )
    daily_avg = _latte_amount(stats.get("daily_avg"))
    if daily_avg is None or daily_avg <= 0:
        raise LatteRoastError(
            "empty_stats",
            "最近没有非必要消费记录，先记几笔再来看拿铁因子吧",
        )
    months = _latte_int(stats.get("months"))

    user_content = _build_latte_roast_user_content(stats)
    payload = {
        "model": LATTE_ROAST_MODEL,
        "messages": [
            {"role": "system", "content": LATTE_ROAST_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": LATTE_ROAST_TEMPERATURE,
        "max_tokens": LATTE_ROAST_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=LATTE_ROAST_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("拿铁因子拷问请求超时（统计月数=%s）", months)
        raise LatteRoastError("network", "生成拿铁因子拷问超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("拿铁因子拷问请求连接失败（统计月数=%s）", months)
        raise LatteRoastError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "拿铁因子拷问请求失败（统计月数=%s，异常=%s）", months, type(exc).__name__
        )
        raise LatteRoastError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("拿铁因子拷问接口返回 HTTP %s（统计月数=%s）", resp.status_code, months)
        raise LatteRoastError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效拷问"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("拿铁因子拷问响应不是合法 JSON（统计月数=%s）", months)
        raise LatteRoastError("parse", "AI 没有返回有效拷问，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("拿铁因子拷问响应结构不符合预期（统计月数=%s）", months)
        raise LatteRoastError("parse", "AI 没有返回有效拷问，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("拿铁因子拷问响应 content 为空（统计月数=%s）", months)
        raise LatteRoastError("parse", "AI 没有返回有效拷问，请重试")

    content = content.strip()
    if len(content) > LATTE_ROAST_MAX_CHARS:
        content = content[:LATTE_ROAST_MAX_CHARS] + "…"
    logger.info("拿铁因子拷问生成成功（统计月数=%s，字数=%d）", months, len(content))
    return content


# ============================================================================
# 财务沙盘顾问（DeepSeek Chat，把 N 个月推演结果点评成一段话）
# ----------------------------------------------------------------------------
# 与「月度复盘」的区别：输入不是原始账单，而是 db.simulate_sandbox 的推演结果，
# 所以文案重点是"第 X 个月面临破产"与"立刻能做的削减"，而不是解释钱花在哪。
#   · 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）；
#   · api_key 为空、stats 不合法时**不发请求**（先校验，再花网络往返）；
#   · 日志只记月数、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
# ============================================================================

# 系统提示词模板：{months} 由调用方按实际推演月数填充。
# 6 个月时渲染结果与旧文案逐字一致（产品口径不变），其它月数只替换两处「N 个月」。
_SANDBOX_ADVICE_SYSTEM_PROMPT_TEMPLATE = (
    "你是一个懂大学生、说话犀利但给出可执行方案的理财教练。请根据我的 {months} 个月财务沙盘推演结果，用不超过 60 字写一段总结点评：如果推演中某个月结余会跌破 0，必须点明“第 X 个月面临破产”并给 1~2 条立刻能做的削减建议；如果没有破产，就说明 {months} 个月后还能剩下多少钱并鼓励一句。不要寒暄、不要分点、不要解释，直接输出一段话。"
)

# 向后兼容：模块级常量仍是「6 个月」成品文案（逐字保持，供旧调用与断言使用）。
SANDBOX_ADVICE_SYSTEM_PROMPT = _SANDBOX_ADVICE_SYSTEM_PROMPT_TEMPLATE.format(months=6)


def _build_sandbox_advice_system_prompt(months_count) -> str:
    """按实际推演月数渲染系统提示词；月数非法（非数字 / < 1）时回落到 6。"""
    try:
        months = int(months_count)
    except (TypeError, ValueError):
        months = 6
    if months < 1:
        months = 6
    return _SANDBOX_ADVICE_SYSTEM_PROMPT_TEMPLATE.format(months=months)


SANDBOX_ADVICE_MODEL = "deepseek-chat"
SANDBOX_ADVICE_TEMPERATURE = 0.7  # 0.7：点评要犀利但不能跑题成段子
SANDBOX_ADVICE_MAX_TOKENS = 200   # 60 字 + 余量
SANDBOX_ADVICE_TIMEOUT = 20
SANDBOX_ADVICE_MAX_CHARS = 80     # 返回文本硬上限：超过截断到 80 字并补「…」


class SandboxAdviceError(Exception):
    """沙盘顾问生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / months 缺失或为空（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"SandboxAdviceError(code={self.code!r}, message={self.message!r})"


def _sandbox_advice_amount(value) -> float:
    """stats 里的金额宽松转 float：None / bool / 非数字 / nan / inf → 0.0。

    为什么宽容而不是抛错：stats 由调用方从 db.simulate_sandbox 的返回拼出来，
    某个键缺失只说明"这项没有数据"，不该让整段点评失败；结构层面的硬校验
    在 generate_sandbox_advice 里（stats 不是 dict / months 为空直接报错）。
    """
    if isinstance(value, bool):
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount):
        return 0.0
    return amount


def _sandbox_advice_months(stats: dict) -> list[dict]:
    """把 stats["months"] 收敛成 [{"month": str, "balance": float}]。

    结构不对的条目直接跳过（绝不 KeyError）；months 不是 list/tuple 时返回 []，
    由调用方统一翻译成 empty_stats。
    """
    entries: list[dict] = []
    months = stats.get("months")
    if not isinstance(months, (list, tuple)):
        return entries
    for item in months:
        if not isinstance(item, dict):
            continue
        entries.append({
            "month": str(item.get("month") or "").strip() or "未知月份",
            "balance": _sandbox_advice_amount(item.get("balance")),
        })
    return entries


def _build_sandbox_advice_user_content(stats: dict) -> str:
    """把沙盘推演结果排成中文条目（金额一律两位小数，逐月一行）。"""
    initial = _sandbox_advice_amount(stats.get("initial_balance"))
    base = _sandbox_advice_amount(stats.get("monthly_base_expense"))
    extra = _sandbox_advice_amount(stats.get("extra_expense"))
    cut = _sandbox_advice_amount(stats.get("cut_expense"))
    entries = _sandbox_advice_months(stats)

    broke_month = str(stats.get("broke_month") or "").strip()
    broke_index = stats.get("broke_index")
    if isinstance(broke_index, bool) or not isinstance(broke_index, int):
        broke_index = None
    if broke_month:
        broke_text = (
            f"{broke_month}（第 {broke_index} 个月）"
            if broke_index is not None
            else broke_month
        )
    else:
        broke_text = "无"

    raw_final = stats.get("final_balance")
    if raw_final is not None:
        final_balance = _sandbox_advice_amount(raw_final)
    elif entries:
        final_balance = entries[-1]["balance"]
    else:
        final_balance = 0.0

    balance_lines = [
        f"  {item['month']}：{item['balance']:.2f} 元" for item in entries
    ]
    if not balance_lines:
        balance_lines.append("  无")

    lines = [
        "这是我的沙盘推演结果，请按系统要求写一段总结点评：",
        f"初始结余：{initial:.2f} 元",
        f"每月基线支出：{base:.2f} 元",
        f"额外支出：{extra:.2f} 元",
        f"削减支出：{cut:.2f} 元",
        "逐月结余：",
        *balance_lines,
        f"破产月份：{broke_text}",
        f"最终结余：{final_balance:.2f} 元",
    ]
    return "\n".join(lines)


def generate_sandbox_advice(stats: dict, api_key: str) -> str:
    """根据财务沙盘推演结果生成一段不超过 60 字的总结点评（strip 后文本）。

    点评口径按 stats 里的实际月数走（1~24 个月都支持）：系统提示词中的
    「N 个月」由 _build_sandbox_advice_system_prompt 填入，6 个月时与旧文案逐字一致。

    stats 约定（缺失字段按"无 / 0"处理，绝不 KeyError）：
        {"initial_balance": 1500.0, "extra_expense": 300.0, "cut_expense": 0.0,
         "monthly_base_expense": 1500.0,
         "months": [{"month": "2027-01", "balance": -300.0}, ...],
         "broke_month": "2027-01"|None, "broke_index": 4|None,
         "final_balance": -200.0}

    失败一律抛 SandboxAdviceError（中文 message，可直接上页面）：
      · api_key 为空                        → no_api_key（且**不发请求**）；
      · stats 不是 dict / months 缺失或为空   → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常        → network；
      · 非 200                              → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空     → parse。
    返回文本超过 80 字时截断到 80 字并补「…」。
    日志只记推演月数、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise SandboxAdviceError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，沙盘顾问暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：没有逐月推演结果时，模型只能硬编"建议"，毫无意义。
    if not isinstance(stats, dict):
        raise SandboxAdviceError(
            "empty_stats",
            "沙盘推演数据不完整，暂时无法生成点评，请刷新后重试",
        )
    entries = _sandbox_advice_months(stats)
    if not entries:
        raise SandboxAdviceError(
            "empty_stats",
            "还没有沙盘推演结果，先做一次推演再来看点评吧",
        )
    months_count = len(entries)

    user_content = _build_sandbox_advice_user_content(stats)
    system_prompt = _build_sandbox_advice_system_prompt(months_count)
    payload = {
        "model": SANDBOX_ADVICE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": SANDBOX_ADVICE_TEMPERATURE,
        "max_tokens": SANDBOX_ADVICE_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=SANDBOX_ADVICE_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("沙盘顾问请求超时（推演月数=%s）", months_count)
        raise SandboxAdviceError("network", "生成沙盘点评超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("沙盘顾问请求连接失败（推演月数=%s）", months_count)
        raise SandboxAdviceError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "沙盘顾问请求失败（推演月数=%s，异常=%s）", months_count, type(exc).__name__
        )
        raise SandboxAdviceError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("沙盘顾问接口返回 HTTP %s（推演月数=%s）", resp.status_code, months_count)
        raise SandboxAdviceError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效点评"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("沙盘顾问响应不是合法 JSON（推演月数=%s）", months_count)
        raise SandboxAdviceError("parse", "AI 没有返回有效点评，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("沙盘顾问响应结构不符合预期（推演月数=%s）", months_count)
        raise SandboxAdviceError("parse", "AI 没有返回有效点评，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("沙盘顾问响应 content 为空（推演月数=%s）", months_count)
        raise SandboxAdviceError("parse", "AI 没有返回有效点评，请重试")

    content = content.strip()
    if len(content) > SANDBOX_ADVICE_MAX_CHARS:
        content = content[:SANDBOX_ADVICE_MAX_CHARS] + "…"
    logger.info("沙盘顾问生成成功（推演月数=%s，字数=%d）", months_count, len(content))
    return content


# ============================================================================
# 月度消费回忆录（DeepSeek Chat，把本月几笔「锚点消费」写成一段日记）
# ----------------------------------------------------------------------------
# 与「财务沙盘顾问」的差异：输入不是推演结果，而是 db.build_memoir_anchors 的
# 锚点（本月最开心 / 最心疼 / 最高频商户）；输出也不是一段话，而是
# {"memoir": 正文, "blessing": 下月寄语} 两段，因此比沙盘点评多一步「切分」。
# 错误分层与请求写法严格照抄沙盘顾问：
#   · api_key 为空、stats 不可写时**不发请求**（先校验，再花网络往返）；
#   · 日志只记月份、字数、状态码与异常类型，**绝不**记 api_key 或正文内容。
# ============================================================================

# 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）。
MONTHLY_MEMOIR_SYSTEM_PROMPT = (
    "你是一个懂大学生、文笔温暖又带点幽默的日记主理人。请根据我本月的这几笔关键消费，"
    "用第一人称写一段 150 字左右的“月度消费回忆录”。不要罗列数据，要写出消费背后的情绪和故事。"
    "最后给一句下个月的寄语。"
)

MONTHLY_MEMOIR_MODEL = "deepseek-chat"
MONTHLY_MEMOIR_TEMPERATURE = 0.9        # 0.9：回忆录要有画面感和情绪，不怕文风跳一点
MONTHLY_MEMOIR_MAX_TOKENS = 500         # 150 字正文 + 一句寄语 + 余量
MONTHLY_MEMOIR_TIMEOUT = 25
MONTHLY_MEMOIR_MAX_CHARS = 320          # 正文硬上限：超过截断到 320 字并补「…」
MONTHLY_MEMOIR_MAX_BLESSING_CHARS = 40  # 寄语硬上限：超过截断到 40 字并补「…」

# 「下月寄语」标记：容忍「下 月 寄 语」「寄　语」（全角空格）这类模型手滑写法。
# 冒号不放进匹配里（全角/半角/省略冒号都由后面的 strip 兜住）。
_MONTHLY_MEMOIR_BLESSING_MARKER = re.compile(r"下\s*月\s*寄\s*语")
# 正文结尾要剥掉的分隔线 / 破折号字符（"——"、"***"、"---"）。
_MONTHLY_MEMOIR_MEMOIR_TAIL_CHARS = " \t\r\n\u3000—–－-·*=~_"
# 寄语首尾要去掉的冒号 / 引号 / 空白：模型常写成「下月寄语：“少喝两杯奶茶”」。
_MONTHLY_MEMOIR_BLESSING_EDGE_CHARS = (
    " \t\r\n\u3000:：\"'“”‘’「」『』《》【】[]()（）"
)


class MonthlyMemoirError(Exception):
    """月度消费回忆录生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / 没有可写的内容（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空 / 解析不出正文

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"MonthlyMemoirError(code={self.code!r}, message={self.message!r})"


def _monthly_memoir_amount(value) -> float:
    """stats 里的金额宽松转 float：None / bool / 非数字 / nan / inf → 0.0。

    为什么宽容而不是抛错：stats 由 db.build_memoir_anchors 产出，某个键缺失只
    说明"这项没有数据"，不该让整段回忆录失败；结构层面的硬校验在
    generate_monthly_memoir 里（stats 不是 dict / 什么都没有直接报错）。
    """
    if isinstance(value, bool):
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount):
        return 0.0
    return amount


def _monthly_memoir_count(value) -> int:
    """stats 里的次数宽松转 int：None / bool / 非数字 / nan / inf / 负数 → 0。"""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _monthly_memoir_text(value) -> str:
    """stats 里的文本宽松转 str：None / bool → ""，其余 str() 后 strip。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def _monthly_memoir_anchor(stats: dict, key: str) -> dict:
    """把 stats[key] 收敛成一条锚点；不是 dict / 什么内容都没有 → None（整行不写）。

    这些字段混在一起按需取用：happiest / painful 用 merchant/amount/date/category，
    top_merchant 用 merchant/count/total，缺的字段一律给"空 / 0"，绝不 KeyError。
    """
    raw = stats.get(key)
    if not isinstance(raw, dict):
        return None
    anchor = {
        "merchant": _monthly_memoir_text(raw.get("merchant")),
        "amount": _monthly_memoir_amount(raw.get("amount")),
        "date": _monthly_memoir_text(raw.get("date")),
        "category": _monthly_memoir_text(raw.get("category")),
        "count": _monthly_memoir_count(raw.get("count")),
        "total": _monthly_memoir_amount(raw.get("total")),
    }
    if (
        not anchor["merchant"]
        and not anchor["amount"]
        and not anchor["count"]
        and not anchor["total"]
    ):
        return None
    return anchor


def _monthly_memoir_month(stats: dict) -> str:
    """取展示用月份文案：优先 month_label（"2026年10月"），退回 month（"2026-10"）。"""
    month_label = _monthly_memoir_text(stats.get("month_label"))
    if month_label:
        return month_label
    return _monthly_memoir_text(stats.get("month"))


def _monthly_memoir_expense_line(label: str, anchor: dict) -> str:
    """拼「最开心的一笔：淘宝 ¥299.00（2026-10-12，购物）」这类锚点行。"""
    merchant = anchor["merchant"] or "未知商户"
    line = f"{label}：{merchant} ¥{anchor['amount']:.2f}"
    extra = [item for item in (anchor["date"], anchor["category"]) if item]
    if extra:
        line += "（" + "，".join(extra) + "）"
    return line


def _monthly_memoir_merchant_line(label: str, anchor: dict) -> str:
    """拼「最高频商户：食堂，3 次，累计 ¥53.00」。"""
    merchant = anchor["merchant"] or "未知商户"
    line = f"{label}：{merchant}"
    if anchor["count"]:
        line += f"，{anchor['count']} 次"
    if anchor["total"]:
        line += f"，累计 ¥{anchor['total']:.2f}"
    return line


def _build_monthly_memoir_user_content(stats: dict) -> str:
    """把 stats 排成中文条目，并在末尾追加输出格式要求（格式要求不进 system prompt）。"""
    month_text = _monthly_memoir_month(stats)
    happiest = _monthly_memoir_anchor(stats, "happiest")
    painful = _monthly_memoir_anchor(stats, "painful")
    top_merchant = _monthly_memoir_anchor(stats, "top_merchant")

    expense = _monthly_memoir_amount(stats.get("expense"))
    income = _monthly_memoir_amount(stats.get("income"))
    raw_net = stats.get("net")
    net = _monthly_memoir_amount(raw_net) if raw_net is not None else income - expense
    bill_count = _monthly_memoir_count(stats.get("bill_count"))
    has_expense = bool(stats.get("has_expense"))

    lines = ["这是我本月最值得写的几笔消费，请按系统要求写一段月度消费回忆录："]
    if month_text:
        lines.append(f"本月：{month_text}")
    if happiest is not None:
        lines.append(_monthly_memoir_expense_line("最开心的一笔", happiest))
    if painful is not None:
        lines.append(_monthly_memoir_expense_line("最心疼的一笔", painful))
    if top_merchant is not None:
        lines.append(_monthly_memoir_merchant_line("最高频商户", top_merchant))
    if has_expense or expense or income or net:
        lines.append(
            f"本月支出：¥{expense:.2f}，收入：¥{income:.2f}，总结余：¥{net:.2f}"
        )
    if bill_count:
        lines.append(f"本月账单：{bill_count} 笔")
    lines.append(
        "输出格式要求：先用第一人称写回忆录正文（150 字左右），"
        "最后单独一行以「下月寄语：」开头写一句下个月的寄语；"
        "不要分点、不要解释、不要复述上面的数字。"
    )
    return "\n".join(lines)


def _monthly_memoir_last_sentence(text: str) -> tuple[str, str]:
    """没有「下月寄语」标记时的兜底：最后一句当寄语，其余当正文。"""
    body = text.strip()
    cut = max(body.rfind("。"), body.rfind("\n"))
    if cut < 0:
        # 只有一句、连句号都没有：能写出来的都是正文，不硬切（避免死板失败）。
        return body, ""
    if body[cut + 1:].strip():
        return body[:cut], body[cut + 1:].strip()
    # 结尾正好是句号 / 空行：往前找一个分隔符；寄语仍从原串切，保住句末标点。
    head = body[:cut].rstrip()
    prev = max(head.rfind("。"), head.rfind("\n"))
    if prev < 0:
        # 通篇只有一句话：整段当正文，不硬造空寄语。
        return head, ""
    return body[:prev], body[prev + 1:].strip()


def _monthly_memoir_split(content: str) -> tuple[str, str]:
    """把模型输出切成（正文, 下月寄语）。

    · 有「下月寄语」标记（兼容全角 / 半角冒号、「寄　语」中间有空格）：标记前是正文，
      标记后去掉首尾冒号引号空白就是寄语；
    · 没有标记：最后一句当寄语、其余当正文（不因为模型少写四个字就整段失败）。
    """
    text = content.strip()
    match = _MONTHLY_MEMOIR_BLESSING_MARKER.search(text)
    if match:
        memoir_raw = text[:match.start()]
        blessing_raw = text[match.end():]
    else:
        memoir_raw, blessing_raw = _monthly_memoir_last_sentence(text)

    memoir = memoir_raw.strip().rstrip(_MONTHLY_MEMOIR_MEMOIR_TAIL_CHARS).strip()
    blessing = blessing_raw.strip().strip(_MONTHLY_MEMOIR_BLESSING_EDGE_CHARS).strip()
    return memoir, blessing


def generate_monthly_memoir(stats: dict, api_key: str) -> dict:
    """根据本月锚点数据生成「月度消费回忆录」：{"memoir": 正文, "blessing": 下月寄语}。

    stats 约定（由 db.build_memoir_anchors 产出；缺失字段按"无 / 0"处理，绝不 KeyError）：
        {"month": "2026-10", "month_label": "2026年10月",
         "has_expense": True, "expense": 452.0, "income": 1500.0,
         "net": 1048.0, "bill_count": 7,
         "happiest": {"merchant": "淘宝", "amount": 299.0,
                      "date": "2026-10-12", "category": "购物"} | None,
         "painful": {...} | None,
         "top_merchant": {"merchant": "食堂", "count": 3, "total": 53.0} | None}

    失败一律抛 MonthlyMemoirError（中文 message，可直接上页面）：
      · api_key 为空                              → no_api_key（且**不发请求**）；
      · stats 不是 dict / 没有任何可写的内容        → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常              → network；
      · 非 200                                    → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空 / 切不出正文 → parse。
    正文超过 320 字截断补「…」，寄语超过 40 字截断补「…」。
    日志只记月份、字数、状态码与异常类型，**绝不**记 api_key 或正文内容。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise MonthlyMemoirError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，月度回忆录暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：一个月什么都没记时，模型只能硬编故事，毫无意义。
    if not isinstance(stats, dict):
        raise MonthlyMemoirError(
            "empty_stats",
            "本月消费数据不完整，暂时无法生成回忆录，请刷新后重试",
        )

    anchors = {
        key: _monthly_memoir_anchor(stats, key)
        for key in ("happiest", "painful", "top_merchant")
    }
    has_expense = bool(stats.get("has_expense"))
    expense = _monthly_memoir_amount(stats.get("expense"))
    income = _monthly_memoir_amount(stats.get("income"))
    bill_count = _monthly_memoir_count(stats.get("bill_count"))
    if (
        not has_expense
        and not any(anchors.values())
        and not expense
        and not income
        and not bill_count
    ):
        raise MonthlyMemoirError(
            "empty_stats",
            "本月还没有消费记录，先记几笔再来写回忆录吧",
        )

    month_label = _monthly_memoir_month(stats) or "未知月份"
    user_content = _build_monthly_memoir_user_content(stats)
    payload = {
        "model": MONTHLY_MEMOIR_MODEL,
        "messages": [
            {"role": "system", "content": MONTHLY_MEMOIR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": MONTHLY_MEMOIR_TEMPERATURE,
        "max_tokens": MONTHLY_MEMOIR_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=MONTHLY_MEMOIR_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("月度回忆录请求超时（月份=%s）", month_label)
        raise MonthlyMemoirError("network", "生成月度回忆录超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("月度回忆录请求连接失败（月份=%s）", month_label)
        raise MonthlyMemoirError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "月度回忆录请求失败（月份=%s，异常=%s）", month_label, type(exc).__name__
        )
        raise MonthlyMemoirError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("月度回忆录接口返回 HTTP %s（月份=%s）", resp.status_code, month_label)
        raise MonthlyMemoirError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效回忆录"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("月度回忆录响应不是合法 JSON（月份=%s）", month_label)
        raise MonthlyMemoirError("parse", "AI 没有返回有效回忆录，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("月度回忆录响应结构不符合预期（月份=%s）", month_label)
        raise MonthlyMemoirError("parse", "AI 没有返回有效回忆录，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("月度回忆录响应 content 为空（月份=%s）", month_label)
        raise MonthlyMemoirError("parse", "AI 没有返回有效回忆录，请重试")

    # ⑥ 切分：正文 + 下月寄语；正文为空说明模型只写了一句寄语，按解析失败处理。
    memoir, blessing = _monthly_memoir_split(content)
    if not memoir:
        logger.warning("月度回忆录响应解析不出正文（月份=%s）", month_label)
        raise MonthlyMemoirError("parse", "AI 没有返回有效回忆录，请重试")

    if len(memoir) > MONTHLY_MEMOIR_MAX_CHARS:
        memoir = memoir[:MONTHLY_MEMOIR_MAX_CHARS] + "…"
    if len(blessing) > MONTHLY_MEMOIR_MAX_BLESSING_CHARS:
        blessing = blessing[:MONTHLY_MEMOIR_MAX_BLESSING_CHARS] + "…"
    logger.info("月度回忆录生成成功（月份=%s，正文字数=%d）", month_label, len(memoir))
    return {"memoir": memoir, "blessing": blessing}


# ============================================================================
# 同龄人点评（DeepSeek Chat：把「我的雷达图 vs 全站均值」写成一段扎心短评）
# ----------------------------------------------------------------------------
# 与「沙盘顾问 / 月度回忆录」同构：先校验（没 key、没数据都不发请求）→ 组 payload
# → 发请求 → 异常分层 → 清洗模型输出（去引号、「点评：」前缀）→ 截断 → 脱敏日志。
# 差异只在入参与返回：入参是 app.py 组装好的 comparison，返回是一段纯字符串
# （不带引号、不带「点评：」前缀），可以直接贴到页面上。
# 日志只记月份、字数、状态码与异常类型，**绝不**记 api_key 或正文内容。
# ============================================================================

# 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）。
PEER_ROAST_SYSTEM_PROMPT = (
    "你是一个说话扎心的理财学长。请根据我的雷达图与全站均值的对比，写一段 50 字左右的点评。"
    "例如：“你的结余能力超过了全校 80% 的同学，但奶茶消费量居然是平均水平的 2 倍，你是想开奶茶店吗？”"
)

PEER_ROAST_MODEL = "deepseek-chat"
PEER_ROAST_TEMPERATURE = 0.8  # 0.8：要扎心、要有梗，但不能飘成段子
PEER_ROAST_MAX_TOKENS = 300   # 50 字 + 余量
PEER_ROAST_TIMEOUT = 20
PEER_ROAST_MAX_CHARS = 120    # 返回文本硬上限：超过截断到 120 字并补「…」

# 模型常见的"包装"：首尾中英文引号 / 括号，以及「点评：」前缀（全角半角冒号都认）。
_PEER_ROAST_EDGE_CHARS = " \t\r\n\u3000\"'“”‘’「」『』《》【】[]（）()"
_PEER_ROAST_PREFIX = re.compile(r"^(?:点\s*评\s*[:：]\s*)+")
# 输出格式要求不进 system prompt，跟沙盘顾问 / 回忆录一样追加在 user message 末尾。
_PEER_ROAST_OUTPUT_RULE = (
    "写一段 50 字左右的点评，一到两句话，直接说，不要分点、不要复述所有数字，"
    "不要加『点评：』前缀。"
)


class PeerRoastError(Exception):
    """同龄人点评生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  comparison 不是 dict / mine 与 peer 都没有有效得分 /
                   metrics 不是 dict（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空 / 清洗后为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"PeerRoastError(code={self.code!r}, message={self.message!r})"


def _peer_roast_amount(value) -> float:
    """comparison 里的数值宽松转 float：None / bool / 非数字 / nan / inf → 0.0。

    为什么宽容而不是抛错：comparison 由 app.py 从雷达图结果拼出来，某个键缺失
    只说明"这项没有数据"，不该让整段点评失败；结构层面的硬校验在
    generate_peer_roast 里（comparison 不是 dict / 两边都没有得分直接报错）。
    """
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _peer_roast_count(value) -> int:
    """用户数宽松转 int：None / bool / 非数字 / nan / inf / 负数 → 0。"""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _peer_roast_text(value) -> str:
    """comparison 里的文本宽松转 str：None / bool → ""，其余 str() 后 strip。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def _peer_roast_number_text(value) -> str:
    """得分 / 差值的展示口径：整数不带小数点（62），半档保留一位（62.5）。"""
    return f"{_peer_roast_amount(value):g}"


def _peer_roast_percent_text(value) -> str:
    """占比展示口径：0.13 → "13%"（先乘 100，再收敛成整数或一位小数）。"""
    return f"{round(_peer_roast_amount(value) * 100, 1):g}%"


def _peer_roast_mapping(value) -> dict[str, float]:
    """把 mine / peer / gaps 收敛成 {维度名: float}；不是 dict → {}（绝不 KeyError）。

    维度名统一走 _peer_roast_text，值统一走 _peer_roast_amount：单项数据脏了
    就按 0 处理（维度本身还在，只是这一项没数据），不影响其它维度。
    """
    result: dict[str, float] = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        name = _peer_roast_text(key)
        if not name:
            continue
        result[name] = _peer_roast_amount(item)
    return result


def _peer_roast_clean(content: str) -> str:
    """去掉首尾空白、中英文引号与「点评：」前缀（模型爱加的包装）。

    引号与前缀可能嵌套（「点评：“xxx”」），所以循环剥几轮直到稳定；
    剥完为空由调用方统一翻译成 parse。
    """
    text = content.strip()
    for _ in range(3):
        previous = text
        text = text.strip(_PEER_ROAST_EDGE_CHARS)
        text = _PEER_ROAST_PREFIX.sub("", text)
        text = text.strip(_PEER_ROAST_EDGE_CHARS)
        if text == previous:
            break
    return text


def _build_peer_roast_user_content(comparison: dict) -> str:
    """把 comparison 排成中文条目，并在末尾追加输出格式要求（格式要求不进 system prompt）。"""
    month_label = _peer_roast_text(comparison.get("month_label"))
    user_count = _peer_roast_count(comparison.get("user_count"))
    mine = _peer_roast_mapping(comparison.get("mine"))
    peer = _peer_roast_mapping(comparison.get("peer"))
    gaps = _peer_roast_mapping(comparison.get("gaps"))
    raw_metrics = comparison.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}

    # 维度顺序以「我的得分」为主，再补只出现在全站均值里的维度（缺维度天然跳过）。
    names = list(mine)
    names += [name for name in peer if name not in mine]

    if month_label and user_count:
        lines = [f"本月：{month_label}（全站 {user_count} 位同学样本）"]
    elif month_label:
        lines = [f"本月：{month_label}"]
    elif user_count:
        lines = [f"本月（全站 {user_count} 位同学样本）"]
    else:
        lines = ["本月（样本：全站均值）"]

    mine_items = [
        f"{name} {_peer_roast_number_text(mine[name])}"
        for name in names
        if name in mine
    ]
    peer_items = [
        f"{name} {_peer_roast_number_text(peer[name])}"
        for name in names
        if name in peer
    ]
    if mine_items:
        lines.append("我的得分：" + " / ".join(mine_items))
    if peer_items:
        lines.append("全站均值：" + " / ".join(peer_items))

    ahead: list[str] = []
    behind: list[str] = []
    for name in names:
        # gaps 有就用 gaps，没有（或这一维缺）就现场相减，绝不 KeyError。
        if name in gaps:
            gap = gaps[name]
        else:
            gap = mine.get(name, 0.0) - peer.get(name, 0.0)
        if gap > 0:
            ahead.append(f"{name} +{_peer_roast_number_text(gap)}")
        elif gap < 0:
            behind.append(name + " " + _peer_roast_number_text(gap))
    if ahead or behind:
        parts = []
        if ahead:
            parts.append("、".join(ahead))
        if behind:
            parts.append("、".join(behind))
        lines.append("领先/落后：" + "；".join(parts))
    else:
        lines.append("领先/落后：各项基本持平")

    segments: list[str] = []
    if "my_daily_expense" in metrics or "peer_daily_expense" in metrics:
        my_daily = _peer_roast_amount(metrics.get("my_daily_expense"))
        peer_daily = _peer_roast_amount(metrics.get("peer_daily_expense"))
        segments.append(f"日均支出我 ¥{my_daily:.2f} / 全站 ¥{peer_daily:.2f}")
    if "my_milk_tea_share" in metrics or "peer_milk_tea_share" in metrics:
        my_tea = _peer_roast_percent_text(metrics.get("my_milk_tea_share"))
        peer_tea = _peer_roast_percent_text(metrics.get("peer_milk_tea_share"))
        segments.append(f"奶茶占比 {my_tea} / {peer_tea}")
    if "my_saving_rate" in metrics or "peer_saving_rate" in metrics:
        my_rate = _peer_roast_percent_text(metrics.get("my_saving_rate"))
        peer_rate = _peer_roast_percent_text(metrics.get("peer_saving_rate"))
        segments.append(f"结余率 {my_rate} / {peer_rate}")
    if segments:
        lines.append("关键指标：" + "；".join(segments))

    lines.append("输出要求：" + _PEER_ROAST_OUTPUT_RULE)
    return "\n".join(lines)


def generate_peer_roast(comparison: dict, api_key: str) -> str:
    """根据「我的雷达图 vs 全站均值」生成一段 50 字左右的扎心点评（strip 后的纯字符串）。

    comparison 约定（由 app.py 组装；缺失维度 / 非数字按 0 或跳过，绝不 KeyError）：
        {"month_label": "2026年10月", "user_count": 2,
         "mine": {"消费克制力": 62.0, ...}, "peer": {"消费克制力": 48.0, ...},
         "gaps": {"消费克制力": 14.0, ...},
         "metrics": {"my_daily_expense": 15.1, "peer_daily_expense": 18.9,
                     "my_milk_tea_share": 0.13, "peer_milk_tea_share": 0.05,
                     "my_saving_rate": 0.35, "peer_saving_rate": 0.18,
                     "my_expense": 452.0, "peer_expense": 379.2}}

    失败一律抛 PeerRoastError（中文 message，可直接上页面）：
      · api_key 为空                                    → no_api_key（且**不发请求**）；
      · comparison 不是 dict / mine 与 peer 都没有得分     → empty_stats（且**不发请求**）；
      · metrics 不是 dict                               → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常                    → network；
      · 非 200                                         → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空 / 清洗后为空     → parse。
    返回前去掉首尾中英文引号与「点评：」前缀；超过 120 字截断补「…」。
    日志只记月份、字数、状态码与异常类型，**绝不**记 api_key 或正文内容。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise PeerRoastError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，同龄人点评暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：两边都没有得分 / metrics 结构不对时，模型只能硬编，毫无意义。
    if not isinstance(comparison, dict):
        raise PeerRoastError(
            "empty_stats",
            "雷达图对比数据不完整，暂时无法生成点评，请刷新后重试",
        )
    mine = _peer_roast_mapping(comparison.get("mine"))
    peer = _peer_roast_mapping(comparison.get("peer"))
    if not mine and not peer:
        raise PeerRoastError(
            "empty_stats",
            "还没有可对比的雷达图数据，先记几笔账单再来看点评吧",
        )
    if not isinstance(comparison.get("metrics"), dict):
        raise PeerRoastError(
            "empty_stats",
            "雷达图对比数据不完整，暂时无法生成点评，请刷新后重试",
        )

    month_label = _peer_roast_text(comparison.get("month_label")) or "未知月份"
    user_content = _build_peer_roast_user_content(comparison)
    payload = {
        "model": PEER_ROAST_MODEL,
        "messages": [
            {"role": "system", "content": PEER_ROAST_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": PEER_ROAST_TEMPERATURE,
        "max_tokens": PEER_ROAST_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=PEER_ROAST_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("同龄人点评请求超时（月份=%s）", month_label)
        raise PeerRoastError("network", "生成同龄人点评超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("同龄人点评请求连接失败（月份=%s）", month_label)
        raise PeerRoastError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "同龄人点评请求失败（月份=%s，异常=%s）", month_label, type(exc).__name__
        )
        raise PeerRoastError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("同龄人点评接口返回 HTTP %s（月份=%s）", resp.status_code, month_label)
        raise PeerRoastError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效点评"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("同龄人点评响应不是合法 JSON（月份=%s）", month_label)
        raise PeerRoastError("parse", "AI 没有返回有效点评，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("同龄人点评响应结构不符合预期（月份=%s）", month_label)
        raise PeerRoastError("parse", "AI 没有返回有效点评，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("同龄人点评响应 content 为空（月份=%s）", month_label)
        raise PeerRoastError("parse", "AI 没有返回有效点评，请重试")

    # ⑥ 清洗：去首尾引号与「点评：」前缀；洗空了说明模型只回了包装，按解析失败处理。
    content = _peer_roast_clean(content)
    if not content:
        logger.warning("同龄人点评响应解析不出正文（月份=%s）", month_label)
        raise PeerRoastError("parse", "AI 没有返回有效点评，请重试")

    if len(content) > PEER_ROAST_MAX_CHARS:
        content = content[:PEER_ROAST_MAX_CHARS] + "…"
    logger.info("同龄人点评生成成功（月份=%s，字数=%d）", month_label, len(content))
    return content


# ============================================================================
# AI 财务顾问 · 多轮访谈（DeepSeek Chat，一次调用推进一轮：提问或收尾）
# ----------------------------------------------------------------------------
# 与「沙盘顾问 / 月度回忆录 / 同龄人点评」同构：先校验（没 key、没对话也没
# 本月数据都不发请求）→ 组 payload → 发请求 → 异常分层 → 解析/归一模型输出
# → 脱敏日志。差异在于本函数是**有状态的访谈**：
#   · history 是已完成的问答（按时间顺序），每次只推进一轮：问下一个问题，
#     或在回答够多时收尾出一份「个人财务改善建议书」；
#   · 收尾有两条硬保护：回答不足 3 条时模型想收尾也一律改回提问；回答满 4 条时
#     模型还想问也一律强制收尾（用内置兜底方案，不浪费这一轮回答）；
#   · 轮次规则、输出格式、JSON schema 全部写在 user message 里，system prompt
#     逐字固定（产品口径，单测用独立字面量断言）；
#   · 日志只记轮次 / done / 字数 / 状态码 / 异常类型，**绝不**记 api_key，
#     也**绝不**记对话正文或响应体。
# ============================================================================

ADVISOR_SYSTEM_PROMPT = (
    "你是一个温柔、专业、有耐心的大学生财务顾问。你需要通过向我提问，了解我的消费习惯、近期压力、未来目标。"
    "每次只问一个问题，引导我说出真实的财务状况。不要一次问太多。"
)
ADVISOR_MODEL = "deepseek-chat"
ADVISOR_TEMPERATURE = 0.7
ADVISOR_MAX_TOKENS = 800
ADVISOR_TIMEOUT = 30
ADVISOR_MAX_MESSAGE_CHARS = 220      # 单条 message 硬上限
ADVISOR_MIN_ANSWERS = 3              # 少于 3 条回答一律不许收尾
ADVISOR_MAX_QUESTIONS = 4            # 满 4 条回答必须收尾

_ADVISOR_DEFAULT_TITLE = "个人财务改善建议书"
_ADVISOR_DEFAULT_GOAL_NAME = "我的存钱目标"
_ADVISOR_DEFAULT_GOAL_AMOUNT = 500.0
_ADVISOR_TITLE_MAX_CHARS = 40
_ADVISOR_SUMMARY_MAX_CHARS = 200
_ADVISOR_DIAGNOSIS_MAX_ITEMS = 5
_ADVISOR_DIAGNOSIS_MAX_CHARS = 80
_ADVISOR_ACTION_MAX_ITEMS = 4
_ADVISOR_ACTION_TITLE_MAX_CHARS = 30
_ADVISOR_ACTION_DETAIL_MAX_CHARS = 120
_ADVISOR_GOAL_NAME_MAX_CHARS = 30

# 硬性收尾保护用的兜底问题：按已获得的回答条数取（顺序固定，单测依赖下标）。
_ADVISOR_FALLBACK_QUESTIONS = (
    "你每个月的生活费大概是多少？",
    "最近有没有哪笔支出让你压力比较大？",
    "你存钱主要是为了什么呢？",
    "奶茶和外卖一周大概几次？",
)

# 强制收尾时固定给出的收尾语（不随模型输出变化）。
_ADVISOR_FORCED_FINISH_MESSAGE = "我们聊得差不多了，我把建议整理成一页纸给你。"

# JSON schema 原样贴进 user message（不进 system prompt），两种返回形态各给一个例子。
_ADVISOR_JSON_SCHEMA = """问问题时只输出：
{"done": false, "message": "你每个月的生活费大概是多少？"}

收尾时输出：
{
  "done": true,
  "message": "谢谢你说的这些，我大致看清楚了……",
  "proposal": {
    "title": "个人财务改善建议书",
    "summary": "50~80 字的总结",
    "diagnosis": ["诊断 1", "诊断 2"],
    "actions": [{"title": "行动标题", "detail": "具体怎么做"}],
    "goal": {"name": "存钱目标的名字", "target_amount": 500.0, "deadline": "2027-03"}
  }
}"""


class AdvisorError(Exception):
    """AI 财务顾问访谈失败。

    code 取值：
      no_api_key     未配置 DeepSeek API Key（**未发请求**）
      empty_history  history 不是 list / history 为空且 context 也为空（**未发请求**）
      network        网络异常（超时 / 连接失败 / 其它请求层异常）
      http           接口返回非 200（message 只带状态码，不带响应体）
      parse          响应不是合法 JSON / message 缺失或空白 / 模型输出无法解析

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"AdvisorError(code={self.code!r}, message={self.message!r})"


def _advisor_text(value, default: str = "", limit: int = 0) -> str:
    """顾问相关文本宽松归一：None / bool → default；strip 后为空 → default。

    limit > 0 且超长时截断到 limit 字并补「…」（与 message 的 220+「…」口径一致）。
    """
    if isinstance(value, str):
        text = value.strip()
    elif value is None or isinstance(value, bool):
        text = ""
    else:
        try:
            text = str(value).strip()
        except (TypeError, ValueError):
            text = ""
    if not text:
        text = default
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    return text


def _advisor_amount(value, default: float = 0.0) -> float:
    """顾问相关金额宽松转 float：None / bool / 非数字 / nan / inf → default。"""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _advisor_list(value) -> list:
    """宽松取列表：不是 list / tuple → []（元素原样返回，由调用方逐条归一）。"""
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _advisor_history(history):
    """把 history 过滤成 [{"role": "assistant"|"user", "content": str}, ...]。

    · 不是 list → None（由调用方翻译成 empty_history，**不发请求**）；
    · 非 dict 条目 / 其它 role / content 不是非空字符串 → 直接忽略。
    注意：**合法的空列表**（访谈第一轮）返回 []，不是 None。
    """
    if not isinstance(history, list):
        return None
    entries = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("assistant", "user"):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        entries.append({"role": role, "content": content.strip()})
    return entries


def _advisor_context_parts(context: dict) -> dict:
    """把 context 收敛成 {"month_label", "month_expense", "balance", "save_target"}。

    字段可能缺、类型可能脏：文本统一 strip，金额非数字一律按 0（绝不 KeyError）。
    """
    data = context if isinstance(context, dict) else {}
    return {
        "month_label": _advisor_text(data.get("month_label")),
        "month_expense": _advisor_amount(data.get("month_expense")),
        "balance": _advisor_amount(data.get("balance")),
        "save_target": _advisor_amount(data.get("save_target")),
    }


def _advisor_context_has_data(parts: dict) -> bool:
    """context 是否提供了任何可用信息（月份 / 支出 / 结余 / 目标任一非空）。"""
    return bool(
        parts.get("month_label")
        or parts.get("month_expense")
        or parts.get("balance")
        or parts.get("save_target")
    )


def _advisor_fallback_question(answers: int) -> str:
    """按已回答条数取内置兜底问题；越界时取最后一个（绝不 IndexError）。"""
    index = answers if 0 <= answers < len(_ADVISOR_FALLBACK_QUESTIONS) else len(_ADVISOR_FALLBACK_QUESTIONS) - 1
    return _ADVISOR_FALLBACK_QUESTIONS[index]


def _advisor_deadline(value) -> str:
    """归一 goal.deadline：接受 YYYY-MM 或 YYYY-MM-DD，其它一律空串（绝不编日期）。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m")
        except ValueError:
            return ""
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return ""
        return text
    return ""


def _advisor_parse_json(content: str) -> dict:
    """解析模型输出：去 ```json 围栏 → 取第一个 { 到最后一个 } → json.loads。

    任何一步失败 / 结果不是非空 dict → AdvisorError("parse", ...)。
    """
    text = content.strip() if isinstance(content, str) else ""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced and fenced.group(1).strip():
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise AdvisorError("parse", "AI 没有返回有效的顾问回复，请重试") from None
    if not isinstance(data, dict) or not data:
        raise AdvisorError("parse", "AI 没有返回有效的顾问回复，请重试")
    return data


def _advisor_normalize_proposal(raw, message: str) -> dict:
    """把模型给的 proposal 宽容归一成约定结构（字段缺失一律给默认值）。

    · title：默认「个人财务改善建议书」，≤40 字；
    · summary：默认取本轮 message，≤200 字；
    · diagnosis：list[str]，过滤空串，最多 5 条，每条 ≤80 字；
    · actions：list[dict]，title ≤30 字 / detail ≤120 字，最多 4 条，非法条目丢弃；
    · goal：name ≤30 字（默认「我的存钱目标」）、target_amount > 0（默认 500.0）、
      deadline 只接受 YYYY-MM / YYYY-MM-DD，其它为空串。
    """
    data = raw if isinstance(raw, dict) else {}

    title = _advisor_text(
        data.get("title"), _ADVISOR_DEFAULT_TITLE, _ADVISOR_TITLE_MAX_CHARS
    )
    summary = _advisor_text(
        data.get("summary"), message, _ADVISOR_SUMMARY_MAX_CHARS
    )

    diagnosis = []
    for item in _advisor_list(data.get("diagnosis")):
        text = _advisor_text(item, "", _ADVISOR_DIAGNOSIS_MAX_CHARS)
        if not text:
            continue
        diagnosis.append(text)
        if len(diagnosis) >= _ADVISOR_DIAGNOSIS_MAX_ITEMS:
            break

    actions = []
    for item in _advisor_list(data.get("actions")):
        if not isinstance(item, dict):
            continue
        action_title = _advisor_text(
            item.get("title"), "", _ADVISOR_ACTION_TITLE_MAX_CHARS
        )
        action_detail = _advisor_text(
            item.get("detail"), "", _ADVISOR_ACTION_DETAIL_MAX_CHARS
        )
        if not action_title and not action_detail:
            continue
        actions.append({"title": action_title, "detail": action_detail})
        if len(actions) >= _ADVISOR_ACTION_MAX_ITEMS:
            break

    goal_raw = data.get("goal")
    goal = goal_raw if isinstance(goal_raw, dict) else {}
    goal_name = _advisor_text(
        goal.get("name"), _ADVISOR_DEFAULT_GOAL_NAME, _ADVISOR_GOAL_NAME_MAX_CHARS
    )
    target_amount = _advisor_amount(goal.get("target_amount"), 0.0)
    if target_amount <= 0:
        target_amount = _ADVISOR_DEFAULT_GOAL_AMOUNT

    return {
        "title": title,
        "summary": summary,
        "diagnosis": diagnosis,
        "actions": actions,
        "goal": {
            "name": goal_name,
            "target_amount": float(target_amount),
            "deadline": _advisor_deadline(goal.get("deadline")),
        },
    }


def _advisor_fallback_proposal(parts: dict, entries: list[dict]) -> dict:
    """强制收尾用的内置方案：按 context / history 拼一份 2 诊断 + 2 行动的建议书。

    绝不调用模型（模型这一轮已经跑偏），所以每个字段都必须自己补齐，
    保证 app.py 拿到的结构与正常收尾完全一致。
    """
    month_label = parts.get("month_label") or "这段时间"
    month_expense = _advisor_amount(parts.get("month_expense"))
    balance = _advisor_amount(parts.get("balance"))
    save_target = _advisor_amount(parts.get("save_target"))
    target_amount = save_target if save_target > 0 else _ADVISOR_DEFAULT_GOAL_AMOUNT

    # 已完成的回答条数用于收尾文案（history 里没有 user 回答时退回通用开场）。
    answers = sum(1 for item in entries if item["role"] == "user")
    opening = (
        f"我们聊了 {answers} 轮，谢谢你愿意把这些讲给我听。"
        if answers
        else "谢谢你愿意把这些讲给我听。"
    )

    if month_expense > 0:
        summary = (
            f"{opening}{month_label}你一共花了 {month_expense:.2f} 元，"
            f"现在还剩 {balance:.2f} 元。别急着怪自己，先把节奏放慢，"
            "把花得最多的地方收一收，再给存钱留一个固定的位置。"
        )
        diagnosis = [
            f"这个月已经花掉 {month_expense:.2f} 元，钱多半是在日常小笔消费里悄悄流走的。"
        ]
        action_one = {
            "title": "给日常开销定个上限",
            "detail": (
                f"把每月支出控制在 {round(month_expense * 0.9, 2):.2f} 元左右"
                "（比现在少一成），每天睡前花一分钟对一下账。"
            ),
        }
    else:
        summary = (
            f"{opening}这阵子记账还不多，"
            "但先把方向聊清楚，比记多少笔都重要。我们先把目标定下来，"
            "再从小处开始调整，你会慢慢看到变化的。"
        )
        diagnosis = ["目前账单还不多，先把每一笔记下来，才看得清钱到底去了哪里。"]
        action_one = {
            "title": "先把每一笔记下来",
            "detail": "每天睡前花一分钟记下当天花销，一周回看一次，找出最容易被忽略的那一类。",
        }

    if save_target > 0:
        diagnosis.append(
            f"你给自己定的存钱目标是 {save_target:.2f} 元，按现在的花法还要再收紧一点才够。"
        )
        action_two = {
            "title": f"每月先存下 {save_target:.2f} 元",
            "detail": "生活费到账当天就把这笔钱转到单独的储蓄账户，剩下的再拿来花。",
        }
    else:
        diagnosis.append("你还没有写下明确的存钱目标，先定一个数字，花钱时会更容易停一下。")
        action_two = {
            "title": "先设一个看得见的存钱目标",
            "detail": "在 App 里定一个具体金额和日期（比如 3 个月存 500 元），想乱花时先看一眼它。",
        }

    return {
        "title": _ADVISOR_DEFAULT_TITLE,
        "summary": summary,
        "diagnosis": diagnosis,
        "actions": [action_one, action_two],
        "goal": {
            "name": _ADVISOR_DEFAULT_GOAL_NAME,
            "target_amount": float(target_amount),
            "deadline": "",
        },
    }


def _build_advisor_user_content(entries: list[dict], parts: dict, answers: int) -> str:
    """把本轮上下文 + 已完成问答 + 轮次规则 + JSON schema 拼成 user message。

    所有“怎么做”的指令都放这里，system prompt 保持逐字不变。
    """
    month_label = parts.get("month_label") or ""
    month_expense = _advisor_amount(parts.get("month_expense"))
    balance = _advisor_amount(parts.get("balance"))
    save_target = _advisor_amount(parts.get("save_target"))

    lines = ["这是我的本月财务情况和我们已经聊过的内容，请按系统要求继续访谈："]

    context_lines = []
    if month_label:
        context_lines.append(f"月份：{month_label}")
    if month_expense:
        context_lines.append(f"本月支出：{month_expense:.2f} 元")
    if balance:
        context_lines.append(f"当前结余：{balance:.2f} 元")
    if save_target:
        context_lines.append(f"目标存款：{save_target:.2f} 元")
    if context_lines:
        lines.extend(context_lines)
    else:
        lines.append("本月数据：暂无")

    lines.append("已完成的问答：")
    if entries:
        turn = 0
        for item in entries:
            if item["role"] == "assistant":
                turn += 1
                lines.append(f"{turn}. 顾问：{item['content']}")
            elif turn:
                lines.append(f"   我：{item['content']}")
            else:
                turn = 1
                lines.append(f"{turn}. 我：{item['content']}")
    else:
        lines.append("（还没有开始问答，请你先提出第一个问题。）")

    lines.append("轮次规则：")
    lines.append(
        f"已获得 {answers} 条回答；n < 3 时必须继续提问，不得收尾；"
        "n >= 3 且信息足够时输出方案；n >= 4 时必须输出方案。"
    )
    if answers < ADVISOR_MIN_ANSWERS:
        lines.append(
            f"当前判断：已获得 {answers} 条回答，还不到 {ADVISOR_MIN_ANSWERS} 条，"
            "必须继续提问，不得收尾。"
        )
    elif answers < ADVISOR_MAX_QUESTIONS:
        lines.append(
            f"当前判断：已获得 {answers} 条回答，信息足够就用方案收尾；"
            "还差一点可以再问一个问题。"
        )
    else:
        lines.append(
            f"当前判断：已获得 {answers} 条回答，已达到 {ADVISOR_MAX_QUESTIONS} 条，"
            "必须输出方案，不要再提问。"
        )
    lines.append(
        "其它要求：如果我的回答胡言乱语 / 答非所问，请用一句温柔的话把我拉回主题，"
        "并把问题换个问法再问一次。"
    )
    lines.append(
        "只输出一个 JSON 对象，不要 markdown 代码块、不要多余解释。"
        "问问题时 done=false；收尾时 done=true 且必须带 proposal。"
    )
    lines.append("JSON 格式（字段名不可改）：")
    lines.append(_ADVISOR_JSON_SCHEMA)
    return "\n".join(lines)


def advisor_turn(history: list[dict], context: dict, api_key: str) -> dict:
    """AI 财务顾问访谈的一轮：要么问下一个问题，要么收尾出建议书。

    参数：
        history: [{"role": "assistant"|"user", "content": str}, ...]（按时间顺序；
                 其它 role / 非 dict 条目忽略；**空列表合法** —— 第一轮由顾问开场）
        context: {"month_label": "2026年10月", "month_expense": 452.0,
                  "balance": 1048.0, "save_target": 200.0}（字段可缺，金额非数字按 0）
        api_key: DeepSeek API Key（空/空白 → no_api_key，**不发请求**）

    返回（严格二选一）：
        {"done": False, "message": str}
        {"done": True, "message": str, "proposal": {...}}

    失败一律抛 AdvisorError（中文 message，可直接上页面）：
      · api_key 为空                                  → no_api_key（且**不发请求**）；
      · history 不是 list / history 为空且 context 为空  → empty_history（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常                    → network；
      · 非 200                                         → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / message 缺失或空白                → parse。
    硬保护：
      · 模型 done=true 但回答不足 3 条 → 忽略 done，改为内置兜底提问（不报错）；
      · 模型 done=false 但回答已满 4 条 → 强制收尾，用内置兜底方案。
    message 超过 220 字截断补「…」；proposal 各字段宽容归一。
    日志只记轮次 / done / 字数 / 状态码与异常类型，**绝不**记 api_key 或对话正文。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        logger.warning("顾问访谈未配置 DEEPSEEK_API_KEY（未发请求）")
        raise AdvisorError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，AI 财务顾问暂不可用",
        )
    api_key = api_key.strip()

    # ② 对话前置校验：history 必须是 list；空 history 只在 context 也空时才算非法
    #    （访谈第一轮 history=[] 是合法的，顾问要主动问第一个问题）。
    entries = _advisor_history(history)
    if entries is None:
        logger.warning("顾问访谈的对话记录不是列表（未发请求）")
        raise AdvisorError(
            "empty_history",
            "顾问访谈的对话记录格式不正确，请刷新页面后重试",
        )
    parts = _advisor_context_parts(context)
    if not entries and not _advisor_context_has_data(parts):
        logger.warning("顾问访谈缺少对话与本月数据（未发请求）")
        raise AdvisorError(
            "empty_history",
            "还没有对话内容，也没有本月数据，暂时无法开始财务访谈",
        )

    answers = sum(1 for item in entries if item["role"] == "user")

    user_content = _build_advisor_user_content(entries, parts, answers)
    payload = {
        "model": ADVISOR_MODEL,
        "messages": [
            {"role": "system", "content": ADVISOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": ADVISOR_TEMPERATURE,
        "max_tokens": ADVISOR_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=ADVISOR_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("顾问访谈请求超时（轮次=%s）", answers)
        raise AdvisorError("network", "生成顾问回复超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("顾问访谈请求连接失败（轮次=%s）", answers)
        raise AdvisorError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "顾问访谈请求失败（轮次=%s，异常=%s）", answers, type(exc).__name__
        )
        raise AdvisorError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("顾问访谈接口返回 HTTP %s（轮次=%s）", resp.status_code, answers)
        raise AdvisorError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → parse。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("顾问访谈响应不是合法 JSON（轮次=%s）", answers)
        raise AdvisorError("parse", "AI 没有返回有效的顾问回复，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("顾问访谈响应结构不符合预期（轮次=%s）", answers)
        raise AdvisorError("parse", "AI 没有返回有效的顾问回复，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("顾问访谈响应 content 为空（轮次=%s）", answers)
        raise AdvisorError("parse", "AI 没有返回有效的顾问回复，请重试")

    # ⑥ 解析模型输出（去围栏 / 取 {…}），message 严格要求非空字符串。
    parsed = _advisor_parse_json(content.strip())
    raw_message = parsed.get("message")
    if not isinstance(raw_message, str) or not raw_message.strip():
        logger.warning("顾问访谈响应缺少有效 message（轮次=%s）", answers)
        raise AdvisorError("parse", "AI 没有返回有效的顾问回复，请重试")
    message = raw_message.strip()
    if len(message) > ADVISOR_MAX_MESSAGE_CHARS:
        message = message[:ADVISOR_MAX_MESSAGE_CHARS] + "…"
    done = _coerce_bool(parsed.get("done"), default=False)

    # ⑦ 硬性收尾保护一：回答不足 3 条时，模型的 done=true 一律忽略，改回提问。
    #    用内置问题按 answers 取，不报错、不浪费这一轮回答。
    if done and answers < ADVISOR_MIN_ANSWERS:
        question = _advisor_fallback_question(answers)
        logger.info(
            "顾问访谈生成成功（轮次=%d，done=%s，字数=%d）",
            answers,
            "否",
            len(question),
        )
        return {"done": False, "message": question}

    # ⑧ 硬性收尾保护二：回答已满 4 条时，模型还想提问也强制收尾（内置兜底方案）。
    if not done and answers >= ADVISOR_MAX_QUESTIONS:
        proposal = _advisor_fallback_proposal(parts, entries)
        logger.info(
            "顾问访谈生成成功（轮次=%d，done=%s，字数=%d）",
            answers,
            "是",
            len(_ADVISOR_FORCED_FINISH_MESSAGE),
        )
        return {
            "done": True,
            "message": _ADVISOR_FORCED_FINISH_MESSAGE,
            "proposal": proposal,
        }

    # ⑨ 正常提问。
    if not done:
        logger.info(
            "顾问访谈生成成功（轮次=%d，done=%s，字数=%d）",
            answers,
            "否",
            len(message),
        )
        return {"done": False, "message": message}

    # ⑩ 正常收尾：proposal 全部字段宽容归一。
    proposal = _advisor_normalize_proposal(parsed.get("proposal"), message)
    logger.info(
        "顾问访谈生成成功（轮次=%d，done=%s，字数=%d）",
        answers,
        "是",
        len(message),
    )
    return {"done": True, "message": message, "proposal": proposal}


# ===========================================================================
# 情绪消费教练（DeepSeek Chat：把「情绪标签统计」写成一句有心理学洞察的短句）
# ---------------------------------------------------------------------------
# 与「拿铁因子 / 同龄人」同构：先校验（没 key、没数据都不发请求）→ 组 payload
# → 发请求 → 异常分层 → strip + 截断 → 脱敏日志。
# 差异只在入参与返回：入参是调用方按约定拼好的情绪统计 stats，返回一段纯字符串，
# 可直接贴到页面上。
#   · 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）；
#   · 日志只记模型名、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
# ===========================================================================

# 系统提示词逐字固定（产品口径）。
MOOD_INSIGHT_SYSTEM_PROMPT = (
    "你是一个既懂心理学又懂理财的教练。请根据我的情绪消费分析，给我一句 50 字左右的洞察。"
    "比如：‘我发现你每到考试周（焦虑/疲惫），外卖和奶茶支出就会飙升。"
    "下次压力大时，试试去操场跑两圈代替一杯奶茶？’"
)

MOOD_INSIGHT_MODEL = "deepseek-chat"
MOOD_INSIGHT_TEMPERATURE = 0.7   # 0.7：要洞察、要温度，但不能飘成鸡汤
MOOD_INSIGHT_MAX_TOKENS = 200    # 50 字 + 余量
MOOD_INSIGHT_TIMEOUT = 20
MOOD_INSIGHT_MAX_CHARS = 140     # 返回文本硬上限：超过截断到 140 字并补「…」


class MoodInsightError(Exception):
    """情绪消费教练洞察生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / moods 为空或无有效条目（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"MoodInsightError(code={self.code!r}, message={self.message!r})"


def _mood_insight_amount(value) -> float:
    """stats 里的金额宽松转 float：None / bool / 非数字 / nan / inf → 0.0。"""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _mood_insight_int(value, default: int = 0) -> int:
    """笔数 / 月数转 int：非法 / 负数 → default。"""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0:
        return default
    return int(number)


def _mood_insight_text(value) -> str:
    """mood / emoji 宽松转 str：None / bool → ""，其余 str() 后 strip。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def _mood_insight_entries(stats: dict) -> list[dict]:
    """把 stats["moods"] 收敛成有效条目；不是 list 或条目结构不对的项直接跳过。"""
    moods = stats.get("moods")
    if not isinstance(moods, (list, tuple)):
        return []
    entries = []
    for item in moods:
        if not isinstance(item, dict):
            continue
        if not _mood_insight_text(item.get("mood")):
            continue
        entries.append(item)
    return entries


def _mood_insight_ratio_text(value) -> str:
    """倍数展示口径：2.0 → "2.0 倍"。"""
    ratio = _mood_insight_amount(value)
    if ratio <= 0:
        return "0.0 倍"
    return f"{ratio:.1f} 倍"


def _mood_insight_share_text(value) -> str:
    """占比展示口径：0.6 → "60.0%"（入参是 0~1 的比例）。"""
    share = _mood_insight_amount(value)
    if share <= 0:
        return "0.0%"
    return f"{share * 100:.1f}%"


def _build_mood_insight_user_content(stats: dict, entries: list[dict]) -> str:
    """把情绪消费统计排成中文条目（金额两位小数、倍数一位小数）。"""
    months = _mood_insight_int(stats.get("months"))
    total_expense = _mood_insight_amount(stats.get("total_expense"))
    tagged_expense = _mood_insight_amount(stats.get("tagged_expense"))
    baseline_avg = _mood_insight_amount(stats.get("baseline_avg"))

    lines = [
        "这是我的情绪消费分析，请按系统要求写一句洞察：",
        f"统计月数：{months} 个月",
        f"支出总额：{total_expense:.2f} 元",
        f"带情绪标签的支出：{tagged_expense:.2f} 元",
        f"基线平均单笔：{baseline_avg:.2f} 元",
        "各情绪消费明细：",
    ]
    for item in entries:
        mood = _mood_insight_text(item.get("mood"))
        emoji = _mood_insight_text(item.get("emoji"))
        label = f"{mood} {emoji}".strip() if emoji else mood
        total = _mood_insight_amount(item.get("total"))
        count = _mood_insight_int(item.get("count"))
        avg = _mood_insight_amount(item.get("avg"))
        risk = "高危" if item.get("high_risk") else "正常"
        lines.append(
            f"  {label}：总额 {total:.2f} 元，笔数 {count} 笔，"
            f"平均单笔 {avg:.2f} 元，是基线的 {_mood_insight_ratio_text(item.get('avg_ratio'))}，"
            f"占比 {_mood_insight_share_text(item.get('share'))}，{risk}"
        )

    untagged = stats.get("untagged")
    if isinstance(untagged, dict):
        untagged_total = _mood_insight_amount(untagged.get("total"))
        untagged_count = _mood_insight_int(untagged.get("count"))
        untagged_avg = _mood_insight_amount(untagged.get("avg"))
        lines.append(
            f"未标注情绪支出：总额 {untagged_total:.2f} 元，笔数 {untagged_count} 笔，"
            f"平均单笔 {untagged_avg:.2f} 元"
        )
    else:
        lines.append("未标注情绪支出：无")

    lines.append(
        "请用一句 50 字左右的话总结我的情绪消费模式：要有一点心理学洞察，"
        "并给我一个可执行的替代动作（例如用散步、运动、找朋友聊聊代替情绪化消费）。"
    )
    return "\n".join(lines)


def generate_mood_insight(stats: dict, api_key: str = "") -> str:
    """根据情绪标签统计生成一句约 50 字的「心理 + 理财」洞察，返回 strip 后文本。

    stats 约定（缺失键按 0 / 空处理，绝不 KeyError）：
        {"months": 3, "total_expense": 1520.0, "tagged_expense": 900.0,
         "baseline_avg": 45.0, "has_data": True,
         "high_risk": {"mood": "Emo", "avg": 90.0, "avg_ratio": 2.0,
                       "count": 6, "total": 540.0},
         "moods": [{"mood": "Emo", "emoji": "😢", "total": 540.0, "count": 6,
                    "avg": 90.0, "avg_ratio": 2.0, "share": 0.6,
                    "high_risk": True}, ...],
         "untagged": {"total": 620.0, "count": 12, "avg": 51.67}}

    失败一律抛 MoodInsightError（中文 message，可直接上页面）：
      · api_key 为空                                → no_api_key（且**不发请求**）；
      · stats 不是 dict / moods 为空或无有效条目     → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常              → network；
      · 非 200                                      → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空            → parse。
    返回文本超过 140 字时截断到 140 字并补「…」。
    日志只记模型名、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise MoodInsightError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，情绪消费洞察暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：没有情绪标签时让模型硬编只会产出空话。
    if not isinstance(stats, dict):
        raise MoodInsightError(
            "empty_stats",
            "情绪消费数据不完整，暂时无法生成洞察，请刷新后重试",
        )
    entries = _mood_insight_entries(stats)
    if not entries:
        raise MoodInsightError(
            "empty_stats",
            "还没有带情绪标签的消费记录，先记几笔带心情的账单吧",
        )

    user_content = _build_mood_insight_user_content(stats, entries)
    payload = {
        "model": MOOD_INSIGHT_MODEL,
        "messages": [
            {"role": "system", "content": MOOD_INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": MOOD_INSIGHT_TEMPERATURE,
        "max_tokens": MOOD_INSIGHT_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=MOOD_INSIGHT_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("情绪消费洞察请求超时（模型=%s）", MOOD_INSIGHT_MODEL)
        raise MoodInsightError("network", "生成情绪消费洞察超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("情绪消费洞察请求连接失败（模型=%s）", MOOD_INSIGHT_MODEL)
        raise MoodInsightError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "情绪消费洞察请求失败（模型=%s，异常=%s）",
            MOOD_INSIGHT_MODEL,
            type(exc).__name__,
        )
        raise MoodInsightError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning(
            "情绪消费洞察接口返回 HTTP %s（模型=%s）",
            resp.status_code,
            MOOD_INSIGHT_MODEL,
        )
        raise MoodInsightError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效洞察"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("情绪消费洞察响应不是合法 JSON（模型=%s）", MOOD_INSIGHT_MODEL)
        raise MoodInsightError("parse", "AI 没有返回有效洞察，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("情绪消费洞察响应结构不符合预期（模型=%s）", MOOD_INSIGHT_MODEL)
        raise MoodInsightError("parse", "AI 没有返回有效洞察，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("情绪消费洞察响应 content 为空（模型=%s）", MOOD_INSIGHT_MODEL)
        raise MoodInsightError("parse", "AI 没有返回有效洞察，请重试")

    content = content.strip()
    if len(content) > MOOD_INSIGHT_MAX_CHARS:
        content = content[:MOOD_INSIGHT_MAX_CHARS] + "…"
    logger.info("情绪消费洞察生成成功（模型=%s，字数=%d）", MOOD_INSIGHT_MODEL, len(content))
    return content


# ===========================================================================
# AI 资产管家（DeepSeek Chat：把「资产负债体检」写成一句评价 + 1~2 条处置建议）
# ---------------------------------------------------------------------------
# 与「情绪消费教练」同构：先校验（没 key、没数据都不发请求）→ 组 payload
# → 发请求 → 异常分层 → strip + 截断 → 脱敏日志。
# 差异只在入参与返回：入参是 db.get_asset_overview 的资产负债表 stats，
# 返回一段纯字符串（先评价、再点名资产该卖/该留），可直接贴到页面上。
#   · 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）；
#   · stats 缺失键一律按 0 / 空处理（字段可能随 db 版本增减）；
#   · 日志只记模型名、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
# ===========================================================================

# 系统提示词逐字固定（产品口径）。
ASSET_ADVISOR_SYSTEM_PROMPT = (
    "你是一个懂数码和理财的学长。请评价我的资产负债情况。"
    "如果我的资产一直在贬值，或者负债过高，请给出处置建议"
    "（比如：‘你的 iPad 已经闲置很久且在疯狂贬值，建议立刻去闲鱼回血’）。"
)

ASSET_ADVISOR_MODEL = "deepseek-chat"
ASSET_ADVISOR_TEMPERATURE = 0.7   # 0.7：要像学长给建议，敢点名但不飘成段子
ASSET_ADVISOR_MAX_TOKENS = 300    # 120 字 + 余量
ASSET_ADVISOR_TIMEOUT = 20
ASSET_ADVISOR_MAX_CHARS = 180     # 返回文本硬上限：超过截断到 180 字并补「…」


class AssetAdviceError(Exception):
    """AI 资产管家建议生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / assets 无有效条目且负债 <= 0（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"AssetAdviceError(code={self.code!r}, message={self.message!r})"


def _asset_advice_amount(value) -> float:
    """stats 里的金额宽松转 float：None / bool / 非数字 / nan / inf → 0.0。"""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _asset_advice_int(value, default: int = 0) -> int:
    """已用月数转 int：非法 / 负数 → default（只用于文案，不值得为此报错）。"""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0:
        return default
    return int(number)


def _asset_advice_text(value) -> str:
    """名称 / 分类宽松转 str：None / bool → ""，其余 str() 后 strip。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def _asset_advice_percent_text(value) -> str:
    """比例展示口径：0.64 → "64.0%"（入参是 0~1 的比例）。"""
    ratio = _asset_advice_amount(value)
    if ratio <= 0:
        return "0.0%"
    return f"{ratio * 100:.1f}%"


def _asset_advice_entries(stats: dict) -> list[dict]:
    """把 stats["assets"] 收敛成有效条目；不是 list 或没有名称的项直接跳过。"""
    assets = stats.get("assets")
    if not isinstance(assets, (list, tuple)):
        return []
    entries = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        if not _asset_advice_text(item.get("name")):
            continue
        entries.append(item)
    return entries


def _build_asset_advice_user_content(stats: dict, entries: list[dict]) -> str:
    """把资产负债统计排成中文条目（金额两位小数、比例 / 残值率转百分比）。"""
    cash = _asset_advice_amount(stats.get("cash_balance"))
    cost = _asset_advice_amount(stats.get("assets_cost"))
    residual = _asset_advice_amount(stats.get("assets_residual"))
    liabilities = _asset_advice_amount(stats.get("liabilities"))
    monthly_payment = _asset_advice_amount(stats.get("monthly_payment"))
    net_worth = _asset_advice_amount(stats.get("net_worth"))

    lines = [
        "这是我的资产负债情况，请按系统要求评价并给出处置建议：",
        f"现金结余：{cash:.2f} 元",
        f"资产原值合计：{cost:.2f} 元",
        f"资产残值合计：{residual:.2f} 元",
        f"待还负债：{liabilities:.2f} 元（估算）",
        f"月供：{monthly_payment:.2f} 元",
        f"净资产：{net_worth:.2f} 元",
        f"资产占比：{_asset_advice_percent_text(stats.get('asset_ratio'))}，"
        f"负债率：{_asset_advice_percent_text(stats.get('debt_ratio'))}",
        "资产明细：",
    ]
    for index, item in enumerate(entries, start=1):
        name = _asset_advice_text(item.get("name"))
        category = _asset_advice_text(item.get("category")) or "未分类"
        price = _asset_advice_amount(item.get("purchase_price"))
        residual_value = _asset_advice_amount(item.get("residual"))
        months = _asset_advice_int(item.get("elapsed_months"))
        loss = _asset_advice_amount(item.get("monthly_loss"))
        line = (
            f"  {index}. {name}（{category}）：购入价 {price:.2f} 元，"
            f"当前残值 {residual_value:.2f} 元，"
            f"残值率 {_asset_advice_percent_text(item.get('residual_rate'))}，"
            f"已用 {months} 个月，每月蒸发 {loss:.2f} 元"
        )
        if item.get("fully_depreciated"):
            line += "，已折旧完"
        lines.append(line)
    if not entries:
        lines.append("  无（当前只有负债，没有资产记录）")

    lines.append(
        "请先用一句话评价，再给 1-2 条具体处置建议（点名哪件资产该卖/该留、"
        "负债怎么降），总字数 120 字以内。"
    )
    return "\n".join(lines)


def generate_asset_advice(stats: dict, api_key: str = "") -> str:
    """根据资产负债表统计生成一句评价 + 1~2 条处置建议，返回 strip 后文本。

    stats 约定（来自 db.get_asset_overview，缺失键按 0 / 空处理，绝不 KeyError）：
        {"today": "2026-10-21", "has_assets": True,
         "cash_balance": 1200.0, "assets_cost": 9998.0, "assets_residual": 6500.0,
         "liabilities": 1200.0, "monthly_payment": 200.0,
         "net_worth": 6500.0, "asset_ratio": 0.84, "debt_ratio": 0.16,
         "assets": [{"name": "iPad Air 5", "category": "数码",
                     "purchase_price": 4999.0, "residual": 3200.0,
                     "residual_rate": 0.64, "monthly_loss": 83.32,
                     "elapsed_months": 12.0, "fully_depreciated": False}, ...]}

    失败一律抛 AssetAdviceError（中文 message，可直接上页面）：
      · api_key 为空                                → no_api_key（且**不发请求**）；
      · stats 不是 dict / 无有效资产且负债 <= 0       → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常              → network；
      · 非 200                                      → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / content 缺失或为空            → parse。
    返回文本超过 180 字时截断到 180 字并补「…」。
    日志只记模型名、字数、状态码与异常类型，**绝不**记 api_key 或金额明细。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise AssetAdviceError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，AI 资产管家暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：既没有资产也没有负债时，让模型硬编只会产出空话。
    if not isinstance(stats, dict):
        raise AssetAdviceError(
            "empty_stats",
            "资产负债数据不完整，暂时无法生成建议，请刷新后重试",
        )
    entries = _asset_advice_entries(stats)
    liabilities = _asset_advice_amount(stats.get("liabilities"))
    if not entries and liabilities <= 0:
        raise AssetAdviceError(
            "empty_stats",
            "还没有资产或负债记录，先添加资产或负债再来看看吧",
        )

    user_content = _build_asset_advice_user_content(stats, entries)
    payload = {
        "model": ASSET_ADVISOR_MODEL,
        "messages": [
            {"role": "system", "content": ASSET_ADVISOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": ASSET_ADVISOR_TEMPERATURE,
        "max_tokens": ASSET_ADVISOR_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=ASSET_ADVISOR_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("AI 资产管家请求超时（模型=%s）", ASSET_ADVISOR_MODEL)
        raise AssetAdviceError("network", "生成资产建议超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("AI 资产管家请求连接失败（模型=%s）", ASSET_ADVISOR_MODEL)
        raise AssetAdviceError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "AI 资产管家请求失败（模型=%s，异常=%s）",
            ASSET_ADVISOR_MODEL,
            type(exc).__name__,
        )
        raise AssetAdviceError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning(
            "AI 资产管家接口返回 HTTP %s（模型=%s）",
            resp.status_code,
            ASSET_ADVISOR_MODEL,
        )
        raise AssetAdviceError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效建议"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("AI 资产管家响应不是合法 JSON（模型=%s）", ASSET_ADVISOR_MODEL)
        raise AssetAdviceError("parse", "AI 没有返回有效建议，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("AI 资产管家响应结构不符合预期（模型=%s）", ASSET_ADVISOR_MODEL)
        raise AssetAdviceError("parse", "AI 没有返回有效建议，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("AI 资产管家响应 content 为空（模型=%s）", ASSET_ADVISOR_MODEL)
        raise AssetAdviceError("parse", "AI 没有返回有效建议，请重试")

    content = content.strip()
    if len(content) > ASSET_ADVISOR_MAX_CHARS:
        content = content[:ASSET_ADVISOR_MAX_CHARS] + "…"
    logger.info("AI 资产管家生成成功（模型=%s，字数=%d）", ASSET_ADVISOR_MODEL, len(content))
    return content


# ===========================================================================
# AI 闲鱼文案（DeepSeek Chat：把一件资产的折旧事实写成一条能卖出去的闲鱼文案）
# ---------------------------------------------------------------------------
# 与「AI 资产管家」「情绪消费教练」同构：先校验（没 key、没资产数据都不发请求）
# → 组 payload → 发请求 → 异常分层 → strip + 截断 → 脱敏日志。
# 差异只在入参与返回：入参是 assets 表一行 + 折旧指标，返回一段纯字符串
# （带 # 标签、带建议挂牌价的闲鱼文案），可直接贴到页面上。
#   · 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）；
#   · asset 缺失键一律按 0 / 空处理（字段可能随 db 版本增减，绝不 KeyError）；
#   · 日志只记模型名、字数、状态码与异常类型，**绝不**记 api_key、金额或资产名。
# ===========================================================================

# 系统提示词逐字固定（产品口径）。
XIANYU_SYSTEM_PROMPT = (
    "你是一个闲鱼金牌卖家。请帮我写一段极有吸引力、适合大学生的二手出售文案"
    "（带上必要的标签，如 #学生党自用 #99新）。文案要显得我当初买它是有品位的，"
    "现在卖掉是因为我要专心学习（或者换新），并给出一个建议挂牌价。"
)

XIANYU_COPY_MODEL = "deepseek-chat"
XIANYU_COPY_TEMPERATURE = 0.8   # 0.8：闲鱼文案要有人味、有钩子，但不能编造成色
XIANYU_COPY_MAX_TOKENS = 500    # 200 字 + 标签 + 余量
XIANYU_COPY_TIMEOUT = 20
XIANYU_COPY_MAX_CHARS = 400     # 返回文本硬上限：超过截断到 400 字并补「…」


class XianyuCopyError(Exception):
    """AI 闲鱼文案生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  asset 不是 dict / name 为空 / purchase_price <= 0（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"XianyuCopyError(code={self.code!r}, message={self.message!r})"


def _xianyu_amount(value) -> float:
    """asset 里的金额 / 比例宽松转 float：None / bool / 非数字 / nan / inf → 0.0。"""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _xianyu_text(value) -> str:
    """名称 / 分类宽松转 str：None / bool → ""，其余 str() 后 strip。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def _xianyu_percent_text(value) -> str:
    """比例展示口径：0.5724 → "57.2%"（入参是 0~1 的比例）。"""
    ratio = _xianyu_amount(value)
    if ratio <= 0:
        return "0.0%"
    return f"{ratio * 100:.1f}%"


def _xianyu_months_text(value) -> str:
    """已用时长展示口径：25.7 → "25.7 个月（约 2 年 1 个月）"。"""
    months = _xianyu_amount(value)
    if months <= 0:
        return "不足 1 个月（刚入手）"
    text = f"{months:.1f} 个月"
    total = int(months)
    if total >= 12:
        years, rest = divmod(total, 12)
        text += f"（约 {years} 年 {rest} 个月）" if rest else f"（约 {years} 年）"
    return text


def _build_xianyu_user_content(asset: dict) -> str:
    """把一件资产的折旧事实排成中文条目（金额两位小数、比例 / 残值率转百分比）。"""
    name = _xianyu_text(asset.get("name"))
    category = _xianyu_text(asset.get("category")) or "未分类"
    price = _xianyu_amount(asset.get("purchase_price"))
    residual = _xianyu_amount(asset.get("residual"))
    loss = _xianyu_amount(asset.get("monthly_loss"))
    suggested = _xianyu_amount(asset.get("suggested_price"))

    rate = _xianyu_amount(asset.get("residual_rate"))
    if rate <= 0 and price > 0 and residual > 0:
        # 老数据可能没有 residual_rate：用残值 / 原价兜底，避免文案里出现 0.0%。
        rate = residual / price

    if suggested > 0:
        price_line = (
            f"平台参考挂牌价：¥{suggested:.0f}"
            f"（建议围绕 ¥{suggested:.0f} 挂牌，可按成色小幅上下浮动）"
        )
    else:
        price_line = "平台参考挂牌价：暂无（这件已没有折旧参考，请按当前行情自行定价）"

    lines = [
        "这是我的二手闲置信息，请按系统要求写一段闲鱼出售文案：",
        f"商品名称：{name}",
        f"商品分类：{category}",
        f"购入原价：¥{price:.2f} 元",
        f"当前残值：¥{residual:.2f} 元（残值率 {_xianyu_percent_text(rate)}）",
        f"已使用时长：{_xianyu_months_text(asset.get('elapsed_months'))}",
        f"每月蒸发：¥{loss:.2f} 元",
        price_line,
        "文案要求：200 字以内；带 3-5 个以 # 开头的标签（如 #学生党自用 #99新）；"
        "结尾给出一个明确的建议挂牌价；不要写「我是 AI」之类的话。",
    ]
    return "\n".join(lines)


def generate_xianyu_copy(asset: dict, api_key: str = "") -> str:
    """根据一件资产的折旧指标生成一段闲鱼二手出售文案，返回 strip 后文本。

    asset 约定（assets 表一行 + 折旧指标，缺失键按 0 / 空处理，绝不 KeyError）：
        {"id": 3, "name": "iPad Air 5", "category": "数码",
         "purchase_price": 4999.0, "purchase_date": "2024-09-01",
         "depreciation_rate": 0.2, "residual": 2861.34, "residual_rate": 0.5724,
         "elapsed_months": 25.7, "monthly_loss": 83.32, "suggested_price": 2430.0}

    失败一律抛 XianyuCopyError（中文 message，可直接上页面）：
      · api_key 为空                                            → no_api_key（且**不发请求**）；
      · asset 不是 dict / name 为空 / purchase_price <= 0        → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常                          → network；
      · 非 200                                                  → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / choices/content 缺失或为空                → parse。
    返回文本超过 400 字时截断到 400 字并补「…」。
    日志只记模型名、字数、状态码与异常类型，**绝不**记 api_key、金额或资产名。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise XianyuCopyError(
            "no_api_key",
            "未配置 DEEPSEEK_API_KEY，AI 闲鱼文案暂不可用",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：没有名称或原价时，模型只能编出一段卖不出去的废话。
    if not isinstance(asset, dict):
        raise XianyuCopyError(
            "empty_stats",
            "资产数据不完整，暂时无法生成闲鱼文案，请刷新后重试",
        )
    if not _xianyu_text(asset.get("name")):
        raise XianyuCopyError(
            "empty_stats",
            "缺少资产名称，暂时无法生成闲鱼文案",
        )
    if _xianyu_amount(asset.get("purchase_price")) <= 0:
        raise XianyuCopyError(
            "empty_stats",
            "这件资产还没有购入价，补齐后再生成闲鱼文案吧",
        )

    user_content = _build_xianyu_user_content(asset)
    payload = {
        "model": XIANYU_COPY_MODEL,
        "messages": [
            {"role": "system", "content": XIANYU_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": XIANYU_COPY_TEMPERATURE,
        "max_tokens": XIANYU_COPY_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=XIANYU_COPY_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("AI 闲鱼文案请求超时（模型=%s）", XIANYU_COPY_MODEL)
        raise XianyuCopyError("network", "生成闲鱼文案超时，请稍后再试") from None
    except requests.ConnectionError:
        logger.warning("AI 闲鱼文案请求连接失败（模型=%s）", XIANYU_COPY_MODEL)
        raise XianyuCopyError("network", "网络连接失败，请检查网络") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning(
            "AI 闲鱼文案请求失败（模型=%s，异常=%s）",
            XIANYU_COPY_MODEL,
            type(exc).__name__,
        )
        raise XianyuCopyError("network", "网络连接失败，请检查网络") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning(
            "AI 闲鱼文案接口返回 HTTP %s（模型=%s）",
            resp.status_code,
            XIANYU_COPY_MODEL,
        )
        raise XianyuCopyError(
            "http",
            f"AI 服务返回异常（HTTP {resp.status_code}），请稍后重试",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → 同一条"没有有效文案"。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("AI 闲鱼文案响应不是合法 JSON（模型=%s）", XIANYU_COPY_MODEL)
        raise XianyuCopyError("parse", "AI 没有返回有效文案，请重试") from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("AI 闲鱼文案响应结构不符合预期（模型=%s）", XIANYU_COPY_MODEL)
        raise XianyuCopyError("parse", "AI 没有返回有效文案，请重试") from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("AI 闲鱼文案响应 content 为空（模型=%s）", XIANYU_COPY_MODEL)
        raise XianyuCopyError("parse", "AI 没有返回有效文案，请重试")

    content = content.strip()
    if len(content) > XIANYU_COPY_MAX_CHARS:
        content = content[:XIANYU_COPY_MAX_CHARS] + "…"
    logger.info("AI 闲鱼文案生成成功（模型=%s，字数=%d）", XIANYU_COPY_MODEL, len(content))
    return content


# ===========================================================================
# AI 宏观数据分析师（DeepSeek Chat：把「我的数据 vs 全站均值」写成一段震撼点评）
# ---------------------------------------------------------------------------
# 与「同龄人点评 / AI 资产管家」同构：先校验（没 key、没数据都不发请求）
# → 组 payload → 发请求 → 异常分层 → strip + 截断 → 脱敏日志。
# 差异只在入参与返回：入参是 db.get_macro_stats 的整包返回（global / me / delta），
# 返回一段纯字符串（震撼点评），可直接贴到页面上。
#   · 系统提示词逐字固定（产品口径，改一个字文案风格就跑偏）；
#   · 数值宽松转 float（缺失 / 脏数据按 0 处理，绝不 KeyError）；
#   · delta 有就用 app.py 算好的差值，没有就现场相减 / 相除兜底；
#   · 日志只记 code / 异常类型 / 字数，**绝不**记 api_key、金额、商户名或 user_id。
# ===========================================================================

# 系统提示词逐字固定（产品口径）。
MACRO_ANALYST_SYSTEM_PROMPT = (
    "你是一个宏观数据分析师。根据我的数据与全站均值的对比，给我一段震撼的点评。"
    "比如：'你的恩格尔系数仅为 20%，远低于全站平均的 45%，说明你很少吃食堂；"
    "而你的奶茶消费是平均值的 3 倍，恭喜你，你就是全校最受奶茶店欢迎的人！'"
)

MACRO_INSIGHT_MODEL = "deepseek-chat"
MACRO_INSIGHT_TEMPERATURE = 0.85  # 0.85：点评要"震撼"、要有梗，但不能飘成段子
MACRO_INSIGHT_MAX_TOKENS = 500    # 一段 200 字点评 + 余量
MACRO_INSIGHT_TIMEOUT = 20
MACRO_INSIGHT_MAX_CHARS = 500     # 返回文本硬上限：超过截断到 500 字并补「…」
MACRO_INSIGHT_MIN_CHARS = 20      # 返回文本硬下限：短于 20 字的"点评"视为无效


class MacroInsightError(Exception):
    """AI 宏观洞察生成失败。

    code 取值：
      no_api_key   未配置 DeepSeek API Key（**未发请求**）
      empty_stats  stats 不是 dict / 为空 / has_data 为假 /
                   me 或 global 不是 dict（**未发请求**）
      network      网络异常（超时 / 连接失败 / 其它请求层异常）
      http         接口返回非 200（message 只带状态码，不带响应体）
      parse        响应不是合法 JSON / choices/content 缺失或为空 / 正文过短

    message 为面向用户的中文提示，可直接展示。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"MacroInsightError(code={self.code!r}, message={self.message!r})"


def _macro_insight_amount(value) -> float:
    """stats 里的金额 / 比率宽松转 float：None / bool / 非数字 / nan / inf → 0.0。"""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _macro_insight_count(value) -> int:
    """样本人数 / 笔数转 int：None / bool / 非数字 / nan / inf / 负数 → 0。"""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _macro_insight_text(value) -> str:
    """月份 / 商户名宽松转 str：None / bool → ""，其余 str() 后 strip。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def _macro_insight_mapping(value) -> dict[str, float]:
    """把 category_shares 收敛成 {分类名: 比例}；不是 dict → {}（绝不 KeyError）。"""
    result: dict[str, float] = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        name = _macro_insight_text(key)
        if not name:
            continue
        result[name] = _macro_insight_amount(item)
    return result


def _macro_insight_percent_text(value) -> str:
    """比例展示口径：0.44 → "44.0%"（入参是 0~1 的比例）。"""
    return f"{_macro_insight_amount(value) * 100:.1f}%"


def _macro_insight_signed_amount(value) -> str:
    """金额差值：带正负号、两位小数（1444.64 → "+1444.64"）。"""
    return f"{_macro_insight_amount(value):+.2f}"


def _macro_insight_signed_points(value) -> str:
    """比率差值：比例 → 百分点，带正负号、一位小数（-0.06 → "-6.0"）。"""
    return f"{_macro_insight_amount(value) * 100:+.1f}"


def _macro_insight_ratio_text(value) -> str:
    """倍数展示口径：1.85 → "1.85 倍"。"""
    return f"{_macro_insight_amount(value):.2f} 倍"


def _build_macro_insight_user_content(stats: dict) -> str:
    """把 global / me / delta 排成纯文本分点（金额两位小数、比率转百分比一位小数）。

    只放对比结论需要的数字：月份、样本人数、支出/收入/恩格尔/结余率/笔数、
    分类占比、全站 Top3 热门商户。**绝不**把 me.user_id 之类的主键写进 prompt。
    """
    month_label = (
        _macro_insight_text(stats.get("month_label"))
        or _macro_insight_text(stats.get("month"))
        or "本月"
    )
    user_count = _macro_insight_count(stats.get("user_count"))
    low_sample = _coerce_bool(stats.get("low_sample"), default=False)

    global_stats = stats.get("global") if isinstance(stats.get("global"), dict) else {}
    me = stats.get("me") if isinstance(stats.get("me"), dict) else {}
    delta = stats.get("delta") if isinstance(stats.get("delta"), dict) else {}

    # ---- 支出：差值优先用 app.py 算好的 delta，缺了才现场相减 / 相除兜底 ----
    my_expense = _macro_insight_amount(me.get("expense"))
    peer_expense = _macro_insight_amount(global_stats.get("avg_expense"))
    expense_delta = (
        _macro_insight_amount(delta.get("expense"))
        if "expense" in delta
        else my_expense - peer_expense
    )
    expense_ratio = (
        _macro_insight_amount(delta.get("expense_ratio"))
        if "expense_ratio" in delta
        else (my_expense / peer_expense if peer_expense > 0 else 0.0)
    )
    my_income = _macro_insight_amount(me.get("income"))
    peer_income = _macro_insight_amount(global_stats.get("avg_income"))
    income_delta = (
        _macro_insight_amount(delta.get("income"))
        if "income" in delta
        else my_income - peer_income
    )
    my_engel = _macro_insight_amount(me.get("engel"))
    peer_engel = _macro_insight_amount(global_stats.get("avg_engel"))
    engel_delta = (
        _macro_insight_amount(delta.get("engel"))
        if "engel" in delta
        else my_engel - peer_engel
    )
    my_saving = _macro_insight_amount(me.get("saving_rate"))
    peer_saving = _macro_insight_amount(global_stats.get("avg_saving_rate"))
    saving_delta = (
        _macro_insight_amount(delta.get("saving_rate"))
        if "saving_rate" in delta
        else my_saving - peer_saving
    )
    my_bills = _macro_insight_count(me.get("bill_count"))
    peer_bills = _macro_insight_count(global_stats.get("avg_bill_count"))

    lines = [
        "这是我的本月账本与全站均值的对比，请按系统要求写一段震撼的点评：",
        f"月份：{month_label}（全站样本 {user_count} 位同学）",
    ]
    if low_sample:
        lines.append(
            f"样本提示：全站仅有 {user_count} 位同学的数据，样本偏少，结论仅供参考"
        )
    lines.append(
        f"我的支出：{my_expense:.2f} 元；全站均值：{peer_expense:.2f} 元；"
        f"差值 {_macro_insight_signed_amount(expense_delta)} 元"
        f"（我的支出是全站均值的 {_macro_insight_ratio_text(expense_ratio)}）"
    )
    lines.append(
        f"我的收入：{my_income:.2f} 元；全站均值：{peer_income:.2f} 元；"
        f"差值 {_macro_insight_signed_amount(income_delta)} 元"
    )
    lines.append(
        f"我的恩格尔系数：{_macro_insight_percent_text(my_engel)}；"
        f"全站均值：{_macro_insight_percent_text(peer_engel)}；"
        f"差值 {_macro_insight_signed_points(engel_delta)} 个百分点"
    )
    lines.append(
        f"我的结余率：{_macro_insight_percent_text(my_saving)}；"
        f"全站均值：{_macro_insight_percent_text(peer_saving)}；"
        f"差值 {_macro_insight_signed_points(saving_delta)} 个百分点"
    )
    lines.append(
        f"我的月账单笔数：{my_bills} 笔；全站均值：{peer_bills} 笔；"
        f"差值 {my_bills - peer_bills:+d} 笔"
    )

    # 分类占比：以「我的分类」为主，再补只出现在全站里的分类（缺的一侧按 0% 展示）。
    my_shares = _macro_insight_mapping(me.get("category_shares"))
    peer_shares = _macro_insight_mapping(global_stats.get("category_shares"))
    share_names = list(my_shares) + [name for name in peer_shares if name not in my_shares]
    if share_names:
        items = [
            f"{name} 我 {_macro_insight_percent_text(my_shares.get(name))} / "
            f"全站 {_macro_insight_percent_text(peer_shares.get(name))}"
            for name in share_names
        ]
        lines.append("分类占比：" + "；".join(items))
    else:
        lines.append("分类占比：无")

    # 全站 Top3 热门商户：结构不对的条目直接跳过（宁可少列一个，也不要 KeyError）。
    merchant_lines = []
    top_merchants = global_stats.get("top_merchants")
    if isinstance(top_merchants, (list, tuple)):
        for item in top_merchants[:3]:
            if not isinstance(item, dict):
                continue
            name = _macro_insight_text(item.get("name")) or "未知商户"
            merchant_lines.append(
                f"  {len(merchant_lines) + 1}. {name}"
                f"（{_macro_insight_count(item.get('users'))} 位同学，"
                f"{_macro_insight_count(item.get('count'))} 笔，"
                f"{_macro_insight_amount(item.get('amount')):.2f} 元）"
            )
    if merchant_lines:
        lines.append("全站 Top3 热门商户：")
        lines.extend(merchant_lines)
    else:
        lines.append("全站 Top3 热门商户：无")

    lines.append(
        "输出要求：一段震撼的点评，150-250 字；直接说，不要分点、不要解释、不要复述全部数字；"
        "可以拿食堂、奶茶、外卖这类场景开涮，但不要编造数据。"
    )
    return "\n".join(lines)


def generate_macro_insight(stats: dict, api_key: str = "") -> str:
    """根据「我的数据 vs 全站均值」生成一段震撼点评，返回 strip 后文本。

    stats 约定（来自 db.get_macro_stats，缺失键按 0 / 空处理，绝不 KeyError）：
        {"month": "2026-10", "month_label": "2026年10月",
         "user_count": 3, "low_sample": True, "has_data": True,
         "global": {"avg_expense": 200.0, "avg_income": 350.0, "avg_engel": 0.5,
                    "avg_saving_rate": 0.25, "avg_bill_count": 3.0,
                    "category_shares": {"餐饮": 0.5, "购物": 0.3},
                    "top_merchants": [{"name": "食堂", "users": 3, "count": 3,
                                       "amount": 300.0}]},
         "me": {"user_id": 123, "has_data": True, "expense": 2679.2, "income": 3000.0,
                "engel": 0.44, "saving_rate": 0.11, "bill_count": 10,
                "category_shares": {"餐饮": 0.4, "奶茶咖啡": 0.2},
                "top_merchant": {"name": "食堂", "count": 4, "amount": 300.0}},
         "delta": {"expense": 1444.64, "expense_ratio": 1.85,
                   "engel": -0.06, "saving_rate": -0.14}}

    失败一律抛 MacroInsightError（中文 message，可直接上页面）：
      · api_key 为空（strip 后）                                → no_api_key（且**不发请求**）；
      · stats 不是 dict / 为空 / has_data 为假 / me 或 global 不是 dict
                                                             → empty_stats（且**不发请求**）；
      · 超时 / 连接失败 / 其它请求层异常                          → network；
      · 非 200                                                 → http（只带状态码，不拼响应体）；
      · 响应不是 JSON / 结构不符 / content 为空 / 正文短于 20 字    → parse。
    返回前 strip；超过 500 字截断到 500 字并补「…」；**绝不**把 user_id 写进 prompt。
    日志只记 code、异常类型与字数，**绝不**记 api_key、金额或商户名。
    """
    # ① api_key：先查它，没有 key 时不该先花一次网络往返。
    if not isinstance(api_key, str) or not api_key.strip():
        raise MacroInsightError(
            "no_api_key",
            "未配置 DeepSeek API Key，暂时无法生成宏观洞察",
        )
    api_key = api_key.strip()

    # ② 数据前置校验：没数据时模型只能编，宁可提示"数据不够"也不发请求。
    if not isinstance(stats, dict) or not stats:
        raise MacroInsightError("empty_stats", "还没有足够的数据生成宏观洞察")
    if not _coerce_bool(stats.get("has_data"), default=False):
        raise MacroInsightError("empty_stats", "还没有足够的数据生成宏观洞察")
    if not isinstance(stats.get("global"), dict) or not isinstance(stats.get("me"), dict):
        raise MacroInsightError("empty_stats", "还没有足够的数据生成宏观洞察")

    month_label = (
        _macro_insight_text(stats.get("month_label"))
        or _macro_insight_text(stats.get("month"))
        or "本月"
    )
    user_content = _build_macro_insight_user_content(stats)
    payload = {
        "model": MACRO_INSIGHT_MODEL,
        "messages": [
            {"role": "system", "content": MACRO_ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": MACRO_INSIGHT_TEMPERATURE,
        "max_tokens": MACRO_INSIGHT_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ③ 发请求：超时/连接失败翻译成用户能看懂的句子（不把异常对象漏到页面）。
    try:
        resp = requests.post(
            DEEPSEEK_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=MACRO_INSIGHT_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("宏观洞察生成失败（code=network，异常=Timeout）")
        raise MacroInsightError(
            "network",
            "宏观洞察生成失败：网络超时，请稍后重试",
        ) from None
    except requests.ConnectionError:
        logger.warning("宏观洞察生成失败（code=network，异常=ConnectionError）")
        raise MacroInsightError("network", "宏观洞察生成失败：网络连接失败") from None
    except requests.RequestException as exc:
        # SSL/代理/URL 等其余请求层异常：日志只记异常类型（确保不把 headers / key 带出去）。
        logger.warning("宏观洞察生成失败（code=network，异常=%s）", type(exc).__name__)
        raise MacroInsightError("network", "宏观洞察生成失败：网络连接失败") from None

    # ④ 非 200：只带状态码，不把响应体拼进 message（响应体可能含请求回显）。
    if resp.status_code != 200:
        logger.warning("宏观洞察生成失败（code=http，HTTP=%s）", resp.status_code)
        raise MacroInsightError(
            "http",
            f"宏观洞察生成失败：服务返回 {resp.status_code}",
        )

    # ⑤ 解析：非 JSON / 结构不符 / content 为空 → parse。
    try:
        data = resp.json()
    except (ValueError, TypeError):
        logger.warning("宏观洞察生成失败（code=parse，异常=JSONDecode）")
        raise MacroInsightError(
            "parse",
            "宏观洞察生成失败：模型返回内容无法解析",
        ) from None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("宏观洞察生成失败（code=parse，异常=结构不符）")
        raise MacroInsightError(
            "parse",
            "宏观洞察生成失败：模型返回内容无法解析",
        ) from None
    if not isinstance(content, str) or not content.strip():
        logger.warning("宏观洞察生成失败（code=parse，异常=content 为空）")
        raise MacroInsightError("parse", "宏观洞察生成失败：模型没有返回内容")

    content = content.strip()
    # ⑥ 截断 / 兜底：超过 500 字截断补「…」；短于 20 字说明模型只回了句废话，按 parse 处理。
    if len(content) > MACRO_INSIGHT_MAX_CHARS:
        content = content[:MACRO_INSIGHT_MAX_CHARS] + "…"
    if len(content) < MACRO_INSIGHT_MIN_CHARS:
        logger.warning("宏观洞察生成失败（code=parse，异常=正文过短，字数=%d）", len(content))
        raise MacroInsightError("parse", "宏观洞察生成失败：模型返回内容过短，请重试")

    logger.info("宏观洞察生成成功（月份=%s，字数=%d）", month_label, len(content))
    return content
