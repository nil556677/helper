"""重排后自适应切分：找相邻位次之间的“断崖”作为证据截止点（不取固定条数）。"""

from __future__ import annotations  # 延迟求值注解

__all__ = ["cliff_cut"]  # 导出


def cliff_cut(  # 断崖切分
    scores: list[float],  # 分数（已按降序）
    *,
    max_n: int,  # 上限
    min_n: int = 1,  # 下限
    cliff_ratio: float = 2.0,  # 断崖相对占比（与相邻 gap 参考比）
    min_cliff: float = 0.05,  # 断崖绝对分差
) -> tuple[int, str]:  # (取前 n, 原因)
    """n 取在最大“断崖”处；无断崖取 min(len, max_n)。原因: cliff / max_cap / min_floor / all。"""
    n = len(scores)  # 总数
    if n <= min_n:  # 太少
        return n, "all"  # 全取
    capped = min(n, max_n)  # 上限
    clamped = max(capped, min_n)  # 至少 min_n
    if clamped == 1 and min_n > 1:  # 下限保护（max_n<min_n 时）
        clamped = max(capped, min_n)  # 取更大的
    gaps = [max(0.0, scores[i - 1] - scores[i]) for i in range(1, n)]  # 相邻差
    # 参考 = 前几个 gap 的中位（首个断崖对照局部梯度，避免被尾部噪声带偏）
    local = sorted(gaps[: max(3, min_n + 1)])  # 局部 gap
    reference = local[len(local) // 2] if local else 0.0  # 中位
    if reference <= 0.0:  # 无参考
        return clamped, "max_cap"  # 退上限
    threshold = max(min_cliff, cliff_ratio * reference)  # 断崖阈值
    for i, gap in enumerate(gaps, start=1):  # 逐 gap
        take = i  # 若断则取前 i 个
        if take < min_n:  # 还不够下限
            continue  # 跳过
        if take > max_n:  # 超上限
            break  # 用上限
        if gap >= threshold:  # 是断崖
            return take, f"cliff"  # 断在 i 位后
    return clamped, "max_cap"  # 无断崖 → 上限