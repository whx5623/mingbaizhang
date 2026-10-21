# -*- coding: utf-8 -*-
"""个人资产负债表 · 图表 option 纯函数（ECharts）。

设计口径
--------
· 两张图分工明确：
    1) 资产负债总览柱状图 —— 现金结余 / 资产残值 / 待还负债 / 当前净资产，
       前三根是「体检指标」，最后一根是「结论」，用深蓝单独强调；
    2) 单件资产折旧曲线 —— 未来 36 个月每月残值，回答「这台电脑明年还值多少」。
· 纯函数：只吃 db.get_asset_overview() / asset.curve 的结构，不读 session_state、
  不发请求；脏值一律收敛成 0，绝不抛异常（结构完全不对才抛 ValueError）。
· 浅色主题：坐标轴 #64748B、标题 #0F172A，与「明白账」的 #F8F9FA 背景一致。

注意：本模块 import 了 streamlit_echarts，必须在 Streamlit 运行时里导入
（与 peer_chart.py / mood_chart.py 同理）。
"""
from streamlit_echarts import JsCode

MONEY_COLOR = "#1E88E5"        # 现金结余
ASSET_COLOR = "#16A34A"        # 资产残值
LIABILITY_COLOR = "#EF4444"    # 待还负债
NET_COLOR = "#0F2A44"          # 当前净资产（结论色）
_ASSET_LINE_COLOR = "#1D4ED8"
_AXIS_COLOR = "#64748B"
_TEXT_COLOR = "#334155"
_TITLE_COLOR = "#0F172A"

BALANCE_TOOLTIP = JsCode(
    "(params) => {"
    "  const p = Array.isArray(params) ? params[0] : params;"
    "  const v = Number(p.value);"
    "  return String(p.name || '') + '：' "
    "    + (isNaN(v) ? '-' : (v < 0 ? '-¥' : '¥') + Math.abs(v).toFixed(2));"
    "}"
)

CURVE_TOOLTIP = JsCode(
    "(params) => {"
    "  const p = Array.isArray(params) ? params[0] : params;"
    "  const v = Number(p.value);"
    "  return String(p.name || '') + '<br/>预计残值：' "
    "    + (isNaN(v) ? '-' : '¥' + v.toFixed(2));"
    "}"
)


