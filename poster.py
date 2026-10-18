"""财务诊断海报绘制（Pillow 本地生成，不联网、不消耗 AI 额度）。

产物是一张 1080×1920 的竖向长图，风格参考「网易云年度报告」：
深色渐变背景 → 标题 → 四块核心数据 → AI 点评卡片 → 品牌落款（可选二维码）。

对外只有一个入口 :func:`build_poster`。所有绘制异常（字体缺失、PIL 内部错误、
脏 payload 等）都会统一包装成 :class:`PosterError`，文案可直接展示给用户，
不会把 PIL traceback 漏到 Streamlit 页面上。

字体解析顺序（先到先得）：
1. 环境变量 ``POSTER_FONT_PATH``；
2. 项目内 ``assets/fonts/*.ttf|*.ttc|*.otf``（按文件名排序取第一个）；
3. Windows 自带字体（msyh.ttc / msyhbd.ttc / simhei.ttf）；
4. Linux 云端字体（Noto CJK / 文泉驿；DejaVu 只保底，中文会缺字，会打警告）。
"""

from __future__ import annotations

import glob
import io
import os
from datetime import datetime
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


class PosterError(Exception):
    """海报生成失败；``str(exc)`` 是可直接展示给用户的中文提示。"""


# ============================ 画布与配色 ============================
CANVAS_SIZE = (1080, 1920)
_BG_TOP = (15, 23, 42)        # #0f172a
_BG_BOTTOM = (30, 41, 59)     # #1e293b
_TEXT_MAIN = (248, 250, 252)
_TEXT_MUTED = (148, 163, 184)
_TEXT_DIM = (100, 116, 139)
_ACCENT = (56, 189, 248)
_CARD_BG = (30, 41, 59)
_CARD_BORDER = (51, 65, 85)

_MARGIN = 72
_CONTENT_W = CANVAS_SIZE[0] - _MARGIN * 2

# AI 点评卡片固定区域（高度固定，保证整张海报版式稳定）
_AI_BOX = (72, 1000, 1008, 1480)


# ============================ 字体候选 ============================
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_FONT_DIR = os.path.join(_PROJECT_DIR, "assets", "fonts")

# 这两个列表保留为模块级变量：云端/测试可以整体 patch 成空列表来模拟「无中文字体」。
_WINDOWS_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]
_LINUX_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
# 最后一项只保底：能画英文数字，但中文会变成方框，命中时打警告并继续。
_LAST_RESORT_FONTS = {os.path.normcase(os.path.abspath(_LINUX_FONTS[-1]))}


def _iter_project_fonts() -> list[str]:
    """返回项目内 assets/fonts 下的字体文件（按扩展名分组、文件名排序）。"""
    if not os.path.isdir(_PROJECT_FONT_DIR):
        return []
    found: list[str] = []
    for pattern in ("*.ttf", "*.ttc", "*.otf"):
        found.extend(sorted(glob.glob(os.path.join(_PROJECT_FONT_DIR, pattern))))
    return found


def _resolve_font_path() -> str:
    """按约定顺序找到第一个存在的中文字体文件；全都没有就抛 PosterError。"""
    env_path = (os.environ.get("POSTER_FONT_PATH") or "").strip()
    candidates: list[str] = []
    if env_path:
        candidates.append(env_path)
    candidates.extend(_iter_project_fonts())
    candidates.extend(_WINDOWS_FONTS)
    candidates.extend(_LINUX_FONTS)

    for path in candidates:
        try:
            if path and os.path.isfile(path):
                normalized = os.path.normcase(os.path.abspath(path))
                if normalized in _LAST_RESORT_FONTS:
                    print(
                        "[poster] 警告：只找到 DejaVuSans，中文可能显示为方框；"
                        "请安装 fonts-noto-cjk 或配置 POSTER_FONT_PATH"
                    )
                return path
        except OSError:
            continue

    raise PosterError(
        "缺少中文字体，无法绘制海报：请把中文字体放到 assets/fonts/，"
        "或设置环境变量 POSTER_FONT_PATH（Streamlit Cloud 请安装 fonts-noto-cjk）"
    )


def _resolve_bold_font_path(regular_path: str) -> str:
    """找同家族粗体；找不到就复用常规字体（调用方会用更大的字号兜底）。"""
    folder = os.path.dirname(regular_path)
    base = os.path.basename(regular_path)

    candidates: list[str] = []
    if base.lower() == "msyh.ttc":
        candidates.append(os.path.join(folder, "msyhbd.ttc"))
    for token in ("Regular", "regular"):
        if token in base:
            candidates.append(os.path.join(folder, base.replace(token, "Bold")))

    for candidate in candidates:
        try:
            if os.path.isfile(candidate):
                return candidate
        except OSError:
            continue
    return regular_path


