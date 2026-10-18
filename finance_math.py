"""金融计算纯函数。

本模块只依赖 Python 标准库，不访问数据库、不读取配置或密钥。

约定：
- 所有金额均为正数，单位为“元”。
- ``compute_irr`` 返回的是**月利率**及其按复利换算的年化利率。
- 函数无副作用，相同输入始终得到相同输出。
"""

from __future__ import annotations

import math
import numbers
from typing import Any

__all__ = ["compute_irr", "payment_from_fee", "debt_ratio"]

# 二分法默认搜索区间。下界必须严格大于 -1，否则 (1 + m) ** (-n) 无意义。
_BISECTION_LOW = -0.9999
_BISECTION_HIGH = 10.0
# 二分法需要约 37 次即可把 11 元区间缩到 1e-10，这里留足余量。
_BISECTION_MAX_ITER = 200


def _finite_real(value: Any, name: str) -> float:
    """把入参转成有限 float；布尔值、非实数、NaN、inf 一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{name} 必须是有限的实数")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} 必须是有限的实数") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} 不能是 NaN 或无穷大")
    return result


def _positive_real(value: Any, name: str) -> float:
    """校验严格大于 0 的有限实数。"""
    result = _finite_real(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} 必须大于 0")
    return result


def _nonnegative_real(value: Any, name: str) -> float:
    """校验大于等于 0 的有限实数。"""
    result = _finite_real(value, name)
    if result < 0.0:
        raise ValueError(f"{name} 不能为负数")
    return result


def _validate_periods(periods: Any) -> int:
    """校验期数：必须是 1~600 的整数（bool 不算整数）。"""
    if isinstance(periods, bool) or not isinstance(periods, numbers.Integral):
        raise ValueError("期数 periods 必须是整数")
    result = int(periods)
    if result < 1 or result > 600:
        raise ValueError("期数 periods 必须在 1 到 600 之间")
    return result


def _validate_tol(tol: Any) -> float:
    """校验收敛阈值。"""
    result = _positive_real(tol, "收敛阈值 tol")
    return result


def _validate_max_iter(max_iter: Any) -> int:
    """校验最大迭代次数。"""
    if isinstance(max_iter, bool) or not isinstance(max_iter, numbers.Integral):
        raise ValueError("最大迭代次数 max_iter 必须是整数")
    result = int(max_iter)
    if result < 1:
        raise ValueError("最大迭代次数 max_iter 必须大于 0")
    return result


def _annuity_factor(monthly_rate: float, periods: int) -> float:
    """计算年金现值系数 sum((1+m)^(-k), k=1..n)。

    等价于 ``(1 - (1+m)^(-n)) / m``（m != 0）。
    靠近 0 时用 ``expm1/log1p`` 避免 1 - (1+m)^(-n) 的灾难性相消；
    m 为正且较大时使用负指数，避免 (1+m)^n 上溢。
    """
    if monthly_rate == 0.0:
        return float(periods)

    # 小利率下 1 - (1+m)^(-n) 会损失大量有效数字，改用稳定形式。
    if abs(monthly_rate) < 1e-5:
        try:
            return -math.expm1(-periods * math.log1p(monthly_rate)) / monthly_rate
        except OverflowError:
            return math.inf

    try:
        discount = (1.0 + monthly_rate) ** (-periods)
    except OverflowError:
        # 例如 m 非常接近 -1 时，折现因子本身超出 float 范围。
        return math.inf
    return (1.0 - discount) / monthly_rate


def _npv(
    monthly_rate: float,
    principal: float,
    periods: int,
    payment_per_period: float,
) -> float:
    """给定月利率 m 的净现值 NPV(m)。

    t=0 收到本金（正现金流），t=1..n 每期支付固定金额（负现金流）。
    返回 ``-inf`` 表示 m 位于有效域边缘、现值已低于 float 可表示范围。
    """
    if monthly_rate <= -1.0:
        # 有效域是 m > -1；此分支只用于防御，不会在正常搜索中触发。
        return -math.inf
    if monthly_rate == 0.0:
        return principal - payment_per_period * periods

    factor = _annuity_factor(monthly_rate, periods)
    if math.isinf(factor):
        return -math.inf
    return principal - payment_per_period * factor


def _npv_derivative(
    monthly_rate: float,
    periods: int,
    payment_per_period: float,
) -> float:
    """NPV 对月利率的解析导数。

    NPV(m) = P - A * (1 - (1+m)^(-n)) / m
    NPV'(m) = A * (1 - (1+m)^(-n) - n*m*(1+m)^(-n-1)) / m^2
    在 m=0 处使用极限 A*n*(n+1)/2。
    """
    if monthly_rate <= -1.0:
        return math.inf
    if monthly_rate == 0.0:
        return payment_per_period * periods * (periods + 1) / 2.0

    # 极靠近 0 时上面的差分会相消，使用泰勒展开的前几项。
    if abs(monthly_rate) < 1e-8:
        n = float(periods)
        s1 = n * (n + 1.0) / 2.0
        s2 = n * (n + 1.0) * (n + 2.0) / 3.0
        s3 = n * (n + 1.0) * (n + 2.0) * (n + 3.0) / 8.0
        return payment_per_period * (
            s1 - monthly_rate * s2 + monthly_rate * monthly_rate * s3
        )

    try:
        discount = (1.0 + monthly_rate) ** (-periods)
    except OverflowError:
        return math.inf

    numerator = (
        1.0
        - discount
        - periods * monthly_rate * discount / (1.0 + monthly_rate)
    )
    return payment_per_period * numerator / (monthly_rate * monthly_rate)


def _make_result(
    monthly_rate: float,
    principal: float,
    periods: int,
    payment_per_period: float,
    *,
    iterations: int,
    converged: bool,
    method: str,
) -> dict:
    """组装统一返回结构。"""
    if monthly_rate == 0.0:
        monthly_rate = 0.0  # 归一化 -0.0

    try:
        annual_rate = (1.0 + monthly_rate) ** 12 - 1.0
    except OverflowError:
        annual_rate = math.inf

    total_payment = payment_per_period * periods
    total_interest = total_payment - principal
    if total_interest == 0.0:
        total_interest = 0.0  # 归一化 -0.0

    return {
        "monthly_rate": float(monthly_rate),
        "annual_rate": float(annual_rate),
        "total_payment": float(total_payment),
        "total_interest": float(total_interest),
        "iterations": int(iterations),
        "converged": bool(converged),
        "method": method,
    }


def _solve_by_bisection(
    principal: float,
    periods: int,
    payment_per_period: float,
    tol: float,
) -> tuple[float, int, bool]:
    """在 [-0.9999, 10] 上二分求根；必要时向两侧扩展区间。

    NPV(m) 在 m > -1 上严格单调递增，因此根唯一。
    返回 (monthly_rate, iterations, converged)。
    """
    low = _BISECTION_LOW
    high = _BISECTION_HIGH
    f_low = _npv(low, principal, periods, payment_per_period)
    f_high = _npv(high, principal, periods, payment_per_period)

    # 极端参数可能把根推到默认区间之外；向 -1 或正无穷方向扩展。
    if f_low > 0.0:
        for exponent in range(5, 16):
            candidate = -1.0 + 10.0 ** (-exponent)
            if candidate >= low:
                # 必须比当前下界更靠近 -1；舍入到 -1 的候选也跳过。
                continue
            low = candidate
            f_low = _npv(low, principal, periods, payment_per_period)
            if f_low <= 0.0:
                break
        else:
            raise ValueError("在有效月利率范围 (-1, +∞) 内找不到 IRR 根")

    if f_high < 0.0:
        # 每期还款额相对本金极小时，根可能远大于 10；按十进制扩展上界。
        expanded = False
        for _ in range(330):
            next_high = high * 10.0
            if not math.isfinite(next_high):
                break
            high = next_high
            f_high = _npv(high, principal, periods, payment_per_period)
            if f_high >= 0.0:
                expanded = True
                break
        if not expanded:
            raise ValueError("在有效月利率范围 (-1, +∞) 内找不到 IRR 根")

    if math.isnan(f_low) or math.isnan(f_high):
        raise ValueError("在有效月利率范围 (-1, +∞) 内找不到 IRR 根")

    if f_low == 0.0:
        return low, 0, True
    if f_high == 0.0:
        return high, 0, True

    monthly_rate = (low + high) / 2.0
    converged = False
    iterations = 0
    for i in range(1, _BISECTION_MAX_ITER + 1):
        iterations = i
        monthly_rate = (low + high) / 2.0
        f_mid = _npv(monthly_rate, principal, periods, payment_per_period)

        if (
            f_mid == 0.0
            or abs(f_mid) <= tol
            or (high - low) / 2.0 <= tol * max(1.0, abs(monthly_rate))
        ):
            converged = True
            break
        if f_mid < 0.0:
            low = monthly_rate
        else:
            high = monthly_rate

    return monthly_rate, iterations, converged


def compute_irr(
    principal: float,
    periods: int,
    payment_per_period: float,
    *,
    tol: float = 1e-10,
    max_iter: int = 100,
) -> dict:
    """计算等额本息/等额费用的月利率与实际年化利率。

    现金流约定：
    - t=0 收到 ``principal``（正现金流）；
    - t=1..periods 每期支付 ``payment_per_period``（负现金流）。
    - NPV(m) = principal - payment * (1 - (1+m)^(-periods)) / m。

    求解流程：
    1. 先看 m=0 时的 NPV；``payment * periods == principal`` 时直接返回
       ``method="exact"``。
    2. 否则从 0.01 开始牛顿迭代，使用解析导数。
    3. 牛顿迭代发散、越出定义域、导数为零或达到 ``max_iter`` 仍未收敛时，
       回退到 [-0.9999, 10] 上的二分法（极端情况下会向两侧扩展）。
    4. 年化利率按复利计算：``(1 + monthly_rate) ** 12 - 1``，
       不是简单的月利率乘 12。

    参数：
    - principal：本金，必须为有限正数；
    - periods：期数，1~600 的整数；
    - payment_per_period：每期还款额，必须为有限正数；
    - tol：收敛阈值，必须为正；
    - max_iter：牛顿迭代的最大次数。

    返回字段：
    - monthly_rate：月利率；
    - annual_rate：复利年化利率；
    - total_payment：总还款额；
    - total_interest：总利息（可能为负，表示总还款少于本金）；
    - iterations：最终采用的方法实际迭代次数（exact 为 0）；
    - converged：是否满足收敛条件；
    - method：``"newton"`` / ``"bisection"`` / ``"exact"``。

    异常：
    - 任何非法参数（含 NaN、inf）抛出带中文消息的 ``ValueError``。
    """
    principal_value = _positive_real(principal, "本金 principal")
    periods_value = _validate_periods(periods)
    payment_value = _positive_real(
        payment_per_period, "每期还款额 payment_per_period"
    )
    tol_value = _validate_tol(tol)
    max_iter_value = _validate_max_iter(max_iter)

    # 先做 m=0 的符号判断；总还款恰好等于本金时 IRR 精确为 0。
    npv_at_zero = principal_value - payment_value * periods_value
    if npv_at_zero == 0.0:
        return _make_result(
            0.0,
            principal_value,
            periods_value,
            payment_value,
            iterations=0,
            converged=True,
            method="exact",
        )

    monthly_rate = 0.01
    newton_converged = False
    newton_iterations = 0
    # 牛顿法收敛时允许的残差；对绝大多数金额，tol 起主导作用。
    residual_tol = max(tol_value, 1e-12 * max(1.0, principal_value))

    for i in range(1, max_iter_value + 1):
        newton_iterations = i
        f_value = _npv(monthly_rate, principal_value, periods_value, payment_value)

        if not math.isfinite(f_value):
            break
        if abs(f_value) <= tol_value:
            newton_converged = True
            break

        derivative = _npv_derivative(
            monthly_rate, periods_value, payment_value
        )
        if not math.isfinite(derivative) or derivative <= 0.0:
            break

        step = f_value / derivative
        if not math.isfinite(step):
            break

        candidate = monthly_rate - step
        if not math.isfinite(candidate) or candidate <= -1.0:
            # 牛顿步越出 (m > -1) 的定义域，交给二分法兜底。
            break

        if abs(candidate - monthly_rate) <= tol_value * max(
            1.0, abs(candidate)
        ):
            monthly_rate = candidate
            f_candidate = _npv(
                monthly_rate, principal_value, periods_value, payment_value
            )
            if math.isfinite(f_candidate) and abs(f_candidate) <= residual_tol:
                newton_converged = True
            break

        monthly_rate = candidate

    if not newton_converged:
        # 循环正常耗尽时也再检查一次最后一个点，避免漏掉已收敛的情况。
        f_newton = _npv(
            monthly_rate, principal_value, periods_value, payment_value
        )
        if math.isfinite(f_newton) and abs(f_newton) <= residual_tol:
            newton_converged = True

    if newton_converged:
        return _make_result(
            monthly_rate,
            principal_value,
            periods_value,
            payment_value,
            iterations=newton_iterations,
            converged=True,
            method="newton",
        )

    monthly_rate, bisection_iterations, bisection_converged = _solve_by_bisection(
        principal_value, periods_value, payment_value, tol_value
    )
    return _make_result(
        monthly_rate,
        principal_value,
        periods_value,
        payment_value,
        iterations=bisection_iterations,
        converged=bisection_converged,
        method="bisection",
    )


def payment_from_fee(
    principal: float,
    periods: int,
    fee_per_period: float,
) -> float:
    """按“每期固定手续费”计算每期还款额。

    计算公式：``principal / periods + fee_per_period``。
    手续费不是利率，不参与折现，只作为每期固定费用加在等额本金之上。

    异常：本金、期数、手续费非法（含 NaN/inf、负手续费）时抛出 ValueError。
    """
    principal_value = _positive_real(principal, "本金 principal")
    periods_value = _validate_periods(periods)
    fee_value = _nonnegative_real(fee_per_period, "每期手续费 fee_per_period")
    return principal_value / periods_value + fee_value


def debt_ratio(monthly_payment: float, monthly_living: float) -> float | None:
    """计算本月待还分期总额 / 每月可用生活费。

    - ``monthly_living <= 0`` 时没有可用的分母，返回 ``None``；
    - 负数入参（包括 NaN/inf）抛出 ValueError；
    - 返回值大于 0.3 表示“负债率超过 30%”，前端应据此显示红色警告。
    """
    payment_value = _nonnegative_real(monthly_payment, "每月待还金额 monthly_payment")
    living_value = _nonnegative_real(monthly_living, "每月可用生活费 monthly_living")

    if living_value == 0.0:
        return None
    return payment_value / living_value
