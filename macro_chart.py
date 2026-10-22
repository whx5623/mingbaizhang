"""全息数据大屏的纯函数拼装层（ECharts option / 内嵌 HTML）。

设计约束（与并行开发约定一致）：
- 不 import streamlit、不读数据库、不联网，只在内存里拼 dict / str；
- 所有输入都按「脏数据」防御：非 dict、NaN、inf、字符串、None 一律降级成 0，
  任何情况下都不向调用方抛异常（前端页面绝不能因为一份脏数据红屏）；
- 返回的 option 只含 JSON 可序列化内容，不塞 JS 函数字符串
  （streamlit-echarts 不会执行函数字符串，塞了只会变成一段死文本）；
- HTML 片段里所有动态值都做转义：数字经数值清洗，文本走 html.escape。
"""

from __future__ import annotations

import html
import json
import math

# ===== 暗黑科技风配色（全站统一，app.py 的 CSS 也沿用同一套色板）=====
DARK_BG = "#04070F"
CYAN = "#22D3EE"
PURPLE = "#A78BFA"
BLUE = "#3B82F6"
TEXT = "#E2E8F0"
MUTED = "#7C8CA6"
GRID = "rgba(148,163,184,.16)"

# 分类顺序固定：地图/对比图/图例都按这个顺序读，UI 才不会跳来跳去。
CATEGORY_ORDER = ("餐饮", "奶茶咖啡", "购物", "交通", "娱乐", "学习", "医疗", "其他")

# 水球三层水波：同一色系的三种透明度（越靠里越淡），形成液体层叠感。
_LAYER_ALPHAS = (0.88, 0.62, 0.42)


def _number(value, default=0.0):
    """归一成有限 float：bool / 字符串 / None / NaN / inf 一律退回 default。"""
    if isinstance(value, bool):
        return default
    if not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _share_percent(value):
    """0~1 的分类占比 → 0~100 的百分比（保留 1 位），非法值按 0。"""
    ratio = _number(value, 0.0)
    if ratio <= 0.0:
        return 0.0
    return round(min(ratio * 100.0, 100.0), 1)


def _hex_to_rgb(color):
    """'#RRGGBB' → (r, g, b)；非法颜色退回青色，保证 rgba() 永远可拼。"""
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (34, 211, 238)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (34, 211, 238)


def _rgba(color, alpha):
    """十六进制色 + 透明度 → rgba() 字符串（ECharts 与 CSS 都能直接用）。"""
    r, g, b = _hex_to_rgb(color)
    return f"rgba({r},{g},{b},{max(0.0, min(float(alpha), 1.0)):.3f})"


def _bar_gradient(color):
    """柱子的竖向渐变：顶部实色、底部半透明，暗色背景上更像发光管。"""
    return {
        "type": "linear",
        "x": 0,
        "y": 0,
        "x2": 0,
        "y2": 1,
        "colorStops": [
            {"offset": 0, "color": color},
            {"offset": 1, "color": _rgba(color, 0.28)},
        ],
    }


