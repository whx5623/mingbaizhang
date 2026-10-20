# -*- coding: utf-8 -*-
"""多页「个人财务体检报告」PDF 渲染（纯 Pillow：无网络、无数据库、无 Streamlit）。

设计口径
--------
· 输出固定 3 页 A4（1240×1754 @150dpi）：P1 核心概览 / P2 消费透视 / P3 AI 诊断与建议；
· 只吃 app.py 组装好的 payload dict，所有字段可选：缺字段按 0 / 空处理并画占位文案，
  任何输入（含 ``{}``）都**不抛异常**，页面不会因为报告而红屏；
· 图表全部用 Pillow 自绘（玫瑰图 / 日历热力图 / 结余折线），不依赖 matplotlib、kaleido；
· 中文字体直接复用 poster.py 的解析与排版工具（本机微软雅黑、云端 Noto，不硬编码路径）；
· 商务蓝白配色 + 「明白账」斜向水印 + 页眉 wordmark + 页脚页码与免责声明。

对外接口（app.py 与测试只依赖这四个名字，不要改签名）::

    PAGE_SIZE = (1240, 1754)
    REPORT_TITLE = "个人财务体检报告"
    PAGE_COUNT = 3

    classify_spending_mbti(metrics) -> dict
    render_pages(payload) -> list[PIL.Image.Image]
    build_financial_report(payload) -> bytes
    report_available() -> bool
"""

from __future__ import annotations

import datetime as _dt
import io
import math

from PIL import Image, ImageDraw

from poster import (
    _clip_lines,
    _line_height,
    _load_font,
    _money,
    _resolve_bold_font_path,
    _resolve_font_path,
    _safe_float,
    _safe_text,
    _text_width,
    _wrap_text,
)

# ============================ 常量 ============================

PAGE_SIZE = (1240, 1754)  # A4 @150dpi
REPORT_TITLE = "个人财务体检报告"
PAGE_COUNT = 3

_MARGIN = 84
_CONTENT_W = PAGE_SIZE[0] - _MARGIN * 2  # 1072

# 商务蓝白
_NAVY = (15, 42, 68)
_BLUE = (29, 78, 216)
_BLUE_SOFT = (219, 234, 254)
_BG = (245, 248, 252)
_CARD = (255, 255, 255)
_BORDER = (214, 227, 240)
_TEXT = (31, 41, 55)
_MUTED = (100, 116, 139)
_WARN = (220, 38, 38)
_GREEN = (22, 163, 74)
_ORANGE = (217, 119, 6)

# 玫瑰图色板（蓝系循环）
_ROSE_COLORS = [
    (29, 78, 216),
    (37, 99, 235),
    (59, 130, 246),
    (96, 165, 250),
    (147, 197, 253),
    (191, 219, 254),
    (30, 58, 138),
    (14, 165, 233),
]

_WATERMARK_TEXT = "明白账"
_FOOTER_NOTE = "本报告由明白账根据你的记账数据生成，仅供参考，不构成投资建议"


# ============================ 通用工具 ============================


def _font(size: int, bold: bool = False):
    """按字号取字体对象（_load_font 已按 (路径, 字号) 缓存）。"""
    regular = _resolve_font_path()
    path = _resolve_bold_font_path(regular) if bold else regular
    try:
        return _load_font(path, size)
    except Exception:  # noqa: BLE001 - 字体异常时退回常规字体，绝不让报告整体失败
        return _load_font(regular, size)


def _rgba(color, alpha: int) -> tuple:
    return (int(color[0]), int(color[1]), int(color[2]), int(alpha))


def _mix(color_a, color_b, ratio: float) -> tuple:
    """两色线性插值（ratio 0 → a，1 → b）。"""
    t = max(0.0, min(1.0, float(ratio)))
    return tuple(
        int(round(color_a[i] + (color_b[i] - color_a[i]) * t)) for i in range(3)
    )


def _section(draw, title: str, subtitle: str = "") -> None:
    """页面小节标题（P1 顶部与各页复用）。"""
    draw.text((_MARGIN, 212), title, font=_font(32, True), fill=_NAVY)
    if subtitle:
        draw.text((_MARGIN, 256), subtitle, font=_font(17), fill=_MUTED)


def _card(draw, box, radius: int = 18, fill=_CARD, outline=_BORDER) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def _bullet_lines(
    draw, text: str, font, x: int, y: int, max_width: float, max_lines: int = 3, fill=_TEXT
) -> int:
    """把一段中文按宽度换行后逐行绘制，返回下一行的 y。"""
    lines = _clip_lines(draw, _wrap_text(draw, text, font, max_width), font, max_width, max_lines)
    step = _line_height(font, 1.42)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += step
    return y


def _pill(draw, x: int, y: int, text: str, font, fill=_BLUE_SOFT, text_fill=_BLUE) -> int:
    """画一个圆角标签，返回右边界 x（便于横排）。"""
    width = int(_text_width(draw, text, font)) + 26
    draw.rounded_rectangle(
        (x, y, x + width, y + 34), radius=17, fill=fill, outline=None
    )
    draw.text((x + 13, y + 17), text, font=font, fill=text_fill, anchor="lm")
    return x + width