def _num(value, default: float = 0.0) -> float:
    """脏值 → 有限 float：None / 字符串数字 / NaN / inf 都兜住。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def format_money(value) -> str:
    """页面 caption / tooltip 用：整元带千分位，脏值给 ¥0。"""
    return f"¥{_num(value):,.2f}"


def build_balance_option(overview: dict, *, today_label: str = "") -> dict:
    """资产负债总览：四根横向柱（现金 / 资产残值 / 待还负债 / 净资产）。

    overview 只用到 cash_balance / assets_residual / liabilities / net_worth；
    缺字段按 0 处理。不是 dict 时抛 ValueError（调用方降级成 caption）。
    """
    if not isinstance(overview, dict):
        raise ValueError("资产负债表需要 overview dict")

    cash = _num(overview.get("cash_balance"))
    assets = _num(overview.get("assets_residual"))
    liabilities = _num(overview.get("liabilities"))
    net = _num(overview.get("net_worth"))

    names = ["现金结余", "资产残值", "待还负债", "当前净资产"]
    values = [round(cash, 2), round(assets, 2), round(liabilities, 2), round(net, 2)]
    # ECharts 横向柱从下往上画，反转一次让「现金」在最上面，阅读顺序与中文一致
    names.reverse()
    values.reverse()
    colors = [NET_COLOR, LIABILITY_COLOR, ASSET_COLOR, MONEY_COLOR]

    return {
        "backgroundColor": "transparent",
        "title": {
            "text": "资产 vs 负债",
            "subtext": ("按当前残值与待还估算" + (f" · {today_label}" if today_label else "")),
            "left": 8,
            "top": 4,
            "textStyle": {"color": _TITLE_COLOR, "fontSize": 15},
            "subtextStyle": {"color": _AXIS_COLOR, "fontSize": 12},
        },
        "tooltip": {
            "trigger": "item",
            "formatter": BALANCE_TOOLTIP,
            "backgroundColor": "rgba(15, 23, 42, 0.92)",
            "borderColor": "rgba(148, 163, 184, 0.35)",
            "textStyle": {"color": "#e2e8f0", "fontSize": 12},
        },
        "grid": {"left": 86, "right": 46, "top": 76, "bottom": 28},
        "xAxis": {
            "type": "value",
            "axisLabel": {"color": _AXIS_COLOR, "fontSize": 11},
            "axisLine": {"show": False},
            "splitLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.14)"}},
        },
        "yAxis": {
            "type": "category",
            "data": names,
            "axisLabel": {"color": _TEXT_COLOR, "fontSize": 13},
            "axisLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.35)"}},
            "axisTick": {"show": False},
        },
        "series": [
            {
                "type": "bar",
                "barWidth": 26,
                "data": [
                    {
                        "value": value,
                        "name": name,
                        "itemStyle": {
                            "color": color,
                            "borderRadius": [0, 6, 6, 0],
                            "opacity": 0.9,
                        },
                        "label": {
                            "show": True,
                            "position": "right",
                            "formatter": ("-¥{c}" if value < 0 else "¥{c}"),
                            "color": color,
                            "fontSize": 12,
                            "fontWeight": "bold",
                        },
                    }
                    for name, value, color in zip(names, values, colors)
                ],
                "labelLayout": {"hideOverlap": True},
            }
        ],
    }


def build_depreciation_option(asset: dict, *, months: int = 36) -> dict:
    """单件资产未来 months 个月的残值曲线。

    asset 需要：name / purchase_price / depreciation_rate / curve
    （curve = [{"month": "2026-10", "value": 4015.2}, ...]，由 db 计算）。
    curve 为空时画空态引导；不是 dict 时抛 ValueError。
    """
    if not isinstance(asset, dict):
        raise ValueError("折旧曲线需要单件资产 dict")

    name = str(asset.get("name") or "未命名资产").strip() or "未命名资产"
    price = _num(asset.get("purchase_price"))
    rate = _num(asset.get("depreciation_rate"))
    curve = asset.get("curve")
    points = []
    if isinstance(curve, list):
        for item in curve:
            if not isinstance(item, dict):
                continue
            month = str(item.get("month") or "").strip()
            if not month:
                continue
            points.append({"name": month, "value": round(_num(item.get("value")), 2)})

    option = {
        "backgroundColor": "transparent",
        "title": {
            "text": f"{name} · 未来 {int(months)} 个月残值",
            "subtext": f"购入价 {format_money(price)} · 年折旧 {rate * 100:.1f}% · 直线法",
            "left": 8,
            "top": 4,
            "textStyle": {"color": _TITLE_COLOR, "fontSize": 15},
            "subtextStyle": {"color": _AXIS_COLOR, "fontSize": 12},
        },
        "tooltip": {
            "trigger": "axis",
            "formatter": CURVE_TOOLTIP,
            "backgroundColor": "rgba(15, 23, 42, 0.92)",
            "borderColor": "rgba(148, 163, 184, 0.35)",
            "textStyle": {"color": "#e2e8f0", "fontSize": 12},
        },
        "grid": {"left": 74, "right": 34, "top": 82, "bottom": 56},
        "xAxis": {
            "type": "category",
            "data": [point["name"] for point in points],
            "axisLabel": {"color": _AXIS_COLOR, "fontSize": 10, "interval": 5},
            "axisLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.35)"}},
            "axisTick": {"show": False},
            "boundaryGap": False,
        },
        "yAxis": {
            "type": "value",
            "name": "残值（元）",
            "nameTextStyle": {"color": _AXIS_COLOR, "fontSize": 11},
            "axisLabel": {"color": _AXIS_COLOR, "fontSize": 11},
            "splitLine": {"lineStyle": {"color": "rgba(100, 116, 139, 0.14)"}},
            "min": 0,
        },
        "series": [
            {
                "name": name,
                "type": "line",
                "smooth": True,
                "symbol": "circle",
                "symbolSize": 4,
                "showSymbol": False,
                "lineStyle": {"width": 3, "color": _ASSET_LINE_COLOR},
                "itemStyle": {"color": _ASSET_LINE_COLOR},
                "areaStyle": {
                    "color": {
                        "type": "linear",
                        "x": 0, "y": 0, "x2": 0, "y2": 1,
                        "colorStops": [
                            {"offset": 0, "color": "rgba(29, 78, 216, 0.28)"},
                            {"offset": 1, "color": "rgba(29, 78, 216, 0.02)"},
                        ],
                    }
                },
                "data": [point["value"] for point in points],
                "markLine": (
                    {
                        "silent": True,
                        "symbol": "none",
                        "lineStyle": {"type": "dashed", "color": "rgba(100, 116, 139, 0.55)"},
                        "label": {"color": _AXIS_COLOR, "fontSize": 11},
                        "data": [{"yAxis": 0}],
                    }
                    if points
                    else {}
                ),
            }
        ],
    }

    if not points:
        option["graphic"] = [
            {
                "type": "text",
                "left": "center",
                "top": "middle",
                "style": {
                    "text": "这台资产还没有折旧曲线\n先添加一件资产，这里就会亮起来",
                    "fill": _AXIS_COLOR,
                    "fontSize": 14,
                    "lineHeight": 24,
                    "align": "center",
                },
            }
        ]

    return option