def build_compare_option(stats: dict) -> dict:
    """「我的消费分布 vs 全站平均」双柱状图的 ECharts option。

    数值口径：category_shares 是 0~1 的占比，展示时 ×100 并保留 1 位小数。
    只保留「我或全站平均任一 > 0」的分类，顺序固定为 CATEGORY_ORDER。
    """
    me_block = stats.get("me") if isinstance(stats, dict) else None
    global_block = stats.get("global") if isinstance(stats, dict) else None

    me_shares = me_block.get("category_shares") if isinstance(me_block, dict) else None
    global_shares = (
        global_block.get("category_shares") if isinstance(global_block, dict) else None
    )
    me_map = me_shares if isinstance(me_shares, dict) else {}
    global_map = global_shares if isinstance(global_shares, dict) else {}

    categories = []
    me_values = []
    avg_values = []
    for category in CATEGORY_ORDER:
        mine = _share_percent(me_map.get(category))
        average = _share_percent(global_map.get(category))
        if mine <= 0.0 and average <= 0.0:
            continue
        categories.append(category)
        me_values.append(mine)
        avg_values.append(average)

    me_series = {
        "name": "我",
        "type": "bar",
        "data": me_values,
        "barWidth": 18,
        "barGap": "18%",
        "itemStyle": {
            "borderRadius": [7, 7, 0, 0],
            "color": _bar_gradient(CYAN),
            "shadowBlur": 18,
            "shadowColor": _rgba(CYAN, 0.65),
            "shadowOffsetY": 4,
        },
        "emphasis": {
            "focus": "series",
            "itemStyle": {
                "shadowBlur": 30,
                "shadowColor": _rgba(CYAN, 0.92),
                "borderColor": "#FFFFFF",
                "borderWidth": 1,
            },
        },
    }
    average_series = {
        "name": "全站平均",
        "type": "bar",
        "data": avg_values,
        "barWidth": 18,
        "barGap": "18%",
        "itemStyle": {
            "borderRadius": [7, 7, 0, 0],
            "color": _bar_gradient(PURPLE),
            "shadowBlur": 18,
            "shadowColor": _rgba(PURPLE, 0.65),
            "shadowOffsetY": 4,
        },
        "emphasis": {
            "focus": "series",
            "itemStyle": {
                "shadowBlur": 30,
                "shadowColor": _rgba(PURPLE, 0.92),
                "borderColor": "#FFFFFF",
                "borderWidth": 1,
            },
        },
    }

    return {
        "backgroundColor": "rgba(0,0,0,0)",
        "animationDuration": 700,
        "animationEasing": "cubicOut",
        "textStyle": {"color": TEXT, "fontFamily": "Segoe UI, PingFang SC, Microsoft YaHei"},
        "grid": {"left": 16, "right": 22, "top": 72, "bottom": 14, "containLabel": True},
        "legend": {
            "data": ["我", "全站平均"],
            "right": 18,
            "top": 16,
            "itemWidth": 14,
            "itemHeight": 10,
            "itemGap": 18,
            "textStyle": {"color": TEXT, "fontSize": 13},
        },
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "shadow"},
            "backgroundColor": "rgba(8,14,26,.94)",
            "borderColor": _rgba(CYAN, 0.45),
            "textStyle": {"color": TEXT},
        },
        "xAxis": {
            "type": "category",
            "data": categories,
            "axisLine": {"lineStyle": {"color": _rgba(MUTED, 0.55)}},
            "axisTick": {"show": False},
            "axisLabel": {"color": TEXT, "fontSize": 12, "interval": 0, "margin": 12},
        },
        "yAxis": {
            "type": "value",
            "name": "占比 %",
            "nameTextStyle": {"color": MUTED, "fontSize": 12, "padding": [0, 0, 6, 0]},
            "max": 100,
            "axisLine": {"show": False},
            "axisTick": {"show": False},
            "axisLabel": {"color": MUTED, "fontSize": 12, "formatter": "{value}%"},
            "splitLine": {"show": True, "lineStyle": {"color": GRID, "type": "dashed"}},
        },
        "series": [me_series, average_series],
    }


def _coerce_score(score):
    """健康分清洗：只接受数字（含数字字符串），夹在 0~100，其它一律 0。"""
    if isinstance(score, bool):
        return 0.0
    if isinstance(score, (int, float)):
        value = float(score)
    elif isinstance(score, str):
        try:
            value = float(score.strip())
        except (TypeError, ValueError):
            return 0.0
    else:
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(value, 100.0))


def _coerce_size(size):
    """水球容器尺寸：只接受数字，夹在 200~800，避免负数 / 超大值把布局撑爆。"""
    if isinstance(size, bool):
        return 360
    if isinstance(size, (int, float)):
        value = float(size)
    elif isinstance(size, str):
        try:
            value = float(size.strip())
        except (TypeError, ValueError):
            return 360
    else:
        return 360
    if not math.isfinite(value):
        return 360
    return int(max(200.0, min(value, 800.0)))


def _score_text(value):
    """78.0 → '78'；78.5 → '78.5'（不显示无意义的小数尾巴）。"""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text or "0"