# ============================ 页面骨架 ============================


def _new_page() -> Image.Image:
    return Image.new("RGB", PAGE_SIZE, (255, 255, 255))


def _watermark(img: Image.Image) -> Image.Image:
    """斜向重复「明白账」水印：低透明度、不抢内容。"""
    tile = Image.new("RGBA", (520, 320), (0, 0, 0, 0))
    _ImageDraw = ImageDraw.Draw(tile)
    _ImageDraw.text(
        (24, 130), _WATERMARK_TEXT, font=_font(46, True), fill=_rgba(_BLUE, 26)
    )
    tile = tile.rotate(30, expand=True, resample=Image.BICUBIC)

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    step_x, step_y = tile.width + 90, tile.height + 40
    for y in range(-220, img.height + 220, step_y):
        for x in range(-260, img.width + 260, step_x):
            layer.alpha_composite(tile, (x, y))
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    return base.convert("RGB")


def _header(img: Image.Image, payload: dict) -> None:
    draw = ImageDraw.Draw(img)
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    nickname = _safe_text(user.get("nickname"), "同学") or "同学"
    month_label = _safe_text(user.get("month_label"), "") or "本月"
    generated_at = _safe_text(user.get("generated_at"), "") or _dt.datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    draw.text((_MARGIN, 48), "明白账", font=_font(46, True), fill=_NAVY)
    draw.text((_MARGIN + 2, 106), "MINGBAIZHANG", font=_font(14), fill=_MUTED)

    draw.text(
        (PAGE_SIZE[0] - _MARGIN, 52),
        REPORT_TITLE,
        font=_font(26, True),
        fill=_NAVY,
        anchor="ra",
    )
    draw.text(
        (PAGE_SIZE[0] - _MARGIN, 92),
        f"{nickname} · {month_label} · 生成于 {generated_at}",
        font=_font(15),
        fill=_MUTED,
        anchor="ra",
    )
    draw.line((_MARGIN, 168, PAGE_SIZE[0] - _MARGIN, 168), fill=_BORDER, width=2)


def _footer(draw, page_no: int, month_label: str) -> None:
    y = PAGE_SIZE[1] - 66
    draw.line((_MARGIN, y - 18, PAGE_SIZE[0] - _MARGIN, y - 18), fill=_BORDER, width=1)
    draw.text(
        (_MARGIN, y),
        f"明白账 · {REPORT_TITLE} · {month_label or '本月'} · 第 {page_no} / {PAGE_COUNT} 页",
        font=_font(14),
        fill=_MUTED,
    )
    draw.text(
        (PAGE_SIZE[0] - _MARGIN, y),
        _FOOTER_NOTE,
        font=_font(13),
        fill=(148, 163, 184),
        anchor="ra",
    )


def _page_shell(payload: dict, page_no: int) -> tuple:
    """返回 (页面, 页脚用的月份文案)；页眉/水印统一处理。"""
    img = _new_page()
    _watermark(img)
    _header(img, payload)
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    month_label = _safe_text(user.get("month_label"), "")
    return img, month_label


# ============================ 消费 MBTI ============================

_MBTI_TABLE = {
    "ISTJ": ("账本守门员", "每一笔开销都有去处，你的安全感来自可预期的余额。", ("计划", "稳定", "务实")),
    "ISFJ": ("稳稳当当小管家", "愿意为家人和小确幸花钱，月底还能给自己留一点余量。", ("踏实", "顾家", "温柔")),
    "INFJ": ("长期主义隐士", "不太追热闹，更愿意把钱花在真正喜欢的人和事上。", ("长期", "克制", "有主见")),
    "INTJ": ("财务规划师", "先想清楚三年后的目标，再决定今天这杯咖啡值不值。", ("规划", "理性", "目标感")),
    "ISTP": ("实用极简派", "能修就不换、能用就不买，消费清单短得让人佩服。", ("务实", "极简", "手巧")),
    "ISFP": ("小确幸体验家", "愿意为份量和氛围付费，快乐来得很具体。", ("体验", "感官", "当下")),
    "INFP": ("氛围感收藏家", "买的是心情和故事，常常为「一眼心动」买单。", ("氛围", "审美", "理想")),
    "INTP": ("性价比研究员", "下单前先看完测评，几乎不交智商税。", ("研究", "比价", "冷静")),
    "ESTP": ("即时行乐派", "钱要花在当下最爽的那一秒，计划永远赶不上变化。", ("即时", "行动", "爽快")),
    "ESFP": ("快乐星球常驻", "朋友一喊就出门，快乐消费是你的主场。", ("社交", "快乐", "外放")),
    "ENFP": ("热情剁手党", "购物车永远满着，灵感一来就下单。", ("体验优先", "情绪消费", "随缘记账")),
    "ENTP": ("新鲜尝鲜家", "新出的都想试，订阅列表比账单还长。", ("尝鲜", "多元", "脑洞")),
    "ESTJ": ("预算执行官", "预算表就是纪律，超支会让你浑身难受。", ("执行力", "预算", "秩序")),
    "ESFJ": ("请客达人", "人情往来舍得花，朋友开心你也开心。", ("社交", "大方", "人情")),
    "ENFJ": ("生活品质官", "愿意为品质和仪式感买单，也乐于带着朋友一起变好。", ("品质", "仪式感", "带节奏")),
    "ENTJ": ("目标冲刺者", "每一笔钱都要为目标服务，效率是唯一的评判标准。", ("目标", "效率", "冲刺")),
}


