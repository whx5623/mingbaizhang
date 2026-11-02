# privacy.py
"""隐私脱敏模式（Privacy Mode）的纯函数。

产品背景：「隐私优先」是答辩时的护城河之一，现场演示需要一个**一键开关**：
  · 展示层：账单明细、商户名、图表标签自动打码（蜜雪冰城 → 蜜雪**）；
  · 发送层：语音转写 / 聊天文本在发往 AI 之前，本地正则替换手机号、身份证、
    银行卡、邮箱等敏感串，避免把用户隐私原样送出。

设计边界：
  · 只做**字符串变换**，不读库、不写库、不依赖 Streamlit —— 便于单测；
  · 任何输入都收敛成 str，绝不抛异常（脱敏是兜底逻辑，不能把页面搞崩）；
  · 展示打码是**不可逆**的（星号），发送脱敏也是不可逆的；原始数据始终留在本地库中。
"""

import re

MASK_CHAR = "*"

# 展示层默认保留前 2 个字：蜜雪冰城 → 蜜雪**；海底捞 → 海底*；食堂 → 食*。
MASK_KEEP_CHARS = 2
MASK_MAX_STARS = 6

# 发送层正则：只替换"像敏感号码"的子串，金额与日期保持原样，AI 才能继续记账。
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([\w.+-]{1,2})[\w.+-]*@([\w.-]+\.[A-Za-z]{2,})")


def _as_text(value) -> str:
    """把任意值收敛成 str：None → ""；其它 → str(value)。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def mask_merchant(name) -> str:
    """商户名/任意短文本打码：保留前 2 个字，其余变星号（最多 6 个）。

    - "" / None → ""
    - 1 个字：「食」→「食」（没有可打码的部分，保持原样）
    - 2 个字：「食堂」→「食*」
    - 3 个字：「海底捞」→「海底*」
    - 4 个字：「蜜雪冰城」→「蜜雪**」
    """
    text = _as_text(name).strip()
    if not text:
        return ""
    keep = max(1, int(MASK_KEEP_CHARS))
    if len(text) <= 1:
        return text
    if len(text) <= keep:
        return text[:1] + MASK_CHAR * (len(text) - 1)
    stars = min(MASK_MAX_STARS, max(1, len(text) - keep))
    return text[:keep] + MASK_CHAR * stars


def mask_text(text) -> str:
    """任意展示文本的轻量脱敏：去掉首尾空白后，按 mask_merchant 的规则打码。

    只用于**短文本**（商户名、分类、平台名）；长文本（账单备注、聊天正文）不要用，
    否则会把整段话变成「你好****」。
    """
    return mask_merchant(text)


def mask_sensitive_text(text) -> str:
    """发送给 AI 之前做本地正则脱敏：手机号 / 身份证 / 银行卡 / 邮箱。

    例：
      13812345678      → 138****5678
      110101199001011234 → 110101********1234
      6222021234567890123 → 6222***********0123（按卡号规则）
      alice@example.com → al***@example.com

    金额（如 12.50）、日期（2026-11-01）、普通中文一律不动 —— 这些是记账必需信息。
    任何异常都返回原文（脱敏失败也不能阻断记账）。
    """
    source = _as_text(text)
    if not source:
        return ""
    try:
        masked = _ID_CARD_RE.sub(r"\1" + MASK_CHAR * 8 + r"\2", source)
        masked = _BANK_CARD_RE.sub(r"\1" + MASK_CHAR * 8 + r"\2", masked)
        masked = _PHONE_RE.sub(r"\1" + MASK_CHAR * 4 + r"\2", masked)
        masked = _EMAIL_RE.sub(lambda m: m.group(1) + "***@" + m.group(2), masked)
        return masked
    except Exception:
        return source


def mask_amount(value) -> str:
    """金额展示打码：隐藏小数后两位（128.50 → 128.**，-39.90 → -39.**）。

    为什么是「隐藏后两位」而不是随机打乱：随机数会让同一笔账单每次刷新都不一样，
    演示时容易被追问「这数怎么变了」；隐藏两位既遮住精确金额，又保留量级可读性。

    边界：
      · None / 空串 / 非数字 → ""（不显示金额，而不是显示 0）；
      · 0 → "0.**"；
      · 负数保留负号；千分位保留（1,234.**）。
    """
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number:  # NaN
        return ""
    try:
        sign = "-" if number < 0 else ""
        body = f"{abs(number):,.2f}"
        integer_part = body.split(".")[0]
        return f"{sign}{integer_part}.**"
    except Exception:
        return ""


def mask_record(record: dict, fields=("merchant",)) -> dict:
    """字典版打码（不修改入参）：只处理指定字段，其它字段原样返回。"""
    if not isinstance(record, dict):
        return {}
    masked = dict(record)
    for field in fields:
        if field in masked:
            masked[field] = mask_merchant(masked[field])
    return masked