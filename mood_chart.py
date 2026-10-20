# -*- coding: utf-8 -*-
"""情绪消费气象站 · 气泡图（ECharts option 纯函数构造）。

设计口径
--------
· 纯函数：只吃 db.get_mood_spending() 的 moods 列表，不读 session_state、不发请求；
· 一张气泡图同时回答两个问题：
    - 横轴 = 该情绪下的**平均单笔**（客单价，看「贵不贵」）
    - 纵轴 = 该情绪下的**消费总额**（看「总共花多少」）
    - 气泡面积 ∝ 总额（sqrt 映射，避免大额把小额压成一个点）
· 五种情绪固定配色：开心金 / Emo 蓝 / 焦虑红 / 疲惫紫 / 激动粉；
  「高危情绪」描边加粗并标注 ⚠，一眼能找到那个让你钱包破防的情绪；
· 虚线 markLine 落在「全部支出的平均单笔」上，气泡在右侧 = 这种情绪下更舍得花钱。

注意：本模块 import 了 streamlit_echarts，必须在 Streamlit 运行时里导入
（裸 Python 进程 import 会在组件注册处抛错，与 peer_chart.py 同理）。
"""
from streamlit_echarts import JsCode

# 与 db.MOOD_OPTIONS 逐字一致、顺序也一致：两边任一改动都要同步。
MOOD_ORDER = ("开心", "Emo", "焦虑", "疲惫", "激动")

MOOD_EMOJI = {
    "开心": "😊",
    "Emo": "😢",
    "焦虑": "😠",
    "疲惫": "😴",
    "激动": "🤩",
}

MOOD_COLORS = {
    "开心": "#F59E0B",   # 金
    "Emo": "#3B82F6",    # 蓝
    "焦虑": "#EF4444",   # 红
    "疲惫": "#8B5CF6",   # 紫
    "激动": "#EC4899",   # 粉
}
_MOOD_FALLBACK_COLOR = "#64748B"

# tooltip：中文分三段（情绪 / 总额 / 客单价 / 笔数），脏值一律给占位。
TOOLTIP_FORMATTER = JsCode(
    "(params) => {"
    "  const d = params.data || {};"
    "  const v = Array.isArray(d.value) ? d.value : [];"
    "  const money = (x) => (x === null || x === undefined || isNaN(Number(x)))"
    "    ? '-' : '¥' + Number(x).toFixed(2);"
    "  return String(d.name || '') + '<br/>'"
    "    + '消费总额：' + money(v[1]) + '<br/>'"
    "    + '平均单笔：' + money(v[0]) + '<br/>'"
    "    + '笔数：' + (d.count === undefined ? '-' : String(d.count));"
    "}"
)


