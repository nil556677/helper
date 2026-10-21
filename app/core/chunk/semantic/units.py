"""切单元/候选：细粒度单元生成与各信号打分（第 1~4 级的输入基础）。

## 在流水线中的位置

语义兜底在 ``ladder`` 里按阶梯尝试信号；本模块提供两件基础设施：
:func:`_units`（把长文本切成 ≈20% 预算的小单元）与
:func:`lexical_similarities` / :func:`embedding_similarities`（相邻单元相似度）。

## 输入 / 输出

- ``_units(text, budget, count)``：输入长文本 + 单元预算，输出单元文本列表。
- ``lexical_similarities(units)``：输出长度 ``n-1`` 的 Jaccard 序列（免费）。
- ``embedding_similarities(units, embed)``：输出余弦序列；失败/形状不对返回 ``None``。

## 关键约定

1. **单元要尽量落在标点上**（``_BREAK_CHARS``）：切在词中间会让相似度全是噪声；
2. **计数口径与切分器一致**（调用方传入 ``count``），否则"单元预算"和"块预算"对不上；
3. 嵌入失败**只降级不抛**：返回 ``None``，由 ``ladder`` 落到下一级（预算切）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from loguru import logger

#: 切单元时愿意"往回退"找标点的最大比例
_BREAK_BACK_RATIO = 0.25

#: 可以当单元边界的位置（标点/空白任意一个都行——这里只求"切得读得通"）
_BREAK_CHARS = "。！？；!?;，、：,.。 \n\t）)】」』"


def _fit_length(text: str, start: int, end: int, budget: int, count: Callable[[str], int]) -> int:
    """``[start, end)`` 里塞进 ``budget`` 的最长长度（二分；token 数随长度单调不减）。

    参数：
        text: 原文。
        start / end: 搜索区间（左闭右开，调用方保证 ``start < end``）。
        budget: 单元预算（token）。
        count: 数 token 的函数。
    返回：``1 ≤ 长度 ≤ end - start``；**至少 1**，保证调用方的游标一定前进。
    """
    low, high = 1, end - start
    best = 1
    while low <= high:
        middle = (low + high) // 2
        if count(text[start : start + middle]) <= budget:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _units(text: str, budget: int, count: Callable[[str], int]) -> list[str]:
    """把长文本切成**细粒度**候选单元（尽量落在标点上）。

    参数：
        text: 待切文本。
        budget: 单个单元的 token 预算（通常 = 块预算 × 20%）。
        count: 数 token 的函数。
    返回：单元文本列表（已去两端空白，空单元不产出）；顺序与原文一致。
    说明：单元只用于"打分找边界"，不直接入库；真正的块由 DP 把相邻单元合并而成。
    """
    units: list[str] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        # 先按预算取最长前缀，再往回找标点：宁可单元短一点，也要切在可读的位置
        step = _fit_length(text, cursor, length, budget, count)
        end = cursor + step
        if end < length:
            # 只往回退不超过 25%：退太多会让单元过短（相似度噪声变大、DP 变慢）
            back = max(cursor + 1, end - int(step * _BREAK_BACK_RATIO))
            for probe in range(end, back - 1, -1):
                if probe > cursor and text[probe - 1] in _BREAK_CHARS:
                    # 边界挪到标点**之后**：标点留在前一个单元末尾
                    end = probe
                    break
        piece = text[cursor:end].strip()
        if piece:
            units.append(piece)
        cursor = end
    return units


def _ngrams(text: str, size: int = 2) -> set[str]:
    """字符 n-gram 集合（**先去掉所有空白**，否则空格会造出无意义的 gram）。

    参数：
        text: 原文。
        size: gram 长度（默认 2，即中文二字组）。
    返回：gram 集合；去空白后为空时返回空集合。
    """
    compact = re.sub(r"\s+", "", text)
    if len(compact) <= size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    """两集合的 Jaccard 相似度。

    参数：
        left / right: 两个 gram 集合。
    返回：``|交集| / |并集|``（``[0, 1]``）；并集为空时返回 0（不除零）。
    """
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def lexical_similarities(units: list[str]) -> list[float]:
    """相邻单元的词面相似度（字组 Jaccard；免费、无模型依赖）。

    参数：
        units: 单元文本列表。
    返回：长度 ``len(units) - 1``（单元少于 2 个时为空列表）；
        第 i 项是单元 i 与 i+1 的相似度，值域 ``[0, 1]``。
    """
    grams = [_ngrams(unit) for unit in units]
    return [_jaccard(grams[index], grams[index + 1]) for index in range(len(units) - 1)]


def embedding_similarities(
    units: list[str], embed: Callable[[Sequence[str]], object]
) -> list[float] | None:
    """相邻单元的余弦相似度（``embed_texts`` 返回逐行归一化的向量 → 点积即余弦）。

    参数：
        units: 单元文本列表。
        embed: 批量嵌入函数（测试可注入假实现）。
    返回：长度 ``len(units) - 1`` 的相似度列表；嵌入失败或返回形状不对时 ``None``。
    异常：**不向外抛**——嵌入失败只降级（上层的语义阶梯会落到下一级/预算切）。
    """
    try:
        import numpy as np

        # float32 足够（这里只做相邻比较、不参与入库向量），内存比 float64 省一半。
        matrix = np.asarray(embed(units), dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(units):
            logger.warning("语义兜底放弃：嵌入结果形状不对 {}", getattr(matrix, "shape", None))
            return None
        # 模型输出已归一化，点积即余弦；这里不再做一次归一化（省一遍矩阵运算）
        return [float(np.dot(matrix[i], matrix[i + 1])) for i in range(len(units) - 1)]
    except Exception as exc:  # 嵌入失败不能让导入失败
        logger.warning("语义兜底：嵌入失败（退到词面/预算切）：{}", exc)
        return None