def _liquid_palette(value):
    """水球配色随分数变化：≥80 青蓝、≥60 紫、<60 橙红。"""
    if value >= 80.0:
        return CYAN, BLUE, _rgba(CYAN, 0.55)
    if value >= 60.0:
        return PURPLE, "#7C3AED", _rgba(PURPLE, 0.55)
    return "#FB923C", "#EF4444", _rgba("#FB923C", 0.55)


def _liquid_option(value, score_text):
    """拼 liquidFill 的 option（纯 JSON，无函数字符串）。"""
    primary, secondary, glow = _liquid_palette(value)
    ratio = round(value / 100.0, 4)

    # 三层水波：从分数往下每层减 0.1，最低夹到 0，避免负数水位。
    layers = [round(max(ratio - 0.1 * index, 0.0), 4) for index in range(3)]
    layer_colors = [
        {
            "type": "linear",
            "x": 0,
            "y": 0,
            "x2": 1,
            "y2": 1,
            "colorStops": [
                {"offset": 0, "color": _rgba(primary, alpha + 0.08)},
                {"offset": 1, "color": _rgba(secondary, max(alpha - 0.22, 0.12))},
            ],
        }
        for alpha in _LAYER_ALPHAS
    ]

    return {
        "backgroundColor": "rgba(0,0,0,0)",
        "series": [
            {
                "type": "liquidFill",
                "shape": "circle",
                "radius": "82%",
                "center": ["50%", "52%"],
                "data": layers,
                "color": layer_colors,
                "amplitude": "7%",
                "waveLength": "72%",
                "phase": 0,
                "waveAnimation": True,
                "animationDuration": 1400,
                "animationDurationUpdate": 900,
                "outline": {"show": False},
                "backgroundStyle": {
                    "color": "#07101F",
                    "borderColor": _rgba(MUTED, 0.22),
                    "borderWidth": 1,
                    "shadowBlur": 26,
                    "shadowColor": glow,
                },
                "label": {
                    "show": True,
                    "insideColor": "#FFFFFF",
                    "color": TEXT,
                    "fontSize": 46,
                    "fontWeight": 800,
                    "fontFamily": "Segoe UI, PingFang SC, Microsoft YaHei",
                    "formatter": score_text,
                    "textShadowBlur": 18,
                    "textShadowColor": glow,
                },
                "itemStyle": {
                    "opacity": 0.94,
                    "shadowBlur": 22,
                    "shadowColor": glow,
                },
            }
        ],
    }