def _number(value, default: float = 0.0) -> float:
    """把脏值收敛成有限 float：None / 字符串数字 / NaN / inf 都能兜住。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _bubble_size(total: float, max_total: float) -> float:
    """气泡直径 ∝ sqrt(总额)，最小 26、最大 78：小额度也看得见，大额度不撑爆画布。"""
    if max_total <= 0:
        return 26.0
    ratio = max(0.0, min(1.0, total / max_total))
    return round(26.0 + 52.0 * (ratio ** 0.5), 1)


def build_mood_bubble_option(
    moods: list,
    *,
    baseline_avg: float = 0.0,
    total_expense: float = 0.0,
    months: int = 3,
) -> dict:
    """拼「情绪 × 金额」气泡图 option。

    参数
    ----
    moods          : [{"mood","total","count","avg","share","avg_ratio","high_risk"}, ...]
    baseline_avg   : 全部支出的平均单笔（画一条竖直虚线做参照）
    total_expense  : 统计窗口内的支出总额（只进副标题，不参与坐标）
    months         : 统计窗口月数（只进副标题）

    结构不对时抛 ValueError（调用方降级成一行提示）；单条脏数据跳过，不整体失败。
    """
    if not isinstance(moods, list):
        raise ValueError("情绪气泡图需要 moods 列表")

    items = []
    for raw in moods:
        if not isinstance(raw, dict):
            continue
        mood = str(raw.get("mood") or "").strip()
        if not mood:
            continue
        total = max(0.0, _number(raw.get("total")))
        count = int(max(0, _number(raw.get("count"))))
        avg = max(0.0, _number(raw.get("avg")))
        if total <= 0 and count <= 0:
            continue
        emoji = MOOD_EMOJI.get(mood, "")
        color = MOOD_COLORS.get(mood, _MOOD_FALLBACK_COLOR)
        items.append(
            {
                "name": f"{emoji} {mood}".strip(),
                "value": [round(avg, 2), round(total, 2)],
                "count": count,
                "total": round(total, 2),
                "avg": round(avg, 2),
                "high_risk": bool(raw.get("high_risk")),
                "color": color,
            }
        )

    # 高危情绪排在最后画（后画的在上层），保证 ⚠ 不被普通气泡盖住
    items.sort(key=lambda item: item["total"], reverse=True)
    items.sort(key=lambda item: 1 if item["high_risk"] else 0)

    max_total = max([item["total"] for item in items] + [0.0])
    baseline = max(0.0, _number(baseline_avg))

    series_data = []
    for item in items:
        series_data.append(
            {
                "name": item["name"],
                "value": item["value"],
                "count": item["count"],
                "total": item["total"],
                "symbolSize": _bubble_size(item["total"], max_total),
                "itemStyle": {
                    "color": item["color"],
                    "opacity": 0.78,
                    "borderColor": "#EF4444" if item["high_risk"] else "rgba(255,255,255,0.9)",
                    "borderWidth": 3 if item["high_risk"] else 1,
                    "shadowBlur": 14 if item["high_risk"] else 6,
                    "shadowColor": "rgba(15, 23, 42, 0.45)",
                },
                "label": {
                    "show": True,
                    "position": "top",
                    "formatter": ("⚠ {b}" if item["high_risk"] else "{b}"),
                    "color": "#DC2626" if item["high_risk"] else "#334155",
                    "fontSize": 12,
                    "fontWeight": "bold" if item["high_risk"] else "normal",
                },
            }
        )

    option = {
        "backgroundColor": "transparent",
        "title": {
            "text": "情绪 × 金额",
            "subtext": (
                f"近 {int(months)} 个月支出 ¥{_number(total_expense):.2f} · "
                "气泡越大花得越多 · 越靠右单笔越贵"
            ),
            "left": 8,
            "top": 4,
            "textStyle": {"color": "#0F172A", "fontSize": 15},
            "subtextStyle": {"color": "#64748B", "fontSize": 12},
        },
        "tooltip": {
            "trigger": "item",
            "formatter": TOOLTIP_FORMATTER,
            "backgroundColor": "rgba(15, 23, 42, 0.92)",
            "borderColor": "rgba(148, 163, 184, 0.35)",
            "textStyle": {"color": "#e2e8f0", "fontSize": 12},
        },
        "grid": {"left": 78, "right": 34, "top": 86, "bottom": 62},
        "xAxis": {
            "type": "value",
            "name": "平均单笔（元）",
            "nameLocation": "middle",
            "nameGap": 30,
            "nameTextStyle": {"color": "#64748B", "fontSize": 12},
            "axisLabel": {"color": "#64748B", "fontSize": 11},
            "axisLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.45)"}},
            "splitLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.16)"}},
            "min": 0,
        },
        "yAxis": {
            "type": "value",
            "name": "消费总额（元）",
            "nameTextStyle": {"color": "#64748B", "fontSize": 12},
            "axisLabel": {"color": "#64748B", "fontSize": 11},
            "axisLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.45)"}},
            "splitLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.16)"}},
            "min": 0,
        },
        "series": [
            {
                "name": "情绪消费",
                "type": "scatter",
                "data": series_data,
                "emphasis": {"scale": 1.12},
                "markLine": (
                    {
                        "silent": True,
                        "symbol": "none",
                        "lineStyle": {
                            "type": "dashed",
                            "color": "rgba(100, 116, 139, 0.55)",
                        },
                        "label": {
                            "formatter": f"平均单笔 ¥{baseline:.2f}",
                            "color": "#64748B",
                            "fontSize": 11,
                            "position": "end",
                        },
                        "data": [{"xAxis": round(baseline, 2)}],
                    }
                    if baseline > 0
                    else {}
                ),
            }
        ],
    }

    if not series_data:
        option["graphic"] = [
            {
                "type": "text",
                "left": "center",
                "top": "middle",
                "style": {
                    "text": "还没有带情绪标签的账单\n记账时顺手选一个情绪，这里就会亮起来",
                    "fill": "#64748B",
                    "fontSize": 14,
                    "lineHeight": 24,
                    "align": "center",
                },
            }
        ]

    return option


def format_money(value) -> str:
    """页面 caption 用的金额格式化：脏值给 ¥0.00，绝不出现 nan。"""
    return f"¥{_number(value):,.2f}"