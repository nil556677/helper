"""阶梯本体：五级语义切分阶梯与递归切段（什么样的片段用哪一级征用）。

## 在流水线中的位置

``split_semantically``（``main``）→ 本模块的 :func:`_split_piece` 递归走阶梯 →
需要时调 ``units`` 打分、``dp`` 选点。只在切分器判定"无结构 + 单句超预算"时进入。

## 输入 / 输出

- 输入：一段超预算文本、块预算、计数函数、可选嵌入函数。
- 输出：按原文顺序排好的块文本列表（每个都 ≤ budget）；由 ``main`` 再做总校验。

## 阶梯（廉价信号优先）

| 序 | 级别 | 信号 |
| --- | --- | --- |
| 1 | ``numbered`` | 编号/条款号 |
| 2 | ``turnword`` | 句首转折连接词 |
| 3 | ``lexical`` | 相邻单元字组 Jaccard |
| 4 | ``embedding`` | 相邻单元嵌入余弦 |
| 5 | ``budget`` | 按预算均匀切（无信号兜底） |

## 关键约定

1. **递归**：某级切出来的片段仍超预算，就对这些片段继续用下一级
   （``_split_piece`` 的 ``level + 1``），直到 ≤ budget；
2. **一级不适用就跳过**：信号平坦 / 单元太少 / 嵌入失败都返回"空候选"，
   自动落到下一级——不抛异常、不中断导入；
3. ``used`` 记录真正用过的级别，仅供日志/诊断，不参与切分决策。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from loguru import logger

from app.core.chunk.semantic._shared import (
    _MIN_CHUNK_RATIO,
    _UNIT_RATIO,
    MAX_UNITS,
)
from app.core.chunk.semantic.dp import is_flat, optimal_cuts, smooth
from app.core.chunk.semantic.units import (
    _units,
    embedding_similarities,
    lexical_similarities,
)

#: 第 1 级：编号/条款号（作者显式写的边界）。
#: **不能只认行首**——我们的触发场景恰恰是"整段没有换行"的长文本，
#: 只认行首等于这一级永远用不上（实测踩过）。所以：行首 **或** 句末标点之后都算编号。
_NUMBERED_BREAK = re.compile(
    r"(?:(?<=[。！？；!?;…\n])|^)(?:"
    r"第\s*[一二三四五六七八九十百零〇\d]+\s*[条章节款项]|"
    r"[（(]\s*[一二三四五六七八九十\d]{1,3}\s*[）)]|"
    r"[一二三四五六七八九十]{1,3}\s*[、.．]|"
    r"\d{1,2}(?:\.\d{1,2}){1,3}\s*[、.．]?\s"
    r")"
)

#: 第 2 级：话题转折的连接词（出现在**句首/段首**才算）
_TURN_WORDS = (
    "综上",
    "总之",
    "另外",
    "此外",
    "但是",
    "然而",
    "不过",
    "因此",
    "所以",
    "据此",
    "同时",
    "反之",
    "换言之",
    "另一方面",
    "接下来",
    "具体而言",
)


def _split_by_pattern(text: str, pattern: re.Pattern[str]) -> list[str]:
    """按零宽匹配的位置切开（切点落在匹配处之前）。

    参数：
        text: 待切文本。
        pattern: 带零宽断言的编号正则（见 :data:`_NUMBERED_BREAK`）。
    返回：按编号切出的片段（去空白、丢空段）；匹配点少于 2 个时返回空列表
        （一个点切不开，等于没切）。
    说明：匹配到的编号本身归到**后一段**（切点在匹配之前），
        这样每段以"第X条/（一）/1.1"开头，可读性最好。
    """
    positions = [match.start() for match in pattern.finditer(text)]
    if len(positions) < 2:
        return []
    pieces: list[str] = []
    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(text)
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
    if pieces and positions[0] > 0:
        # 第一个编号之前还有正文：单独成段（不能因为"没编号"就丢掉）
        head = text[: positions[0]].strip()
        if head:
            pieces.insert(0, head)
    return pieces


def _split_by_turn_words(text: str) -> list[str]:
    """在**句首出现转折连接词**处切开。

    参数：
        text: 待切文本。
    返回：按连接词切出的片段；切不出一段以上时返回空列表（交给下一级）。
    说明：只在句末标点/换行**之后**找连接词——正文中间的"但是"不是话题边界；
        ``lstrip`` 之后的偏移要加回空白长度，才能得到原文里的真实下标。
    """
    positions: list[int] = []
    for match in re.finditer(r"[。！？；!?;\n]", text):
        after = match.end()
        remainder = text[after:].lstrip()
        if not remainder:
            continue
        offset = after + (len(text[after:]) - len(remainder))
        if any(remainder.startswith(word) for word in _TURN_WORDS):
            positions.append(offset)
    if len(positions) < 1:
        return []
    pieces: list[str] = []
    start = 0
    for position in positions:
        piece = text[start:position].strip()
        if piece:
            pieces.append(piece)
        start = position
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    # 只有一个切点也至少两段；拼不回来就返回空，让下一级处理
    return pieces if len(pieces) > 1 else []


#: 各级的名字（日志与测试用）
LEVEL_NUMBERED = "numbered"
LEVEL_TURN_WORD = "turnword"
LEVEL_LEXICAL = "lexical"
LEVEL_EMBEDDING = "embedding"
LEVEL_BUDGET = "budget"


def _merge_units(units: list[str], cuts: list[int]) -> list[str]:
    """按切点把单元合并成块。

    参数：
        units: 细粒度单元。
        cuts: 切点列表（升序；``i`` 表示第 i 个单元是新块开头）；空 = 不切。
    返回：合并后的块文本（去空白、丢空段）；无切点时返回整段一项。
    """
    if not cuts:
        return ["".join(units).strip()]
    pieces: list[str] = []
    start = 0
    # 末尾补一个 len(units)，让最后一块也被收进来
    for cut in [*cuts, len(units)]:
        piece = "".join(units[start:cut]).strip()
        if piece:
            pieces.append(piece)
        start = cut
    return pieces


def budget_split(text: str, budget: int, count: Callable[[str], int]) -> list[str]:
    """第 5 级：按预算把文本切成块（对齐细单元，不做语义判断）。

    **必须对"拼好的候选"重新计数**，不能把各单元的 token 数相加——计数不可加：
    字符估算有取整（5 个 88 字符单元各 60 → 相加 300，拼起来 440 字符实际 303），
    真分词器还会在拼接边界合并 token。相加会让块**悄悄超预算**（实测踩过：
    300 的预算切出 303 → 进向量被截尾）。

    参数：
        text: 待切文本（已经过前 4 级，仍超预算）。
        budget: 单块 token 上限。
        count: 数 token 的函数。
    返回：按顺序切好的块（去空白、丢空段）；文本只有空白时返回空列表。
    """
    # 细单元 = 20% 预算：与打分口径一致，合并时才有"几个单元凑一块"的意义
    unit_budget = max(1, int(budget * _UNIT_RATIO))
    units = _units(text, unit_budget, count)
    if len(units) <= 1:
        return [text.strip()] if text.strip() else []
    pieces: list[str] = []
    start = 0
    while start < len(units):
        end = start + 1
        # 逐个单元试拼；每加一个都按"拼好的文本"重新计数（不可累加）
        while end < len(units) and count("".join(units[start : end + 1])) <= budget:
            end += 1
        pieces.append("".join(units[start:end]).strip())
        start = end
    return [piece for piece in pieces if piece]


def enforce_budget(pieces: list[str], budget: int, count: Callable[[str], int]) -> list[str]:
    """兜底保证：把仍然超预算的片段再切（二分找"塞得进的最长前缀"）。

    为什么还需要它：DP 的可行性判断用的是**单元计数的前缀和**（要 O(1) 才好算），
    而计数不可加 → 选出来的块可能比预算大几个 token。这一遍按**真实计数**收口：
    宁可硬切几个字符，也不让块超预算（超了就是尾部被静默截断）。

    参数：
        pieces: 已切好的块（可能有几块仍超预算）。
        budget: 单块 token 上限。
        count: 数 token 的函数。
    返回：按顺序重切后的块（去空白、丢空段）；每块都 ≤ budget。
    """
    result: list[str] = []
    for piece in pieces:
        if count(piece) <= budget:
            result.append(piece)
            continue
        cursor = 0
        while cursor < len(piece):
            # 二分找"从 cursor 起能塞进预算的最长前缀"；至少前进 1，避免死循环
            low, high, best = 1, len(piece) - cursor, 1
            while low <= high:
                middle = (low + high) // 2
                if count(piece[cursor : cursor + middle]) <= budget:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            result.append(piece[cursor : cursor + best])
            cursor += best
    return [piece for piece in result if piece.strip()]


def _score_level(
    units: list[str],
    *,
    level: str,
    embed: Callable[[Sequence[str]], object] | None,
) -> list[float] | None:
    """按级别算相似度序列；这一级不适用（拿不到/太平坦）返回 ``None``。

    参数：
        units: 细粒度单元。
        level: 级别名（``lexical`` / ``embedding``；其余返回 ``None``）。
        embed: 嵌入函数；``None`` 时第 4 级不可用。
    返回：平滑后的相似度序列（长度 ``n-1``）；信号平坦或嵌入不可用时 ``None``。
    """
    if level == LEVEL_LEXICAL:
        scores = lexical_similarities(units)
    elif level == LEVEL_EMBEDDING:
        if embed is None:
            return None
        scores = embedding_similarities(units, embed)
        if scores is None:
            return None
    else:  # pragma: no cover - 只由上面的分支调用
        return None
    scores = smooth(scores)
    if is_flat(scores):
        # 平坦说明"这级信号分不出边界"：升级下一级，别按噪声切
        logger.debug("语义兜底：{} 这一级信号平坦，升级下一级", level)
        return None
    return scores


def _split_piece(
    text: str,
    budget: int,
    count: Callable[[str], int],
    embed: Callable[[Sequence[str]], object] | None,
    *,
    level: int = 0,
    used: list[str] | None = None,
) -> list[str]:
    """按阶梯递归切一段文本（返回的每一块都 ≤ ``budget``）。

    参数：
        text: 当前要切的文本（可能是上一级切出来的一个候选片段）。
        budget: 单块 token 上限。
        count: 数 token 的函数。
        embed: 嵌入函数（第 4 级用）；``None`` 时跳过第 4 级。
        level: 当前尝试的级别下标（0 = 编号；递归时 +1）。
        used: 可选收集器，记录真正用过的级别名（日志/诊断用）。
    返回：按原文顺序切好的块；递归到第 5 级时一定返回结果（不再递归）。
    说明：这是"阶梯"的调度器——某一级切出多段就对这些段继续下一级；
        切不动就整段交给下一级，绝不在这一级反复尝试。
    """
    # 终止条件：这段已经放得下，不需要任何切分
    if count(text) <= budget:
        return [text]
    # 固定顺序：越靠前越便宜、越可信；第 5 级（budget）不在列表里（它是兜底）
    levels = (LEVEL_NUMBERED, LEVEL_TURN_WORD, LEVEL_LEXICAL, LEVEL_EMBEDDING)
    if level >= len(levels):
        if used is not None:
            used.append(LEVEL_BUDGET)
        return budget_split(text, budget, count)

    name = levels[level]
    candidates: list[str] = []
    if name == LEVEL_NUMBERED:
        candidates = _split_by_pattern(text, _NUMBERED_BREAK)
    elif name == LEVEL_TURN_WORD:
        candidates = _split_by_turn_words(text)
    else:
        unit_budget = max(1, int(budget * _UNIT_RATIO))
        units = _units(text, unit_budget, count)
        # 单元太少（1 个）没法比较；太多（> MAX_UNITS）DP 是 O(n²)，会拖垮一次导入
        if 1 < len(units) <= MAX_UNITS:
            scores = _score_level(units, level=name, embed=embed)
            # 这一级不可用（信号平坦/嵌入失败）：candidates 保持为空，落到下一级
            if scores is not None:
                cuts = optimal_cuts(
                    units,
                    scores,
                    budget=budget,
                    minimum=max(1, int(budget * _MIN_CHUNK_RATIO)),
                    count=count,
                )
                if cuts:
                    candidates = _merge_units(units, cuts)

    if len(candidates) > 1:
        # 这一级切成功了：记录级别，再对每个候选片段递归下一级（它可能仍超预算）
        if used is not None:
            used.append(name)
        result: list[str] = []
        for candidate in candidates:
            result.extend(
                _split_piece(candidate, budget, count, embed, level=level + 1, used=used)
            )
        return result
    # 这一级没切动（或只切出一段）：整段交给下一级，不在原地重试
    return _split_piece(text, budget, count, embed, level=level + 1, used=used)
