"""财务健康分（纯标准库、无副作用、可单测）。

用途
----
给「明白账」首页算一个 0~100 的健康分，并给出等级/称号与可读的扣分项，
供页面展示、也供 ai_agent.generate_health_comment 生成一句点评。

设计约束
--------
· **纯函数**：不读数据库、不读 secrets、不打网络、不写日志、不改全局状态；
  同一组入参永远得到同一组输出，可以直接在单测里断言。
· **绝不抛异常**：任一入参为 None / 字符串 / NaN / inf / 负数 / 类型不对，
  都按"这一项没有数据"安全降级，返回仍然合法（0~100、等级/称号齐全）的结果。
  健康分是展示性辅助功能，不能因为一个脏入参把首页搞崩。
· 只用标准库（math），不 import pandas/numpy/streamlit。

评分口径（总分 100）
--------------------
1. 预算控制 40 分：
   · budget <= 0（含 None/非法）→ 20 分（中性），deductions 加「未设置预算」；
   · ratio = max(0, month_expense) / budget；
     - ratio <= 0.8            → 40 分；
     - 0.8 < ratio <= 1.0      → 线性 40 → 30（40 - (ratio-0.8)*50）；
     - ratio > 1.0             → max(0, 30 - (ratio-1)*100)，
                                 deductions 加「本月超支 {month_expense-budget:.0f} 元」；
   · month_balance > 0 时在预算分上 +5，封顶 40（有结余说明当月现金流没绷紧）。
2. 储蓄目标 30 分：
   · goal_progress 非 None → 30 * clamp(goal_progress, 0, 1)；
   · 否则用 save_target：save_target > 0 → 30 * clamp(month_balance/save_target, 0, 1)，
     save_target <= 0（含 None/非法）→ 15 分（中性）；
   · 有效进度 p < 0.5 → deductions 加「储蓄进度仅 {p*100:.0f}%」。
3. 消费健康 30 分：
   · total = sum(category_totals 中的非负有限值)；total <= 0（含空/非法）→ 20 分（中性）；
   · unhealthy = (奶茶咖啡 + 娱乐) / total；
     score = 30 - max(0, unhealthy - 0.2) * 75，下限 0；
     unhealthy > 0.3 → deductions 加「奶茶咖啡+娱乐占比 {unhealthy*100:.0f}%」；
   · 单一分类占比 > 0.6 → 再 -5（下限 0），并加「「分类」占比过高」。
4. 总分 = round(三项之和)，再 clamp 到 0~100。等级/称号：
   · >= 90 → A / 理财大师
   · >= 75 → B / 稳健达人
   · >= 60 → C / 及格边缘
   · 其余   → D / 月光族

返回
----
{
  "score": int,                 # 0~100
  "grade": "A"|"B"|"C"|"D",
  "title": "理财大师"|"稳健达人"|"及格边缘"|"月光族",
  "deductions": list[str],      # 中文短句，可为空（无扣分项时）
  "breakdown": {"budget": int, "saving": int, "consumption": int},
}
注意：breakdown 是三项各自四舍五入后的整数，score 是三项**未舍入**之和再四舍五入，
极端情况下 breakdown 相加与 score 可能差 1 分，这是为了让 score 严格等于口径 4。
"""

from __future__ import annotations

import math


# (最低分, 等级, 称号)：从高到低匹配，最后一项 0 兜底，保证任何分数都有归属。
_GRADE_TABLE = (
    (90, "A", "理财大师"),
    (75, "B", "稳健达人"),
    (60, "C", "及格边缘"),
    (0, "D", "月光族"),
)