@lru_cache(maxsize=96)
def _load_font(path: str, size: int):
    """按 (路径, 字号) 缓存字体对象；TTC 直接 truetype 即可，无需 index。"""
    return ImageFont.truetype(path, int(size))


# ============================ 文本工具 ============================
def _text_width(draw, text: str, font) -> float:
    """测量文本宽度；不同 Pillow 版本 API 略有差异，逐级降级。"""
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        try:
            return float(font.getlength(text))
        except Exception:
            return float(len(text) * max(10, int(getattr(font, "size", 20))))


def _wrap_text(draw, text: str, font, max_width: float) -> list[str]:
    """按字符宽度换行（中文逐字），保留手动换行符。"""
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def _clip_lines(draw, lines: list[str], font, max_width: float, max_lines: int) -> list[str]:
    """限制行数；被截断的最后一行补省略号。"""
    if len(lines) <= max_lines:
        return lines
    clipped = lines[:max_lines]
    last = clipped[-1]
    while last and _text_width(draw, last + "…", font) > max_width:
        last = last[:-1]
    clipped[-1] = last + "…"
    return clipped


def _line_height(font, ratio: float = 1.32) -> int:
    try:
        size = int(font.size)
    except Exception:
        size = 20
    return max(1, int(round(size * ratio)))


def _money(value) -> str:
    try:
        return f"¥{float(value):,.2f}"
    except (TypeError, ValueError):
        return "¥0.00"


# ============================ 绘制积木 ============================
def _draw_gradient(img: Image.Image) -> None:
    """逐行 draw.line 画竖向渐变（1080×1920 共 1920 行，开销可接受）。"""
    draw = ImageDraw.Draw(img)
    width, height = img.size
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            int(round(_BG_TOP[i] + (_BG_BOTTOM[i] - _BG_TOP[i]) * ratio))
            for i in range(3)
        )
        draw.line([(0, y), (width, y)], fill=color)


def _draw_decor(img: Image.Image) -> None:
    """两个大尺寸半透明圆，给纯色渐变增加一点层次。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((-280, 1400, 380, 2060), fill=(56, 189, 248, 16))
    draw.ellipse((820, -240, 1280, 220), fill=(129, 140, 248, 20))
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    img.paste(base.convert("RGB"))


def _composite_rgba(img: Image.Image, box, radius: int, fill, outline) -> Image.Image:
    """在 RGB 画布上叠加一块半透明圆角矩形，返回新的 RGB 图像。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    result = base.convert("RGB")
    img.paste(result)
    return img


def _draw_centered(draw, y: int, text: str, font, fill) -> None:
    width = _text_width(draw, text, font)
    draw.text(((CANVAS_SIZE[0] - width) / 2, y), text, font=font, fill=fill)


def _draw_metric_card(
    draw,
    box,
    label: str,
    value: str,
    sub: str,
    accent,
    regular_path: str,
    bold_path: str,
) -> None:
    """一块数据卡：左侧竖色条 + 标签 + 大数字（可两行）+ 底部小字。"""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=24, fill=_CARD_BG, outline=_CARD_BORDER, width=2)
    draw.rounded_rectangle((x0 + 2, y0 + 34, x0 + 8, y1 - 34), radius=3, fill=accent)

    label_font = _load_font(regular_path, 27)
    draw.text((x0 + 34, y0 + 22), label, font=label_font, fill=_TEXT_MUTED)

    inner_w = (x1 - x0) - 68
    value_font = _load_font(bold_path, 54)
    lines = _wrap_text(draw, value, value_font, inner_w)
    if len(lines) > 2:
        value_font = _load_font(bold_path, 42)
        lines = _clip_lines(draw, _wrap_text(draw, value, value_font, inner_w), value_font, inner_w, 2)
    elif len(lines) > 1:
        value_font = _load_font(bold_path, 44)
        lines = _clip_lines(draw, lines, value_font, inner_w, 2)

    line_h = _line_height(value_font, 1.18)
    value_y = y0 + 68
    for offset, line in enumerate(lines):
        draw.text((x0 + 34, value_y + offset * line_h), line, font=value_font, fill=_TEXT_MAIN)

    if sub:
        sub_font = _load_font(regular_path, 24)
        sub_y = y1 - 42
        if len(lines) > 1:
            sub_y = y1 - 34
        draw.text((x0 + 34, sub_y), sub, font=sub_font, fill=_ACCENT)


