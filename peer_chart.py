# -*- coding: utf-8 -*-
"""同龄人财务基准 · 雷达图（ECharts option 纯函数构造）。

设计口径
--------
· 纯函数：只吃两组成绩（我的 / 全站平均），不读 session_state、不发请求，
  单测里可以直接断言 option；
· 四个维度固定为「消费克制力、饮食健康度、结余能力、理财意识」，满分 100，
  与 db.score_radar 的输出一一对应；缺维度按 0 处理，绝不抛错；
· 「我的」用蓝色实线 + 半透明面积，「全站平均」用橙色虚线，两条叠在同一张
  雷达上 —— 面积差越大，视觉冲击越强（这就是「同龄人都在怎么花钱」的看点）；
· 维度名用浅灰白，深色看板背景下才看得清。

注意：本模块 import 了 streamlit_echarts，必须在 Streamlit 运行时里导入
（裸 Python 进程 import 会在组件注册处抛错，属于该包的运行时依赖，不是本模块的问题）。
"""
from streamlit_echarts import JsCode

# 与 db.RADAR_DIMENSIONS 逐字一致、顺序也一致：两边任一改动都要同步。
PEER_DIMENSIONS = ("消费克制力", "饮食健康度", "结余能力", "理财意识")

MY_COLOR = "#1E88E5"      # 我的得分：蓝
PEER_COLOR = "#F59E0B"    # 全站平均：橙
_PEER_FALLBACK_COLOR = "#64748B"
_RADAR_MAX = 100.0

# tooltip 数值：整数展示（得分本来就是 0~100 的整数），缺值给占位，绝不出现 NaN。
TOOLTIP_VALUE_FORMATTER = JsCode(
    "(value) => (value === null || value === undefined || isNaN(Number(value)))"
    " ? '-' : Number(value).toFixed(0) + ' 分'"
)


def _clamp(value) -> float:
    """成绩收敛到 [0, 100]；非数字 / NaN / inf 一律 0。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(_RADAR_MAX, number))


def _scores_dict(raw) -> dict:
    """把传入的成绩收敛成 {维度: float}；不是 dict 就整体当空。"""
    if not isinstance(raw, dict):
        return {}
    return {dim: _clamp(raw.get(dim)) for dim in PEER_DIMENSIONS}


def _radar_series(name: str, values: list[float], color: str, *, dashed: bool = False) -> dict:
    """一条雷达系列：线宽 2、半透明面积、圆点；虚线用于「均值」以示参照。"""
    line_style = {"width": 2, "color": color}
    if dashed:
        line_style["type"] = "dashed"
    return {
        "name": name,
        "type": "radar",
        "symbolSize": 6,
        "lineStyle": line_style,
        "itemStyle": {"color": color},
        "areaStyle": {"color": color, "opacity": 0.18 if dashed else 0.35},
        "data": [{"value": values, "name": name}],
    }


def build_radar_option(
    mine: dict,
    peer: dict,
    *,
    peer_label: str = "全站平均",
    user_count: int = 0,
) -> dict:
    """拼「我的得分 vs 全站平均得分」的重叠雷达图 option。

    参数
    ----
    mine        : {"消费克制力": 62.0, ...}（db.score_radar 的输出）
    peer        : 同上，全站平均得分
    peer_label  : 均值系列图例名（默认「全站平均」，可传「全站平均（2 人）」）
    user_count  : 样本人数，仅写进图例/提示，不参与计算

    结构不对（两个都不是 dict）时抛 ValueError，由调用方降级成一句提示；
    单个维度缺失只按 0 分处理，不影响出图。
    """
    if not isinstance(mine, dict) or not isinstance(peer, dict):
        raise ValueError("雷达图需要 mine / peer 两组 dict 成绩")

    mine_scores = _scores_dict(mine)
    peer_scores = _scores_dict(peer)
    mine_values = [round(mine_scores[dim], 1) for dim in PEER_DIMENSIONS]
    peer_values = [round(peer_scores[dim], 1) for dim in PEER_DIMENSIONS]

    legend_name = str(peer_label or "全站平均").strip() or "全站平均"
    if user_count and int(user_count) > 0:
        legend_name = f"{legend_name}（{int(user_count)} 人）"

    return {
        "backgroundColor": "transparent",
        "tooltip": {
            "trigger": "item",
            "valueFormatter": TOOLTIP_VALUE_FORMATTER,
            "backgroundColor": "rgba(15, 23, 42, 0.92)",
            "borderColor": "rgba(148, 163, 184, 0.35)",
            "textStyle": {"color": "#e2e8f0", "fontSize": 12},
        },
        "legend": {
            "bottom": 4,
            "itemGap": 18,
            "icon": "roundRect",
            "textStyle": {"color": "#cbd5e1", "fontSize": 12},
        },
        "radar": {
            "center": ["50%", "54%"],
            "radius": "62%",
            "splitNumber": 4,
            "shape": "polygon",
            "indicator": [{"name": dim, "max": 100} for dim in PEER_DIMENSIONS],
            "axisName": {"color": "#c9d3e0", "fontSize": 12},
            "axisLine": {"lineStyle": {"color": "rgba(148, 163, 184, 0.35)"}},
            "splitLine": {"lineStyle": {"color": "rgba(148, 163, 184, 0.22)"}},
            "splitArea": {
                "areaStyle": {
                    "color": ["rgba(148, 163, 184, 0.04)", "rgba(148, 163, 184, 0.10)"]
                }
            },
        },
        "series": [
            _radar_series("我的得分", mine_values, MY_COLOR),
            _radar_series(legend_name, peer_values, PEER_COLOR, dashed=True),
        ],
    }


def format_score(value) -> str:
    """成绩展示：整数分；脏值给「--」。页面 caption 用。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if number != number or number in (float("inf"), float("-inf")):
        return "--"
    return f"{max(0.0, min(_RADAR_MAX, number)):.0f}"