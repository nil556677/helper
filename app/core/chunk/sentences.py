"""标点切分：按句末/续句标点切句与切从句（括号内不切、小数点不切）。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
``core.Splitter._pieces`` 先按本模块的"硬句"（句末标点）切成片段；
片段仍然超预算时，再用 :func:`soft_spans` 退到"软边界"（续句标点 + 连接词）。

## 输入 / 输出

- 输入：一段文本（可能只是块内的一段区间）。
- 输出：``(start, end)`` 字符区间（左闭右开）；区间**已去掉两端空白**，
  保证"区间 → 行号/bbox"不把上一行/下一行算进来。

## 关键约定

1. **括号/引号内不切**：用期望闭合符号栈判断深度（``_PAIRS`` + ``_QUOTES``）；
2. **小数点不切**：``.`` 前后都是数字时视为数字/版本号的一部分；
3. 句末标点后紧跟的收尾符号（右括号/引号）**并入本句**，不让它们单独成句；
4. 本模块只负责"找边界"，不负责预算、重叠、合并（那些在 ``core.Splitter``）。
"""

from __future__ import annotations

from collections.abc import Iterator

from ._shared import _CLAUSE_END, _PAIRS, _QUOTE_PAIRS, _QUOTES, _SENTENCE_END, _TRAILING


