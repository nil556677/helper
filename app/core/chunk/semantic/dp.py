"""② 平滑 + ③ 选点：移动平均与 DP 最优分割。

## 在流水线中的位置

语义兜底的"断点计算"两步（三级结构见 ``semantic/__init__``）：
``units`` 先切出细粒度单元并算相邻相似度 → **本模块**平滑 + 选点 → ``ladder`` 合并成块。

## 输入 / 输出

- ``smooth(scores)``：输入相似度序列，输出等长平滑序列。
- ``is_flat(scores)``：输入相似度序列，输出"是否整段一个主题"。
- ``optimal_cuts(units, scores, budget, minimum, count)``：
  输入单元/相似度/预算，输出切点下标列表（空列表 = 无可行解）。

## 关键约定

1. **切点 i 的代价 = ``scores[i-1]``**：它衡量"单元 i-1 与 i 之间"的相似度，
   相似度越低越该切；
2. **预算硬约束**：每块 token 必须 ≤ budget；除最后一块外还必须 ≥ minimum
   （尾部放宽，否则短尾会导致无解）；
3. 复杂度 O(n²)，n 由 ``MAX_UNITS`` 限制，超长文本不在这里硬算。
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.chunk.semantic._shared import _FLAT_SPREAD, _SMOOTH_WINDOW


def smooth(scores: list[float], *, window: int = _SMOOTH_WINDOW) -> list[float]:
    """移动平均（窗口在两端自动缩小）——不平滑的话噪声会造出假谷。

    参数：
        scores: 相邻单元相似度序列。
        window: 平滑窗口（奇数最好；``≤ 1`` 或序列太短时原样返回）。
    返回：与输入等长的新列表。
    说明：两端不做零填充，而是让窗口自然缩小（否则端点会被压向 0，
        反而制造出"首尾一定是边界"的假象）。
    """
    if len(scores) <= 2 or window <= 1:
        return list(scores)
    half = window // 2
    smoothed: list[float] = []
    for index in range(len(scores)):
        low = max(0, index - half)
        high = min(len(scores), index + half + 1)
        chunk = scores[low:high]
        smoothed.append(sum(chunk) / len(chunk))
    return smoothed


def is_flat(scores: list[float]) -> bool:
    """信号是否"平坦"（整段一个主题）→ 这一级不该用来定边界。

    参数：
        scores: 已平滑的相似度序列。
    返回：序列长度 < 2，或极差 < ``_FLAT_SPREAD`` 时为 ``True``。
    说明：平坦不等于"没有边界"，而是"这一级的信号分不出边界"——调用方应升级到
        更强的信号（或最后退到预算切），而不是硬按噪声切。
    """
    if len(scores) < 2:
        return True
    return (max(scores) - min(scores)) < _FLAT_SPREAD


def optimal_cuts(
    units: list[str],
    scores: list[float],
    *,
    budget: int,
    minimum: int,
    count: Callable[[str], int],
) -> list[int]:
    """**DP 最优分割**：在"每块 ≤ budget、≥ minimum"的可行切法里，最小化 Σ 切点相似度。

    参数：
        units: 细粒度单元。
        scores: 相邻单元相似度（长度 = 单元数 - 1）。
        budget: 单块上限（token）。
        minimum: 单块下限（token；最后一块放宽，否则尾部短块会导致无解）。
        count: 数 token 的函数。
    返回：切点列表（切点 ``i`` 表示"第 i 个单元是某一块的开始"，升序）；
        无可行解（或单元数 ≤ 1）返回空列表。
    """
    size = len(units)
    if size <= 1:
        return []
    # 前缀和：O(1) 求任意 [start, end) 的 token 数。代价是假设"计数可加"（近似），
    # 真正收口由调用方的 enforce_budget 按拼好的文本重算
    prefix = [0] * (size + 1)
    for index, unit in enumerate(units):
        prefix[index + 1] = prefix[index] + count(unit)

    infinity = float("inf")
    best = [infinity] * (size + 1)
    best[0] = 0.0
    previous = [0] * (size + 1)
    for end in range(1, size + 1):
        # 枚举最后一块的起点：best[end] = min(best[start] + 切在 start 的代价)
        for start in range(end):
            span = prefix[end] - prefix[start]
            if span > budget:
                continue
            if span < minimum and end != size:
                continue  # 太短的块不合法（最后一块放宽）
            # 切点 start 的代价 = 单元 start-1 与 start 的相似度（越低越该切）
            cost = best[start] + (scores[start - 1] if start > 0 else 0.0)
            # 1e-12 吸收浮点误差：代价相同就不更新，保证同一输入切法确定
            if cost < best[end] - 1e-12:
                best[end] = cost
                previous[end] = start
    if best[size] == infinity:
        return []  # 没有任何可行切法：交给上一级/预算切，别在这里硬凑
    cuts: list[int] = []
    end = size
    # 从末尾沿 previous 回溯到 0，得到的是倒序切点，最后排序成升序
    while end > 0:
        start = previous[end]
        if start > 0:
            cuts.append(start)
        end = start
    return sorted(cuts)