def _draw_ai_card(
    img: Image.Image,
    regular_path: str,
    bold_path: str,
    comment: str,
) -> Image.Image:
    """AI 点评卡片：深色半透明底 + 左侧竖色条 + 自动换行的点评。"""
    img = _composite_rgba(
        img,
        _AI_BOX,
        radius=28,
        fill=(30, 41, 59, 200),
        outline=(56, 189, 248, 90),
    )
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = _AI_BOX

    # 左侧竖色条
    draw.rounded_rectangle((x0 + 28, y0 + 40, x0 + 36, y1 - 40), radius=4, fill=_ACCENT)

    heading_font = _load_font(bold_path, 34)
    draw.text((x0 + 66, y0 + 38), "AI 财务点评", font=heading_font, fill=(125, 211, 252))

    body_font = _load_font(regular_path, 38)
    text_left = x0 + 66
    text_width = x1 - text_left - 44
    lines = _clip_lines(draw, _wrap_text(draw, comment, body_font, text_width), body_font, text_width, 4)
    line_h = _line_height(body_font, 1.45)

    region_top = y0 + 112
    region_bottom = y1 - 142
    block_h = len(lines) * line_h
    start_y = region_top + max(0, (region_bottom - region_top - block_h) // 2)
    for offset, line in enumerate(lines):
        draw.text((text_left, start_y + offset * line_h), line, font=body_font, fill=(226, 232, 240))

    quote_font = _load_font(regular_path, 27)
    draw.text((text_left, y1 - 100), "愿每一笔钱，都花在值得的地方。", font=quote_font, fill=_TEXT_DIM)

    sign_font = _load_font(regular_path, 23)
    signature = "—— 明白账 · AI 财务管家"
    sign_w = _text_width(draw, signature, sign_font)
    draw.text((x1 - 40 - sign_w, y1 - 54), signature, font=sign_font, fill=(71, 85, 105))
    return img


def _make_qr(app_url: str, size: int = 160):
    """生成二维码位图；qrcode 缺失或编码失败时返回 None（海报照常出，只是没码）。"""
    if not app_url:
        return None
    try:
        import qrcode
    except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
        print(f"[poster] qrcode 不可用，跳过二维码：{type(exc).__name__}: {exc}")
        return None
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=1,
        )
        qr.add_data(app_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return qr_img.resize((size, size), Image.Resampling.NEAREST)
    except Exception as exc:
        print(f"[poster] 二维码绘制失败，跳过：{type(exc).__name__}: {exc}")
        return None


def _draw_qr(img: Image.Image, app_url: str, regular_path: str) -> bool:
    """右下角白底二维码 + 一行小字；返回是否真的画上了。"""
    qr_img = _make_qr(app_url)
    if qr_img is None:
        return False

    left, top, side = 830, 1630, 172
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((left, top, left + side, top + side), radius=14, fill=(255, 255, 255))
    img.paste(qr_img, (left + 6, top + 6))

    draw = ImageDraw.Draw(img)
    caption_font = _load_font(regular_path, 22)
    caption = "扫码查看我的账本"
    caption_w = _text_width(draw, caption, caption_font)
    draw.text(
        (left + (side - caption_w) / 2, top + side + 8),
        caption,
        font=caption_font,
        fill=_TEXT_MUTED,
    )
    return True


# ============================ payload 归一化 ============================
def _safe_text(value, fallback: str) -> str:
    try:
        text = str(value).strip()
    except Exception:
        return fallback
    return text or fallback


def _safe_float(value, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    # NaN / inf 会让文字排版爆炸，统一压回 fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return max(0.0, number)


def _normalize_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PosterError("海报数据格式不正确，请稍后再试")

    month = _safe_text(payload.get("month"), datetime.now().strftime("%Y-%m"))
    if len(month) != 7 or month[4] != "-":
        month = datetime.now().strftime("%Y-%m")

    try:
        score = int(round(float(payload.get("health_score") or 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    return {
        "nickname": _safe_text(payload.get("nickname"), "用户"),
        "month": month,
        "total_expense": _safe_float(payload.get("total_expense")),
        "health_score": score,
        "health_grade": _safe_text(payload.get("health_grade"), "-"),
        "health_title": _safe_text(payload.get("health_title"), "未评级"),
        "top_category": _safe_text(payload.get("top_category"), "暂无"),
        "top_amount": _safe_float(payload.get("top_amount")),
        "comment": _safe_text(
            payload.get("comment"),
            "这个月辛苦了，继续坚持记账，下个月会更从容。",
        ),
        "app_url": _safe_text(payload.get("app_url"), ""),
    }


# ============================ 主流程 ============================
def _render(data: dict) -> bytes:
    regular_path = _resolve_font_path()
    bold_path = _resolve_bold_font_path(regular_path)

    img = Image.new("RGB", CANVAS_SIZE, _BG_TOP)
    _draw_gradient(img)
    _draw_decor(img)
    draw = ImageDraw.Draw(img)

    # ---- 顶部小标题 ----
    draw.rounded_rectangle((_MARGIN, 98, _MARGIN + 8, 128), radius=4, fill=_ACCENT)
    small_font = _load_font(regular_path, 30)
    draw.text((_MARGIN + 24, 92), "明白账 · 月度财务诊断", font=small_font, fill=(125, 211, 252))

    # ---- 大标题：月份 + 「我的钱都去哪了」----
    month_font = _load_font(bold_path, 48)
    draw.text((_MARGIN, 160), data["month"], font=month_font, fill=_ACCENT)

    title_font = _load_font(bold_path, 76)
    draw.text((_MARGIN, 226), "我的钱都去哪了", font=title_font, fill=_TEXT_MAIN)

    # ---- 副标题 + 昵称 ----
    subtitle_font = _load_font(regular_path, 28)
    draw.text((_MARGIN, 346), "AI 财务诊断 · 每一笔都算数", font=subtitle_font, fill=_TEXT_DIM)

    nickname = data["nickname"]
    if len(nickname) > 12:
        nickname = nickname[:12] + "…"
    hello = f"Hi，{nickname}"
    hello_w = _text_width(draw, hello, subtitle_font)
    draw.text(
        (CANVAS_SIZE[0] - _MARGIN - hello_w, 346),
        hello,
        font=subtitle_font,
        fill=(125, 211, 252),
    )

    # ---- 分隔线 ----
    draw.line((_MARGIN, 412, CANVAS_SIZE[0] - _MARGIN, 412), fill=_CARD_BORDER, width=2)

    # ---- 四块核心数据（2×2）----
    gap = 32
    card_w = (CANVAS_SIZE[0] - _MARGIN * 2 - gap) // 2
    card_h = 210
    row1, row2 = 470, 470 + card_h + gap
    col1, col2 = _MARGIN, _MARGIN + card_w + gap

    _draw_metric_card(
        draw,
        (col1, row1, col1 + card_w, row1 + card_h),
        "本月支出",
        _money(data["total_expense"]),
        "本月累计支出",
        (56, 189, 248),
        regular_path,
        bold_path,
    )
    _draw_metric_card(
        draw,
        (col2, row1, col2 + card_w, row1 + card_h),
        "健康分",
        f"{data['health_score']} 分",
        f"{data['health_grade']} 级",
        (52, 211, 153),
        regular_path,
        bold_path,
    )
    _draw_metric_card(
        draw,
        (col1, row2, col1 + card_w, row2 + card_h),
        "Top1 消费",
        data["top_category"],
        _money(data["top_amount"]),
        (251, 191, 36),
        regular_path,
        bold_path,
    )
    _draw_metric_card(
        draw,
        (col2, row2, col2 + card_w, row2 + card_h),
        "等级称号",
        data["health_title"],
        f"{data['health_grade']} 级 · 财务等级",
        (167, 139, 250),
        regular_path,
        bold_path,
    )

    # ---- AI 点评卡片 ----
    img = _draw_ai_card(img, regular_path, bold_path, data["comment"])
    draw = ImageDraw.Draw(img)

    # ---- 底部文案 ----
    quote_font = _load_font(regular_path, 30)
    draw.text(
        (_MARGIN, 1558),
        "钱不是省出来的，是看清楚之后花出来的。",
        font=quote_font,
        fill=_TEXT_DIM,
    )

    # ---- 二维码（仅当 APP_URL 配置且 qrcode 可用）----
    has_qr = _draw_qr(img, data["app_url"], regular_path)
    draw = ImageDraw.Draw(img)

    # ---- 落款 ----
    footer_font = _load_font(regular_path, 28)
    footer = f"@明白账 · {datetime.now().strftime('%Y-%m-%d')}"
    if has_qr:
        draw.text((_MARGIN, 1838), footer, font=footer_font, fill=_TEXT_MUTED)
    else:
        _draw_centered(draw, 1838, footer, footer_font, _TEXT_MUTED)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def build_poster(payload: dict) -> bytes:
    """把 payload 画成 PNG，返回字节串。

    Args:
        payload: 见模块 docstring / 调用方 app.py；缺字段会用安全默认值补齐。

    Returns:
        1080×1920 PNG 的 bytes。

    Raises:
        PosterError: 字体缺失、数据非法或任何绘制失败（``str(exc)`` 可直接展示）。
    """
    try:
        data = _normalize_payload(payload)
        return _render(data)
    except PosterError:
        raise
    except Exception as exc:
        # PIL / 编码 / 内存等内部错误：只把类型写日志，页面统一拿中文兜底文案
        print(f"[poster] 绘制失败：{type(exc).__name__}: {exc}")
        raise PosterError("海报绘制失败，请稍后再试") from exc