def _sentence_spans(text: str) -> Iterator[tuple[int, int]]:
    """按句末标点切句，返回 ``(起, 止)`` 字符区间（**括号/引号内不切、小数点不切**）。

    参数：
        text: 待切文本。
    返回：生成器，按出现顺序产出 ``(start, end)``；区间已去两端空白，空句不产出。
    规则：
    - 句末标点 ``。！？；.!?;…`` 收口；后面紧跟的收尾符号（右括号/引号）一起并入；
    - **括号内不切**（用一个期望闭合符号栈判断深度）；
    - 英文句点前后都是数字时不切（``3.14``、``1.2.3`` 是数字/版本号，不是句号）。
    """
    start = 0
    stack: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        # 开括号：压入它对应的闭符号；"（"与"("各记各的，不互相配对
        if char in _PAIRS:
            stack.append(_PAIRS[char])
        # 引号方向不固定（中英文/单双引号），按"配对出现"处理：栈顶是它自己就弹出，否则压栈
        elif char in _QUOTES:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stack.append(char)
        elif stack and char == stack[-1]:
            stack.pop()
        # 只有"栈空"（不在括号/引号内）时，句末标点才算硬句边界
        if char in _SENTENCE_END and not stack:
            if char == "." and _inside_number(text, index):
                index += 1
                continue
            index += 1
            # 连续的句末标点（"……"）、以及跟在后面的右括号/引号都归入本句，
            # 否则 "（完。）" 会被切成 "（完。" + "）" 两段
            while index < length and (text[index] in _SENTENCE_END or text[index] in _TRAILING):
                index += 1
            trimmed = _trim(text, start, index)
            if trimmed is not None:
                yield trimmed
            start = index
            continue
        index += 1
    trimmed = _trim(text, start, length)
    if trimmed is not None:
        yield trimmed


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    """把区间两端的空白收进内侧（**空白不算内容**）。

    参数：
        text: 原文。
        start / end: 要收边的区间（左闭右开）。
    返回：收边后的 ``(start, end)``；区间只剩空白时返回 ``None``。
    为什么必须做：切句时区间起点常落在上一句的换行上；带进区间会让
    "字符偏移 → 行号"多算一行（实测：`第二行。` 被算成上一行）、
    bbox 也会多框一行。定位就是靠这两个映射，差一行就是引用指错行。
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _inside_number(text: str, index: int) -> bool:
    """这个 ``.`` 是否夹在数字之间（``3.14`` → 不是句末）。

    参数：
        text: 原文。
        index: ``.`` 在原文里的下标。
    返回：前后相邻字符都是数字时 ``True``；在文本边界时按非数字处理。
    """
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    return previous.isdigit() and following.isdigit()


def split_sentences(text: str) -> list[str]:
    """把文本按**句末标点**分成句子（保留标点，空句不返回）。

    参数：
        text: 待切文本。
    返回：句子字符串列表；每项已 ``strip()``，空白句被丢弃。
    说明：这是"能读通"的粗切分，不感知预算——预算/合并逻辑在 ``core.Splitter``。
    """
    return [text[a:b].strip() for a, b in _sentence_spans(text) if text[a:b].strip()]


def _clause_spans(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """在 ``[start, end)`` 内按**续句标点**再切（只用于单句超预算的退让）。

    参数：
        text: 原文。
        start / end: 要切的区间（左闭右开），调用方保证 ``start < end``。
    返回：生成器，按顺序产出从句区间；**区间未去空白**（本函数只找断点，
        收边由调用方决定）。
    说明：不处理小数点与收尾符号——续句标点里没有 ``.``，且这条路径只用于
        "整句实在放不下"的降级，不追求句读完美。
    """
    stack: list[str] = []
    piece_start = start
    index = start
    while index < end:
        char = text[index]
        if char in _PAIRS:
            stack.append(_PAIRS[char])
        elif char in _QUOTES:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stack.append(char)
        elif stack and char == stack[-1]:
            stack.pop()
        if char in _CLAUSE_END and not stack:
            # 切点放在续句标点**之后**，标点留在前一个从句末尾（读起来才完整）
            index += 1
            if text[piece_start:index].strip():
                yield piece_start, index
            piece_start = index
            continue
        index += 1
    if text[piece_start:end].strip():
        yield piece_start, end


def soft_spans(
    text: str, start: int, end: int, *, respect_quotes: bool = True
) -> list[tuple[int, int]]:
    """在 ``[start, end)`` 内按**软边界**切：句读标点 + 连接词（段首）。

    参数：
        text: 原文。
        start / end: 要切的区间（左闭右开）；``start >= end`` 返回空列表。
        respect_quotes: 是否把引号内视为"不可切"。默认 True（普通段落不要切进对白中间）；
            对**超预算的长对白**，``_cut`` 会再用 False 试一次——否则整段引号里的
            几百字会被当成一句，逼着切分器去跑昂贵的语义兜底（实测：2 MB 小说
            95/102 个块都这样）。括号仍然受保护，只是不再怕引号。
    返回：带空白收边的 ``(start, end)`` 区间列表（升序、互不重叠）。
        没有边界时返回整段一项；整段只有空白时返回空列表。
    说明：连接词只接受**严格落在区间内部**的位置——区间端点已经是边界，
        再切一刀只会得到空片段。与 :func:`_clause_spans` 的区别是连接词也算边界
        （口语稿里真正的停顿常在这里）。
    """
    from .discourse import connector_positions

    if start >= end:
        return []
    # 连接词位置与标点位置可能重合，用 set 去重后再统一按升序切
    cuts: set[int] = set()
    for position in connector_positions(text, start=start, end=end):
        if start < position < end:
            cuts.add(position)

    stack: list[str] = []
    index = start
    while index < end:
        char = text[index]
        if char in _PAIRS and (respect_quotes or char not in _QUOTE_PAIRS):
            stack.append(_PAIRS[char])
        elif char in _QUOTES:
            if respect_quotes:
                if stack and stack[-1] == char:
                    stack.pop()
                else:
                    stack.append(char)
        elif stack and char == stack[-1]:
            stack.pop()
        if char in _CLAUSE_END and not stack:
            # 切点放在续句标点**之后**：标点留在前一段末尾。
            # 末尾标点后面没有内容，就不加切点（否则会多出一个空尾巴）。
            index += 1
            if index < end:
                cuts.add(index)
            continue
        index += 1

    spans: list[tuple[int, int]] = []
    previous = start
    # 按升序把相邻切点之间的区间收边；切点挨太近时 _trim 返回 None，空段自然被丢弃
    for cut in sorted(cuts):
        span = _trim(text, previous, cut)
        if span is not None:
            spans.append(span)
        previous = cut
    tail = _trim(text, previous, end)
    if tail is not None:
        spans.append(tail)
    return spans
