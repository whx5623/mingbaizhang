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
                        "type": {"type": "string", "description": "收入/支出"}
                    },
                    "required": ["date", "merchant", "amount", "payment_method", "type"]
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
