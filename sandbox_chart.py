# -*- coding: utf-8 -*-
"""财务沙盘推演 · 多情景对比折线图（ECharts option 纯函数构造）。

设计口径
--------
· 纯函数：只吃 db.simulate_sandbox_scenarios 的返回，不读 session_state、
  不发请求，所以可以在单测里直接断言 option（系列、颜色、坐标轴、tooltip）；
· 三条线颜色按情景 key 固定（保守绿 / 现行蓝 / 激进红），换月数也不变；
· tooltip 的「¥」格式化必须走 JsCode：streamlit-echarts 只把 JsCode 包装过的
  字符串还原成前端函数（包内先用 "--x_x--0_0--" 占位、再用 new Function 求值），
  普通 JSON 字符串不会被当成函数执行；
· 0 元参考线只挂在「现行」一条系列上，免得三条虚线叠在一起。

注意：本模块 import 了 streamlit_echarts，必须在 Streamlit 运行时里导入
（裸 Python 进程 import 会在组件注册处抛错，属于该包的运行时依赖，不是本模块的问题）。
"""
from streamlit_echarts import JsCode

# 情景 key → 线色。与 db.SANDBOX_SCENARIO_SPECS 的 key 对应；
# 自定义情景（specs 里另起的 key）回落到灰色，不影响出图。
SANDBOX_SCENARIO_COLORS = {
    "conservative": "#10B981",
    "current": "#1E88E5",
    "aggressive": "#EF4444",
}
_FALLBACK_COLOR = "#64748B"

# tooltip 金额：¥1200.00；某档缺值（null / 非数字）时给占位，绝不显示 NaN。
TOOLTIP_VALUE_FORMATTER = JsCode(
    "(value) => (value === null || value === undefined || isNaN(Number(value)))"
    " ? '-' : '¥' + Number(value).toFixed(2)"
)


def build_sandbox_option(scenarios: dict) -> dict:
    """把 simulate_sandbox_scenarios 的返回拼成「三条情景折线」的 ECharts option。

    入参结构（与 db.simulate_sandbox_scenarios 严格一致）：
        scenarios["months"]    : ["2026-11", ...] 三档共享的 x 轴标签
        scenarios["scenarios"] : [{"key", "label", "months": [{"balance"}, ...]}, ...]
        scenarios["current"]   : key="current" 的那一档（主指标 / 0 元参考线从它取）

    结构不对时抛 ValueError，由调用方降级成一句页面提示；本函数不做任何 IO。
    """
    if not isinstance(scenarios, dict):
        raise ValueError("scenarios 必须是 dict")
    labels = scenarios.get("months")
    items = scenarios.get("scenarios")
    if not isinstance(labels, list) or not labels:
        raise ValueError("scenarios 缺少 months 标签")
    if not isinstance(items, list) or not items:
        raise ValueError("scenarios 缺少情景数据")

    current = scenarios.get("current")
    current_key = str(current.get("key") or "current") if isinstance(current, dict) else "current"

    series = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"情景条目不是 dict：{item!r}")
        rows = item.get("months")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"情景 {item.get('key')!r} 缺少逐月数据")
        key = str(item.get("key") or "")
        label = str(item.get("label") or key or "情景")
        color = SANDBOX_SCENARIO_COLORS.get(key, _FALLBACK_COLOR)
        # 只取 balance（float，db 侧已 round2），保持 JSON 可序列化；缺值留 None 由前端占位。
        data = [
            row.get("balance") if isinstance(row, dict) else None
            for row in rows
        ]
        series.append({
            "name": label,
            "type": "line",
            "smooth": True,
            "symbol": "circle",
            "symbolSize": 7,
            "z": 3 if key == current_key else 2,
            "lineStyle": {
                "width": 3 if key == current_key else 2,
                "color": color,
            },
            "itemStyle": {"color": color},
            "emphasis": {"focus": "series"},
            "data": data,
        })

    # 0 元参考线：挂在现行情景上；自定义 specs 没有 current 时退回第一条。
    mark_index = next(
        (i for i, item in enumerate(items) if str(item.get("key") or "") == current_key),
        0,
    )
    series[mark_index]["markLine"] = {
        "silent": True,
        "symbol": "none",
        "lineStyle": {"color": "#EF4444", "type": "dashed", "width": 1},
        "label": {
            "formatter": "0 元",
            "position": "insideEndTop",
            "color": "#EF4444",
            "fontSize": 11,
        },
        "data": [{"yAxis": 0}],
    }

    return {
        # 主题兜底：即使某个 key 没命中 SANDBOX_SCENARIO_COLORS，也不会出现同色线。
        "color": [
            SANDBOX_SCENARIO_COLORS["conservative"],
            SANDBOX_SCENARIO_COLORS["current"],
            SANDBOX_SCENARIO_COLORS["aggressive"],
        ],
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "line", "lineStyle": {"color": "#CBD5E1"}},
            "valueFormatter": TOOLTIP_VALUE_FORMATTER,
            "backgroundColor": "rgba(255, 255, 255, 0.96)",
            "borderColor": "#E2E8F0",
            "textStyle": {"color": "#334155"},
        },
        "legend": {
            "top": 0,
            "left": "center",
            "icon": "roundRect",
            "itemWidth": 14,
            "itemHeight": 8,
            "textStyle": {"color": "#4B5563", "fontSize": 12},
        },
        "grid": {
            "left": "3%",
            "right": "4%",
            "bottom": "3%",
            "top": 48,
            "containLabel": True,
        },
        "xAxis": {
            "type": "category",
            "data": [str(label) for label in labels],
            "boundaryGap": False,
            "axisTick": {"show": False},
            "axisLine": {"lineStyle": {"color": "#E2E8F0"}},
            "axisLabel": {"color": "#6B7280"},
        },
        "yAxis": {
            "type": "value",
            "name": "结余（元）",
            "nameTextStyle": {"color": "#6B7280"},
            "axisLabel": {"color": "#6B7280"},
            "splitLine": {"lineStyle": {"color": "#F1F5F9"}},
        },
        "series": series,
    }