def _finite_float(value):
    """宽松转 float：None / 非法字符串 / NaN / inf → None（合法有限值原样返回）。

    用 None 而不是 0 表示"没有这个数据"：预算缺失要走中性分，而不是被当成
    budget=0 再走一次同一分支 —— 两者结果相同，但 None 能让调用方/单测区分
    "没填"和"填了 0"。所有转换异常都在这里吃掉，保证上层永不抛异常。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp01(value: float) -> float:
    """把进度夹到 [0, 1]。"""
    return min(1.0, max(0.0, value))


def _clean_category_totals(category_totals) -> dict[str, float]:
    """把分类金额清洗成 {分类: 非负有限值}；坏值/负数/非 dict 一律忽略。"""
    if not isinstance(category_totals, dict):
        return {}
    cleaned: dict[str, float] = {}
    for name, raw_amount in category_totals.items():
        amount = _finite_float(raw_amount)
        if amount is None or amount <= 0:
            continue
        cleaned[str(name)] = cleaned.get(str(name), 0.0) + amount
    return cleaned


def _grade_for(score: int) -> tuple[str, str]:
    """按总分查等级/称号；表尾 0 兜底，永远不会落空。"""
    for threshold, grade, title in _GRADE_TABLE:
        if score >= threshold:
            return grade, title
    return "D", "月光族"


def compute_health_score(
    *,
    month_expense: float,
    budget: float,
    month_balance: float,
    save_target: float,
    goal_progress: float | None,
    category_totals: dict[str, float],
) -> dict:
    """计算财务健康分（口径见模块 docstring，任何非法入参都安全降级、不抛异常）。"""
    deductions: list[str] = []

    # ---- 入参清洗：非法值统一变成"没有数据"，负数按 0 处理 ----
    expense = _finite_float(month_expense)
    expense = max(0.0, expense) if expense is not None else 0.0
    balance = _finite_float(month_balance)
    balance = balance if balance is not None else 0.0
    budget_value = _finite_float(budget)

    # ---- 1) 预算控制 40 分 ----
    if budget_value is None or budget_value <= 0:
        budget_score = 20.0
        deductions.append("未设置预算")
        ratio = None
    else:
        ratio = expense / budget_value
        if ratio <= 0.8:
            budget_score = 40.0
        elif ratio <= 1.0:
            # 0.8 → 40，1.0 → 30，线性过渡
            budget_score = 40.0 - (ratio - 0.8) * 50.0
        else:
            budget_score = max(0.0, 30.0 - (ratio - 1.0) * 100.0)
            deductions.append(f"本月超支 {expense - budget_value:.0f} 元")
    if balance > 0:
        # 有结余只给预算项加成，且预算项自身封顶 40（不挤占储蓄/消费两项）。
        budget_score = min(40.0, budget_score + 5.0)

    # ---- 2) 储蓄目标 30 分 ----
    progress = _finite_float(goal_progress)
    if progress is not None:
        p = _clamp01(progress)
        saving_score = 30.0 * p
    else:
        target = _finite_float(save_target)
        if target is not None and target > 0:
            p = _clamp01(balance / target)
            saving_score = 30.0 * p
        else:
            # 没有目标也没有进度：中性 15 分，不奖不罚。
            p = None
            saving_score = 15.0
    if p is not None and p < 0.5:
        deductions.append(f"储蓄进度仅 {p * 100:.0f}%")

    # ---- 3) 消费健康 30 分 ----
    totals = _clean_category_totals(category_totals)
    total = sum(totals.values())
    if total <= 0:
        consumption_score = 20.0
    else:
        unhealthy = (totals.get("奶茶咖啡", 0.0) + totals.get("娱乐", 0.0)) / total
        consumption_score = max(0.0, 30.0 - max(0.0, unhealthy - 0.2) * 75.0)
        if unhealthy > 0.3:
            deductions.append(f"奶茶咖啡+娱乐占比 {unhealthy * 100:.0f}%")
        top_name, top_amount = max(totals.items(), key=lambda item: item[1])
        if top_amount / total > 0.6:
            consumption_score = max(0.0, consumption_score - 5.0)
            deductions.append(f"「{top_name}」占比过高")

    # ---- 4) 汇总 ----
    # 总分用三项未舍入值相加再 round；breakdown 只是展示用的整数拆分。
    score = int(round(budget_score + saving_score + consumption_score))
    score = min(100, max(0, score))
    grade, title = _grade_for(score)
    return {
        "score": score,
        "grade": grade,
        "title": title,
        "deductions": deductions,
        "breakdown": {
            "budget": int(round(budget_score)),
            "saving": int(round(saving_score)),
            "consumption": int(round(consumption_score)),
        },
    }