_LIQUID_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>财务健康分水球</title>
<style>
  html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: transparent; }
  body { font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; overflow: hidden; }
  #macro-liquid-shell { position: relative; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; background: radial-gradient(circle at 50% 34%, rgba(34,211,238,.10), rgba(4,7,15,0) 64%); }
  .macro-liquid-head { display: flex; align-items: center; justify-content: center; gap: 10px; height: 44px; flex: 0 0 44px; }
  .macro-liquid-title { color: #E2E8F0; font-size: 15px; letter-spacing: .14em; text-shadow: 0 0 12px rgba(34,211,238,.55); }
  .macro-liquid-grade { color: __PRIMARY__; font-size: 13px; font-weight: 700; letter-spacing: .08em; padding: 2px 9px; border: 1px solid __GLOW__; border-radius: 999px; background: rgba(8,14,26,.6); box-shadow: 0 0 12px __GLOW__; }
  #macro-liquid-chart { width: 100%; height: calc(100% - 44px); }
  #macro-liquid-fallback { display: none; width: 100%; height: calc(100% - 44px); flex-direction: column; align-items: center; justify-content: center; gap: 8px; }
  .macro-fallback-ring { position: relative; width: __RING__px; height: __RING__px; border-radius: 50%; background: conic-gradient(__PRIMARY__ 0deg, __SECONDARY__ __DEG__deg, rgba(148,163,184,.14) __DEG__deg 360deg); box-shadow: 0 0 30px __GLOW__; display: flex; align-items: center; justify-content: center; }
  .macro-fallback-ring::before { content: ""; position: absolute; inset: 14px; border-radius: 50%; background: radial-gradient(circle at 50% 32%, #0B1B33 0%, #04070F 72%); border: 1px solid rgba(148,163,184,.18); }
  .macro-fallback-inner { position: relative; z-index: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .macro-fallback-score { color: #E2E8F0; font-size: __SCORE_FONT__px; font-weight: 800; line-height: 1; text-shadow: 0 0 18px __GLOW__; }
  .macro-fallback-label { color: #7C8CA6; font-size: 13px; letter-spacing: .12em; }
  .macro-fallback-grade { color: __PRIMARY__; font-size: 14px; font-weight: 700; margin-top: 2px; }
  .macro-fallback-tip { color: #7C8CA6; font-size: 11px; margin-top: 6px; text-align: center; padding: 0 10px; line-height: 1.5; }
</style>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts-liquidfill@3.1.0/dist/echarts-liquidfill.min.js"></script>
</head>
<body>
<div id="macro-liquid-shell">
  <div class="macro-liquid-head">
    <span class="macro-liquid-title">__LABEL_HTML__</span>
    <span class="macro-liquid-grade">__GRADE_HTML__</span>
  </div>
  <div id="macro-liquid-chart"></div>
  <div id="macro-liquid-fallback">
    <div class="macro-fallback-ring">
      <div class="macro-fallback-inner">
        <div class="macro-fallback-score">__SCORE_TEXT__</div>
        <div class="macro-fallback-label">__LABEL_HTML__</div>
        <div class="macro-fallback-grade">__GRADE_HTML__</div>
      </div>
    </div>
    <div class="macro-fallback-tip">图表资源加载失败，已切换静态视图</div>
  </div>
</div>
<script>
(function () {
  var option = __OPTION_JSON__;
  var chartEl = document.getElementById("macro-liquid-chart");
  var fallbackEl = document.getElementById("macro-liquid-fallback");
  var settled = false;
  function showFallback() {
    if (settled) return;
    settled = true;
    window.__macroLiquidOk = false;
    if (chartEl) { chartEl.style.display = "none"; }
    if (fallbackEl) { fallbackEl.style.display = "flex"; }
  }
  function tryRender() {
    if (settled) return true;
    try {
      if (!window.echarts || typeof window.echarts.init !== "function") return false;
      var chart = window.echarts.init(chartEl, null, { renderer: "canvas" });
      chart.setOption(option);
      window.__macroLiquidOk = true;
      return true;
    } catch (err) {
      return false;
    }
  }
  if (!tryRender()) {
    var attempts = 0;
    var timer = setInterval(function () {
      attempts += 1;
      if (tryRender()) {
        clearInterval(timer);
      } else if (attempts >= 10) {
        clearInterval(timer);
        showFallback();
      }
    }, 120);
    setTimeout(function () { if (!window.__macroLiquidOk) { showFallback(); } }, 1200);
  }
  window.addEventListener("resize", function () {
    if (window.echarts && chartEl && window.__macroLiquidOk) {
      var inst = window.echarts.getInstanceByDom(chartEl);
      if (inst) { inst.resize(); }
    }
  });
})();
</script>
</body>
</html>
"""


def build_liquid_html(score, grade: str = "", label: str = "财务健康分", size: int = 360) -> str:
    """返回完整的水球 HTML（交给 st.components.v1.html 渲染）。

    - 分数夹在 0~100，水波取分数/100；配色随分数档位变化；
    - 文本（label / grade）一律 html.escape，数字只保留清洗后的数值；
    - CDN 挂掉 / 断网时，1.2s 后切到纯 CSS 圆环静态视图，绝不白屏。
    """
    value = _coerce_score(score)
    box = _coerce_size(size)
    primary, secondary, glow = _liquid_palette(value)

    label_text = html.escape(str(label or "").strip() or "财务健康分", quote=True)
    grade_text = str(grade or "").strip()
    grade_html = html.escape(grade_text, quote=True) if grade_text else "—"
    score_text = _score_text(value)

    option = _liquid_option(value, score_text)
    # ensure_ascii=True：中文全部转成 \\uXXXX，既不会截断 <script>，也不依赖页面编码。
    option_json = json.dumps(option, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )

    replacements = {
        "__PRIMARY__": primary,
        "__SECONDARY__": secondary,
        "__GLOW__": glow,
        "__RING__": str(int(box * 0.56)),
        "__SCORE_FONT__": str(int(max(26, min(box * 0.14, 64)))),
        "__DEG__": f"{value / 100.0 * 360.0:.1f}",
        "__LABEL_HTML__": label_text,
        "__GRADE_HTML__": grade_html,
        "__SCORE_TEXT__": score_text,
        "__OPTION_JSON__": option_json,
    }
    page = _LIQUID_TEMPLATE
    for token, replacement in replacements.items():
        page = page.replace(token, replacement)
    return page


def _kpi_value(container, key, default=0.0):
    """从可能是脏值的子 dict 里取一个数字。"""
    if not isinstance(container, dict):
        return default
    return _number(container.get(key), default)


def _kpi_delta(delta_block, key, fallback):
    """优先用后端算好的 delta；缺失 / None / 非法值回落到「我 − 全站平均」。"""
    if not isinstance(delta_block, dict):
        return fallback
    if key not in delta_block:
        return fallback
    return _number(delta_block.get(key), fallback)


def build_kpi_cards(stats: dict) -> list[dict]:
    """4 张 KPI 卡的数据（顺序固定：支出 / 恩格尔 / 结余率 / 账单笔数）。

    比率类（engel / saving_rate）在函数内 ×100，前端直接按百分数展示；
    所有缺失值按 0，绝不抛异常。
    """
    me_block = stats.get("me") if isinstance(stats, dict) else None
    global_block = stats.get("global") if isinstance(stats, dict) else None
    delta_block = stats.get("delta") if isinstance(stats, dict) else None

    expense_me = _kpi_value(me_block, "expense")
    expense_avg = _kpi_value(global_block, "avg_expense")
    expense_delta = _kpi_delta(delta_block, "expense", expense_me - expense_avg)

    engel_me = _kpi_value(me_block, "engel") * 100.0
    engel_avg = _kpi_value(global_block, "avg_engel") * 100.0
    # delta 后端给的是 0~1 的小数差，这里同样 ×100；缺失时回落到「我 − 全站平均」。
    engel_delta_ratio = _number(
        delta_block.get("engel") if isinstance(delta_block, dict) else None,
        (engel_me - engel_avg) / 100.0,
    )
    engel_delta = round(engel_delta_ratio * 100.0, 2)

    saving_me = _kpi_value(me_block, "saving_rate") * 100.0
    saving_avg = _kpi_value(global_block, "avg_saving_rate") * 100.0
    saving_delta_ratio = _number(
        delta_block.get("saving_rate") if isinstance(delta_block, dict) else None,
        (saving_me - saving_avg) / 100.0,
    )
    saving_delta = round(saving_delta_ratio * 100.0, 2)

    bills_me = _kpi_value(me_block, "bill_count")
    bills_avg = _kpi_value(global_block, "avg_bill_count")
    bills_delta = _kpi_delta(delta_block, "bill_count", bills_me - bills_avg)

    return [
        {
            "label": "本月支出",
            "me": round(expense_me, 2),
            "avg": round(expense_avg, 2),
            "delta": round(expense_delta, 2),
            "unit": "元",
            "better": "lower",
        },
        {
            "label": "恩格尔系数",
            "me": round(engel_me, 2),
            "avg": round(engel_avg, 2),
            "delta": engel_delta,
            "unit": "%",
            "better": "lower",
        },
        {
            "label": "结余率",
            "me": round(saving_me, 2),
            "avg": round(saving_avg, 2),
            "delta": saving_delta,
            "unit": "%",
            "better": "higher",
        },
        {
            "label": "账单笔数",
            "me": round(bills_me, 2),
            "avg": round(bills_avg, 2),
            "delta": round(bills_delta, 2),
            "unit": "笔",
            "better": "neutral",
        },
    ]