def classify_spending_mbti(metrics: dict) -> dict:
    """按消费行为给一个「消费 MBTI」人格（纯函数，缺字段按 0 处理）。

    四轴口径
    --------
    E / I：fun_share ≥ 25% → E（娱乐购物偏多，社交体验型），否则 I（居家刚需型）；
    S / N：saving_rate ≥ 20% → S（能存下钱），否则 N（钱更容易花掉）；
    T / F：record_ratio ≥ 40% → T（记账规律），否则 F（随缘记账）；
    J / P：有预算且本月支出未超 → J（预算内），否则 P（超预算 / 未设预算）。

    metrics 可额外带 ``expense`` / ``budget`` 两个金额字段（app 侧把 balance 合并进来）。
    """
    data = metrics if isinstance(metrics, dict) else {}
    fun_share = max(0.0, min(1.0, _safe_float(data.get("fun_share"))))
    saving_rate = _safe_float(data.get("saving_rate"))
    record_ratio = max(0.0, min(1.0, _safe_float(data.get("record_ratio"))))
    expense = max(0.0, _safe_float(data.get("expense")))
    budget = max(0.0, _safe_float(data.get("budget")))

    e_letter = "E" if fun_share >= 0.25 else "I"
    s_letter = "S" if saving_rate >= 0.2 else "N"
    t_letter = "T" if record_ratio >= 0.4 else "F"
    j_letter = "J" if (budget > 0 and expense <= budget) else "P"
    code = e_letter + s_letter + t_letter + j_letter

    title, desc, tags = _MBTI_TABLE.get(
        code, ("消费观察员", "你的消费画像还在积累中，再记一阵子会更准。", ("记录", "观察", "成长"))
    )

    if e_letter == "E":
        e_basis = f"E：娱乐+购物占比 {fun_share * 100:.0f}%，社交体验型"
    else:
        e_basis = f"I：娱乐+购物仅占 {fun_share * 100:.0f}%，居家刚需型"
    if s_letter == "S":
        s_basis = f"S：结余率 {saving_rate * 100:.0f}%，能存下钱"
    else:
        s_basis = f"N：结余率 {saving_rate * 100:.0f}%，钱更容易花掉"
    if t_letter == "T":
        t_basis = f"T：记账天数占比 {record_ratio * 100:.0f}%，账本清楚"
    else:
        t_basis = f"F：记账天数占比 {record_ratio * 100:.0f}%，随缘记账"
    if j_letter == "J":
        j_basis = f"J：支出 {_money(expense)} 在预算 {_money(budget)} 内"
    elif budget > 0:
        j_basis = f"P：支出 {_money(expense)} 超出预算 {_money(budget)}"
    else:
        j_basis = "P：还没有设置月度预算"

    return {
        "code": code,
        "title": title,
        "desc": desc,
        "tags": list(tags),
        "basis": [e_basis, s_basis, t_basis, j_basis],
    }


# ============================ P1 核心概览 ============================


