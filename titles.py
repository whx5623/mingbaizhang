"""积分头衔系统：纯本地计算，不读库、不写库、无副作用，可直接单测。

产品定义的四档头衔（按**闭区间**划分，规则来自产品定义）：

    ==========  ==================  ====
    积分区间     头衔                图标
    ==========  ==================  ====
    0 ~ 100      记账新手            🐣
    101 ~ 300    省钱学徒            🌱
    301 ~ 600    理财达人            📈
    601 及以上   财务自由（预备役）  👑
    ==========  ==================  ====

端点归属容易写错，这里再强调一次：
    · 100 分仍是「记账新手」，**101 分**才升到「省钱学徒」；
    · 300 分仍是「省钱学徒」，**301 分**才升到「理财达人」；
    · 600 分仍是「理财达人」，**601 分**才升到「财务自由（预备役）」。

每档最后一个整数分（100 / 300 / 600）的 ``progress`` 记为 1.0：
此时距离下一档只差 1 分，若按 ``(points - threshold) / (next - threshold)``
算会得到 0.99…，进度条看起来「永远差一点」，与「本级已到头」的直觉不符。
最高档（601+）没有下一档，``progress`` 固定 1.0、``next_*`` 全部为 None。
"""

TITLE_LEVELS = [
    (0, "记账新手", "🐣"),
    (101, "省钱学徒", "🌱"),
    (301, "理财达人", "📈"),
    (601, "财务自由（预备役）", "👑"),
]


def _coerce_points(points) -> int:
    """把入参转成非负 int；非法输入统一抛 ValueError。

    显式拒绝 bool：Python 里 ``True`` 是 ``int`` 的子类（``True == 1``），
    不挡掉就会把 ``True`` 静默当成 1 分；``None`` / 非数字字符串 / 负数
    同样一律 ValueError，由调用方决定降级方式。
    """
    if isinstance(points, bool):
        raise ValueError(f"points 必须是非负整数：{points!r}")
    try:
        value = int(points)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"points 必须是非负整数：{points!r}") from exc
    if value < 0:
        raise ValueError(f"points 必须是非负整数：{points!r}")
    return value


def get_title(points) -> dict:
    """按积分计算头衔、等级与升级进度。

    返回 dict 的字段（页面直接按 key 取用）：

    ==================  ====================================================
    key                 含义
    ==================  ====================================================
    points              入参转成的 int（调用方拿到的规范值）
    title / emoji       当前头衔文案 / 图标
    level_index         0..3，对应 TITLE_LEVELS 下标
    threshold           当前等级的起始分
    next_threshold      下一级起始分；最高级为 None
    next_title          下一级头衔；最高级为 None
    next_emoji          下一级图标；最高级为 None
    points_to_next      距下一级还差多少分；最高级为 None
    progress            当前等级内进度 0.0~1.0；最高级为 1.0
    is_max              是否已到最高级
    ==================  ====================================================

    非法入参（负数 / None / 非数字 / bool）→ ValueError。
    """
    value = _coerce_points(points)

    level_index = 0
    for index, (threshold, _title, _emoji) in enumerate(TITLE_LEVELS):
        if value >= threshold:
            level_index = index
        else:
            break

    threshold, title, emoji = TITLE_LEVELS[level_index]
    is_max = level_index == len(TITLE_LEVELS) - 1

    if is_max:
        next_threshold = None
        next_title = None
        next_emoji = None
        points_to_next = None
        progress = 1.0
    else:
        next_threshold, next_title, next_emoji = TITLE_LEVELS[level_index + 1]
        points_to_next = next_threshold - value
        span = next_threshold - threshold
        if value >= next_threshold - 1:
            # 本级最后一个整数分（100 / 300 / 600）：本级已走完，防止显示 99.xx%
            progress = 1.0
        else:
            progress = max(0.0, min(1.0, (value - threshold) / span))

    return {
        "points": value,
        "title": title,
        "emoji": emoji,
        "level_index": level_index,
        "threshold": threshold,
        "next_threshold": next_threshold,
        "next_title": next_title,
        "next_emoji": next_emoji,
        "points_to_next": points_to_next,
        "progress": progress,
        "is_max": is_max,
    }


def crossed_threshold(old_points, new_points) -> bool:
    """判断积分变化是否跨过了等级门槛（用于「升级撒花」）。

    实现即 ``get_title(new)["level_index"] > get_title(old)["level_index"]``。
    任一入参非法时返回 False 而不抛异常：这个函数只决定「撒不撒花」，
    页面不能因为库里的脏数据（如 None / 负数）红屏。
    """
    try:
        old_index = get_title(old_points)["level_index"]
        new_index = get_title(new_points)["level_index"]
    except (TypeError, ValueError, OverflowError):
        return False
    return new_index > old_index