def _page1(payload: dict) -> Image.Image:
    img, month_label = _page_shell(payload, 1)
    draw = ImageDraw.Draw(img)

    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    balance = payload.get("balance") if isinstance(payload.get("balance"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}

    score = int(max(0, min(100, _safe_float(health.get("score")))))
    grade = _safe_text(health.get("grade"), "") or "—"
    title = _safe_text(health.get("title"), "") or "待评估"
    deductions = [
        _safe_text(item, "")
        for item in (health.get("deductions") or [])
        if _safe_text(item, "")
    ][:4]

    income = max(0.0, _safe_float(balance.get("income")))
    expense = max(0.0, _safe_float(balance.get("expense")))
    net = _safe_float(balance.get("net"), income - expense)
    budget = max(0.0, _safe_float(balance.get("budget")))
    days_elapsed = max(0, int(_safe_float(balance.get("days_elapsed"), 0)))
    daily_avg = expense / days_elapsed if days_elapsed > 0 else 0.0

    _section(draw, "核心概览", "先看结论：健康分、消费人格与本月结余，一页看完。")

    # ---- 1) 健康分 ----
    card = (84, 300, 1156, 690)
    _card(draw, card)

    ring_box = (150, 340, 390, 580)
    ring_color = _GREEN if score >= 80 else (_BLUE if score >= 60 else _ORANGE)
    draw.arc(ring_box, start=-90, end=270, fill=_BORDER, width=24)
    if score > 0:
        draw.arc(ring_box, start=-90, end=-90 + 360 * score / 100.0, fill=ring_color, width=24)
    draw.text((270, 438), str(score), font=_font(88, True), fill=_NAVY, anchor="mm")
    draw.text((270, 512), "分", font=_font(24), fill=_MUTED, anchor="mm")

    draw.text((430, 366), "本月财务健康分", font=_font(26, True), fill=_NAVY)
    draw.text(
        (430, 412),
        f"{grade} 级 · {title}",
        font=_font(22),
        fill=ring_color if score < 60 else _BLUE,
    )
    y = 462
    draw.text((430, y), "主要扣分项", font=_font(16, True), fill=_MUTED)
    y += 34
    if deductions:
        for item in deductions:
            draw.ellipse((430, y + 11, 438, y + 19), fill=_ORANGE)
            y = _bullet_lines(draw, item, _font(17), 452, y, 620, max_lines=1)
            y += 6
    else:
        draw.text((430, y), "本月没有明显扣分项，保持住。", font=_font(17), fill=_GREEN)

    # ---- 2) 消费 MBTI ----
    card = (84, 720, 1156, 1080)
    _card(draw, card)
    mbti_input = dict(metrics)
    mbti_input.setdefault("expense", expense)
    mbti_input.setdefault("budget", budget)
    mbti = classify_spending_mbti(mbti_input)

    draw.text((120, 760), "消费 MBTI", font=_font(16, True), fill=_MUTED)
    code_font = _font(66, True)
    draw.text((120, 796), mbti["code"], font=code_font, fill=_BLUE)
    draw.text((120, 880), "你的消费人格", font=_font(14), fill=_MUTED)

    draw.text((470, 756), mbti["title"], font=_font(30, True), fill=_NAVY)
    _bullet_lines(draw, mbti["desc"], _font(18), 470, 806, 620, max_lines=2, fill=_TEXT)

    tag_x = 470
    for tag in mbti["tags"][:3]:
        tag_x = _pill(draw, tag_x, 872, tag, _font(15)) + 10

    basis_y = 944
    draw.text((120, basis_y), "判断依据", font=_font(14, True), fill=_MUTED)
    basis_y += 28
    basis_font = _font(15)
    for line in mbti["basis"]:
        draw.text((120, basis_y), line, font=basis_font, fill=(71, 85, 105))
        basis_y += _line_height(basis_font, 1.35)
        if basis_y > 1052:
            break

    # ---- 3) 结余大盘 ----
    card = (84, 1110, 1156, 1470)
    _card(draw, card)
    draw.text((120, 1140), "本月结余大盘", font=_font(22, True), fill=_NAVY)

    cards = [
        ("总收入", _money(income), _TEXT),
        ("总支出", _money(expense), _TEXT),
        ("本月结余", _money(net), _WARN if net < 0 else _GREEN),
        ("日均支出", _money(daily_avg), _TEXT),
    ]
    box_w = 236
    gap = 18
    x = 120
    for label, value, color in cards:
        draw.rounded_rectangle(
            (x, 1184, x + box_w, 1318), radius=14, fill=_BG, outline=_BORDER, width=1
        )
        draw.text((x + 18, 1206), label, font=_font(15), fill=_MUTED)
        draw.text((x + 18, 1244), value, font=_font(26, True), fill=color)
        x += box_w + gap

    if net < 0:
        draw.text(
            (120, 1332),
            "本月入不敷出，先稳住现金流，再谈存钱目标。",
            font=_font(16),
            fill=_WARN,
        )
        bar_y = 1382
    else:
        draw.text(
            (120, 1332),
            f"按当前节奏，本月还能留下 {_money(net)}。",
            font=_font(16),
            fill=_MUTED,
        )
        bar_y = 1382

    # 预算使用率
    ratio = expense / budget if budget > 0 else 0.0
    ratio = max(0.0, ratio)
    bar_w = 760
    draw.text((120, bar_y), "预算使用率", font=_font(15), fill=_MUTED)
    draw.rounded_rectangle(
        (240, bar_y + 6, 240 + bar_w, bar_y + 26), radius=10, fill=(235, 241, 248)
    )
    fill_w = int(min(1.0, ratio) * bar_w)
    if fill_w > 0:
        draw.rounded_rectangle(
            (240, bar_y + 6, 240 + fill_w, bar_y + 26),
            radius=10,
            fill=_WARN if ratio > 1 else _BLUE,
        )
    label = f"{ratio * 100:.0f}%" if budget > 0 else "未设置预算"
    draw.text((1012, bar_y), label, font=_font(16, True), fill=_WARN if ratio > 1 else _NAVY, anchor="ra")

    _footer(draw, 1, month_label)
    return img


# ============================ P2 消费透视 ============================


def _draw_rose(draw, categories: list, box) -> None:
    """南丁格尔玫瑰图：等角度、半径 ∝ 金额，外圈分类名。"""
    left, top, right, bottom = box
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    values = [max(0.0, _safe_float(item.get("amount"))) for item in categories]
    total = sum(values)
    if total <= 0:
        draw.rounded_rectangle(box, radius=16, outline=_BORDER, width=2, fill=_BG)
        draw.text(
            ((left + right) // 2, (top + bottom) // 2),
            "本月分类数据不足\n先记几笔分类账单",
            font=_font(18),
            fill=_MUTED,
            anchor="mm",
            align="center",
        )
        return

    max_value = max(values)
    min_radius, max_radius = 74.0, 224.0
    step = 360.0 / max(1, len(values))
    start = -90.0
    for index, item in enumerate(categories):
        value = max(0.0, _safe_float(item.get("amount")))
        radius = min_radius + (max_radius - min_radius) * (value / max_value if max_value else 0)
        bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
        color = _ROSE_COLORS[index % len(_ROSE_COLORS)]
        draw.pieslice(
            bbox,
            start=start + index * step,
            end=start + (index + 1) * step,
            fill=color,
            outline=(255, 255, 255),
            width=2,
        )

    for index, item in enumerate(categories):
        value = max(0.0, _safe_float(item.get("amount")))
        radius = min_radius + (max_radius - min_radius) * (value / max_value if max_value else 0)
        angle = math.radians(start + (index + 0.5) * step)
        label_r = min(radius + 26, 258)
        lx = cx + math.cos(angle) * label_r
        ly = cy + math.sin(angle) * label_r
        name = _safe_text(item.get("name"), "其他") or "其他"
        draw.text(
            (lx, ly),
            name[:4],
            font=_font(14, True),
            fill=_NAVY,
            anchor="mm",
            align="center",
        )


def _draw_heatmap(draw, calendar: dict, box) -> None:
    """日历热力图：周一起始，7 列 × 最多 6 行。"""
    left, top, right, bottom = box
    year = int(_safe_float(calendar.get("year"), _dt.date.today().year))
    month = int(_safe_float(calendar.get("month"), _dt.date.today().month))
    if not 1 <= month <= 12:
        month = _dt.date.today().month
    days_raw = calendar.get("days") if isinstance(calendar.get("days"), dict) else {}
    days: dict[int, float] = {}
    for key, value in days_raw.items():
        try:
            day = int(str(key))
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 31:
            days[day] = max(0.0, _safe_float(value))
    try:
        first_weekday = _dt.date(year, month, 1).weekday()
    except ValueError:
        first_weekday = 0
    max_amount = max([max(0.0, _safe_float(calendar.get("max_amount")))] + list(days.values()))

    cell_w, cell_h, gap = 52, 44, 7
    grid_x = left + 6
    grid_y = top + 30
    week_labels = ("一", "二", "三", "四", "五", "六", "日")
    for col, label in enumerate(week_labels):
        draw.text(
            (grid_x + col * (cell_w + gap) + cell_w / 2, top + 8),
            label,
            font=_font(14, True),
            fill=_MUTED,
            anchor="mm",
        )

    for day in range(1, 32):
        try:
            cell_date = _dt.date(year, month, day)
        except ValueError:
            break
        index = first_weekday + day - 1
        row, col = divmod(index, 7)
        x = grid_x + col * (cell_w + gap)
        y = grid_y + row * (cell_h + gap)
        if y + cell_h > bottom - 40:
            break
        amount = days.get(day, 0.0)
        if amount <= 0:
            fill = _BG
            text_fill = _MUTED
        else:
            ratio = amount / max_amount if max_amount > 0 else 0.0
            fill = _mix(_BLUE_SOFT, _BLUE, ratio)
            text_fill = (255, 255, 255) if ratio >= 0.55 else _NAVY
        draw.rounded_rectangle(
            (x, y, x + cell_w, y + cell_h), radius=8, fill=fill, outline=(255, 255, 255), width=1
        )
        draw.text(
            (x + cell_w / 2, y + cell_h / 2),
            str(day),
            font=_font(14),
            fill=text_fill,
            anchor="mm",
        )

    # 5 档图例
    legend_y = bottom - 26
    draw.text((left + 6, legend_y), "少", font=_font(13), fill=_MUTED)
    for index in range(5):
        ratio = index / 4.0
        lx = left + 34 + index * 30
        draw.rounded_rectangle(
            (lx, legend_y - 2, lx + 20, legend_y + 16),
            radius=4,
            fill=_mix(_BLUE_SOFT, _BLUE, ratio),
        )
    draw.text((left + 34 + 5 * 30 + 4, legend_y), "多", font=_font(13), fill=_MUTED)


def _page2(payload: dict) -> Image.Image:
    img, month_label = _page_shell(payload, 2)
    draw = ImageDraw.Draw(img)

    categories = payload.get("categories") if isinstance(payload.get("categories"), list) else []
    categories = [
        {"name": _safe_text(item.get("name"), "其他") or "其他", "amount": _safe_float(item.get("amount"))}
        for item in categories
        if isinstance(item, dict)
    ]
    categories = sorted(categories, key=lambda item: item["amount"], reverse=True)[:8]
    total = sum(item["amount"] for item in categories)

    merchants = payload.get("merchants") if isinstance(payload.get("merchants"), list) else []
    merchants = [
        {"name": _safe_text(item.get("name"), "未知") or "未知", "amount": _safe_float(item.get("amount"))}
        for item in merchants
        if isinstance(item, dict)
    ][:5]

    calendar = payload.get("calendar") if isinstance(payload.get("calendar"), dict) else {}
    year = int(_safe_float(calendar.get("year"), _dt.date.today().year))
    month = int(_safe_float(calendar.get("month"), _dt.date.today().month))
    days_raw = calendar.get("days") if isinstance(calendar.get("days"), dict) else {}
    active_days = sum(
        1 for value in days_raw.values() if _safe_float(value) > 0
    )
    max_day = max([_safe_float(v) for v in days_raw.values()] + [0.0])

    _section(draw, "消费透视", "钱都花在哪儿了：分类结构、每日节奏与大额商户。")

    # ---- 玫瑰图 ----
    _card(draw, (84, 300, 700, 900))
    draw.text((110, 320), "消费结构 · 玫瑰图", font=_font(19, True), fill=_NAVY)
    _draw_rose(draw, categories, (100, 350, 684, 790))

    if categories and total > 0:
        show = categories[:6]
        legend_y = 810
        for index, item in enumerate(show):
            col = index % 2
            row = index // 2
            lx = 110 + col * 296
            ly = legend_y + row * 26
            color = _ROSE_COLORS[categories.index(item) % len(_ROSE_COLORS)]
            draw.rounded_rectangle((lx, ly + 4, lx + 12, ly + 16), radius=3, fill=color)
            share = item["amount"] / total * 100 if total else 0
            text = f"{item['name'][:5]} {_money(item['amount'])} · {share:.0f}%"
            draw.text((lx + 20, ly), text, font=_font(14), fill=_TEXT)

    # ---- 日历热力图 ----
    _card(draw, (724, 300, 1156, 900))
    draw.text((748, 320), "消费日历 · 每日支出", font=_font(19, True), fill=_NAVY)
    draw.text(
        (1132, 324),
        f"{year}年{month}月",
        font=_font(14),
        fill=_MUTED,
        anchor="ra",
    )
    _draw_heatmap(draw, {"year": year, "month": month, "days": days_raw}, (724, 340, 1156, 850))
    draw.text(
        (748, 862),
        f"本月有 {active_days} 天记账，最高单日 {_money(max_day)}",
        font=_font(14),
        fill=_MUTED,
    )

    # ---- Top 商户 ----
    _card(draw, (84, 930, 1156, 1470))
    draw.text((110, 950), "Top 商户", font=_font(19, True), fill=_NAVY)
    draw.text((1132, 954), "按本月支出金额排序", font=_font(14), fill=_MUTED, anchor="ra")
    if not merchants:
        draw.text((110, 1004), "本月还没有足够的商户数据。", font=_font(16), fill=_MUTED)
    else:
        top_amount = max([item["amount"] for item in merchants] + [1.0])
        y = 1004
        for item in merchants:
            draw.text((110, y), item["name"][:12], font=_font(17), fill=_TEXT)
            draw.text((1000, y), _money(item["amount"]), font=_font(17, True), fill=_NAVY, anchor="ra")
            bar_y = y + 30
            draw.rounded_rectangle((110, bar_y, 1130, bar_y + 10), radius=5, fill=(235, 241, 248))
            width = int((item["amount"] / top_amount) * 1020)
            if width > 0:
                draw.rounded_rectangle((110, bar_y, 110 + width, bar_y + 10), radius=5, fill=_BLUE)
            y += 82
            if y > 1400:
                break

    _footer(draw, 2, month_label)
    return img


# ============================ P3 AI 诊断与建议 ============================


def _draw_line_chart(draw, sandbox: dict, box) -> None:
    """未来 N 个月结余预测：三条线（现行 / 砍掉非必需 / 多花一点）。"""
    left, top, right, bottom = box
    labels = sandbox.get("labels") if isinstance(sandbox.get("labels"), list) else []
    labels = [_safe_text(item, "") for item in labels]
    series_specs = [
        ("current", "现行", _BLUE, "solid"),
        ("cut", "砍掉非必需", _GREEN, "dashed"),
        ("extra", "多花一点", _MUTED, "dotted"),
    ]
    values_by_key = {}
    for key, _, _, _ in series_specs:
        raw = sandbox.get(key) if isinstance(sandbox.get(key), list) else []
        values_by_key[key] = [_safe_float(value) for value in raw]

    count = max([len(labels)] + [len(v) for v in values_by_key.values()])
    if count < 2 or all(not v for v in values_by_key.values()):
        draw.text(
            ((left + right) // 2, (top + bottom) // 2),
            "推演数据不足，先记满一个月再来看看",
            font=_font(16),
            fill=_MUTED,
            anchor="mm",
        )
        return

    if not labels or len(labels) < count:
        labels = labels + [""] * (count - len(labels))

    all_values = [value for values in values_by_key.values() for value in values] or [0.0]
    vmin, vmax = min(all_values), max(all_values)
    if vmax == vmin:
        vmax = vmin + 100.0
    pad = (vmax - vmin) * 0.12
    vmin -= pad
    vmax += pad

    plot_left, plot_right = left + 78, right - 20
    plot_top, plot_bottom = top + 20, bottom - 44

    def to_xy(index: int, value: float) -> tuple:
        x = plot_left + (plot_right - plot_left) * (index / max(1, count - 1))
        y = plot_bottom - (plot_bottom - plot_top) * ((value - vmin) / (vmax - vmin))
        return x, y

    # 网格 + Y 轴标签
    for grid in range(4):
        value = vmin + (vmax - vmin) * grid / 3
        y = to_xy(0, value)[1]
        draw.line((plot_left, y, plot_right, y), fill=(232, 238, 246), width=1)
        draw.text((plot_left - 12, y), f"{value:,.0f}", font=_font(12), fill=_MUTED, anchor="rm")

    # X 轴标签
    for index, label in enumerate(labels[:count]):
        x = to_xy(index, vmin)[0]
        draw.text((x, plot_bottom + 14), label, font=_font(13), fill=_MUTED, anchor="mm")

    def draw_series(values: list, color, style: str) -> None:
        points = [to_xy(index, value) for index, value in enumerate(values)]
        if len(points) < 2:
            return
        if style == "solid":
            draw.line(points, fill=color, width=3)
        elif style == "dashed":
            for index in range(len(points) - 1):
                x1, y1 = points[index]
                x2, y2 = points[index + 1]
                segments = 5
                for seg in range(0, segments, 2):
                    draw.line(
                        (
                            x1 + (x2 - x1) * seg / segments,
                            y1 + (y2 - y1) * seg / segments,
                            x1 + (x2 - x1) * (seg + 1) / segments,
                            y1 + (y2 - y1) * (seg + 1) / segments,
                        ),
                        fill=color,
                        width=2,
                    )
        else:  # dotted
            for index in range(len(points) - 1):
                x1, y1 = points[index]
                x2, y2 = points[index + 1]
                segments = 7
                for seg in range(0, segments, 3):
                    draw.ellipse(
                        (
                            x1 + (x2 - x1) * seg / segments - 1.6,
                            y1 + (y2 - y1) * seg / segments - 1.6,
                            x1 + (x2 - x1) * seg / segments + 1.6,
                            y1 + (y2 - y1) * seg / segments + 1.6,
                        ),
                        fill=color,
                    )
        for x, y in points:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(255, 255, 255), outline=color, width=2)

    for key, _, color, style in series_specs:
        values = values_by_key[key][:count]
        if values:
            draw_series(values, color, style)

    # 图例
    legend_x = right - 330
    for key, name, color, style in series_specs:
        draw.rounded_rectangle((legend_x, top - 2, legend_x + 16, top + 8), radius=3, fill=color)
        draw.text((legend_x + 22, top - 4), name, font=_font(13), fill=_TEXT)
        legend_x += 110


def _page3(payload: dict) -> Image.Image:
    img, month_label = _page_shell(payload, 3)
    draw = ImageDraw.Draw(img)

    ai = payload.get("ai") if isinstance(payload.get("ai"), dict) else {}
    latte = payload.get("latte") if isinstance(payload.get("latte"), dict) else {}
    sandbox = payload.get("sandbox") if isinstance(payload.get("sandbox"), dict) else {}

    daily = max(0.0, _safe_float(latte.get("daily")))
    monthly = max(0.0, _safe_float(latte.get("monthly"), daily * 30))
    yearly = max(0.0, _safe_float(latte.get("yearly"), daily * 365))
    ten_year = max(0.0, _safe_float(latte.get("ten_year"), yearly * 10))
    top_item = _safe_text(latte.get("top_item"), "") or "非必要小额消费"

    _section(draw, "AI 深度诊断", "一页纸说清楚：问题、机会成本、未来推演与下月行动。")

    # ---- 灵魂拷问 ----
    soul = _safe_text(ai.get("soul"), "")
    if not soul:
        soul = (
            f"如果每天少花 {_money(daily)} 在{top_item}上，一年就是 {_money(yearly)}"
            f"——这笔钱，够你认真考虑一次说走就走的旅行了。"
        )
    card = (84, 300, 1156, 540)
    _card(draw, card, fill=(248, 251, 255))
    draw.rectangle((84, 300, 90, 540), fill=_BLUE)
    draw.text((120, 322), "AI 灵魂拷问", font=_font(18, True), fill=_BLUE)
    _bullet_lines(draw, soul, _font(20), 120, 360, 980, max_lines=4, fill=_NAVY)

    # AI 诊断（有就放到卡片底部一行，没有就整行省略）
    diagnosis = _safe_text(ai.get("diagnosis"), "")
    if diagnosis:
        diag_text = f"顾问诊断：{diagnosis}"
        diag_lines = _clip_lines(
            draw, _wrap_text(draw, diag_text, _font(15), 980), _font(15), 980, 1
        )
        draw.text((120, 500), diag_lines[0], font=_font(15), fill=_MUTED)

    # ---- 拿铁因子 ----
    card = (84, 570, 1156, 810)
    _card(draw, card)
    draw.text((110, 592), "拿铁因子 · 机会成本", font=_font(19, True), fill=_NAVY)
    numbers = [
        ("日均", _money(daily)),
        ("月均", _money(monthly)),
        ("年均", _money(yearly)),
        ("10 年复利", _money(ten_year)),
    ]
    box_w = 236
    gap = 18
    x = 110
    for label, value in numbers:
        draw.rounded_rectangle(
            (x, 630, x + box_w, 750), radius=14, fill=_BG, outline=_BORDER, width=1
        )
        draw.text((x + 16, 650), label, font=_font(14), fill=_MUTED)
        draw.text((x + 16, 684), value, font=_font(22, True), fill=_NAVY)
        x += box_w + gap
    draw.text(
        (110, 768),
        "按年化 5% 估算，非承诺收益；减少非必要小额消费，长期差距会很明显。",
        font=_font(13),
        fill=_MUTED,
    )

    # ---- 6 个月预测 ----
    card = (84, 840, 1156, 1290)
    _card(draw, card)
    draw.text((110, 862), "未来 6 个月结余预测", font=_font(19, True), fill=_NAVY)
    draw.text(
        (1132, 866),
        "基于本月收支节奏推演",
        font=_font(13),
        fill=_MUTED,
        anchor="ra",
    )
    _draw_line_chart(draw, sandbox, (100, 900, 1140, 1268))

    # ---- 3 条行动建议 ----
    actions = ai.get("actions") if isinstance(ai.get("actions"), list) else []
    actions = [_safe_text(item, "") for item in actions if _safe_text(item, "")]
    if not actions:
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        milk = max(0.0, _safe_float(metrics.get("milk_tea_share")))
        expense = max(0.0, _safe_float((payload.get("balance") or {}).get("expense")))
        saving = _safe_float(metrics.get("saving_rate"))
        if milk > 0 and expense > 0:
            actions.append(
                f"把奶茶咖啡砍掉一半，按本月占比估算每月约省 {_money(expense * milk / 2)}。"
            )
        if saving < 0.2:
            actions.append("生活费到账当天先转 10% 到单独存钱罐，剩下的再安排开销。")
        if len(actions) < 2:
            actions.append("给本月支出设一个 7 天滚动预算，超了就停一天外卖。")
        if len(actions) < 3:
            actions.append("每周日花 5 分钟对一遍账，把「想不起来」的支出标出来。")
        actions = actions[:3]

    draw.text((84, 1320), "下月 3 条具体行动", font=_font(19, True), fill=_NAVY)
    y = 1360
    for index, action in enumerate(actions[:3], start=1):
        draw.rounded_rectangle((84, y, 1156, y + 78), radius=14, fill=_CARD, outline=_BORDER, width=1)
        draw.ellipse((104, y + 19, 144, y + 59), fill=_BLUE)
        draw.text((124, y + 39), str(index), font=_font(20, True), fill=(255, 255, 255), anchor="mm")
        _bullet_lines(draw, action, _font(18), 168, y + 20, 940, max_lines=2, fill=_TEXT)
        y += 92

    _footer(draw, 3, month_label)
    return img


# ============================ 对外入口 ============================


def _fallback_page(page_no: int, payload: dict) -> Image.Image:
    """单页渲染异常时的兜底：仍是完整一页（页眉页脚俱全），只是正文给提示。"""
    img, month_label = _page_shell(payload, page_no)
    draw = ImageDraw.Draw(img)
    draw.text(
        (PAGE_SIZE[0] // 2, PAGE_SIZE[1] // 2 - 20),
        "这一页的数据暂时不足以生成图表",
        font=_font(24, True),
        fill=_NAVY,
        anchor="mm",
    )
    draw.text(
        (PAGE_SIZE[0] // 2, PAGE_SIZE[1] // 2 + 30),
        "先多记几笔账，再回来生成完整报告",
        font=_font(17),
        fill=_MUTED,
        anchor="mm",
    )
    _footer(draw, page_no, month_label)
    return img


def render_pages(payload: dict) -> list:
    """渲染 3 页 PIL.Image（RGB，1240×1754）。任何异常都退化成兜底页。"""
    data = payload if isinstance(payload, dict) else {}
    builders = (_page1, _page2, _page3)
    pages = []
    for index, builder in enumerate(builders, start=1):
        try:
            page = builder(data)
        except Exception as exc:  # noqa: BLE001 - 单页失败不能拖垮整份报告
            print(f"[report] 第 {index} 页渲染失败：{type(exc).__name__}: {exc}")
            page = _fallback_page(index, data)
        if not isinstance(page, Image.Image):
            page = _fallback_page(index, data)
        if page.size != PAGE_SIZE:
            page = page.resize(PAGE_SIZE)
        pages.append(page.convert("RGB"))
    return pages


def build_financial_report(payload: dict) -> bytes:
    """把 3 页合成一份 PDF（Pillow 多页），返回可直接下载的 bytes。"""
    pages = render_pages(payload)
    buffer = io.BytesIO()
    pages[0].save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=150.0,
    )
    return buffer.getvalue()


def report_available() -> bool:
    """Pillow 能画出中文即可用：模块 import 成功就返回 True。"""
    try:
        _font(16)
        _font(16, bold=True)
    except Exception:  # noqa: BLE001 - 字体缺失时让页面显示「暂不可用」而不是红屏
        return False
    return True