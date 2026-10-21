"""连接词边界：口语/书面语里的**话题转折**标记。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
只有"整句放不下、需要在软边界退让"时才调用本模块：连接词与句读标点是**同一级**
（都算软边界），由 ``sentences.soft_spans`` 汇合。

## 输入 / 输出

- 输入：原文 ``text`` + 可选区间 ``[start, end)``。
- 输出：可作为切点的字符下标列表（升序；每个下标是连接词的首字符）。

## 关键约定

1. 只在"段首"位置算边界——前一个字符是开头 / 空白 / 句读标点 / 收尾引号——
   否则会把"不**但**""之**所以**"这类词切碎；
2. **长词优先匹配**（:data:`_ORDERED` 按长度降序），
   例如 ``比如说`` 先于 ``比如``、``总而言之`` 先于 ``总之``；
3. 本模块只**报告位置**，不负责切、也不去重合并（由 ``sentences.soft_spans`` 做）。
"""

from __future__ import annotations

#: 话题转折 / 列举类连接词（**长词优先匹配**）。
TOPIC_SHIFT: tuple[str, ...] = (
    "总的来说",
    "总而言之",
    "换句话说",
    "也就是说",
    "比如说",
    "举个例子",
    "接下来",
    "下面我们",
    "首先",
    "其次",
    "再次",
    "最后",
    "另外",
    "此外",
    "但是",
    "不过",
    "然而",
    "可是",
    "因此",
    "所以",
    "于是",
    "然后",
    "同时",
    "反之",
    "总之",
    "综上",
    "其实",
    "事实上",
    "但",
)

#: 连接词前面出现这些字符时，才认为它处在"段首"。
_BOUNDARY_PREV = frozenset(" \t\n\r。！？；，、：,:!?;…—）】」』”’\"'")


def _by_length_desc() -> tuple[str, ...]:
    """长词优先：``比如说`` 先于 ``比如``（避免短词抢先匹配）。

    返回：按字符长度降序排好的连接词元组（同长度保持声明顺序）。
    说明：只在模块 import 时算一次（常量表不会变），扫描热路径上直接复用。
    """
    return tuple(sorted(TOPIC_SHIFT, key=len, reverse=True))


_ORDERED = _by_length_desc()


def connector_positions(text: str, *, start: int = 0, end: int | None = None) -> list[int]:
    """返回连接词作为"段首"出现的**起始下标**（升序）。

    参数：
        text: 原文。
        start / end: 只在该区间内找（用于递归切分时逐段判断）；
            ``start < 0`` 按 0 处理，``end`` 超过文本长度按文本末尾处理。
    返回：可作为切点的下标列表；没有命中时返回空列表。
    说明：同一位置只记一次（长词优先、命中即 ``break``），**不做去重排序**——
        扫描本身是从左往右的，结果天然升序。
    """
    stop = len(text) if end is None else min(len(text), end)
    out: list[int] = []
    index = max(0, start)
    while index < stop:
        # 段首判定：文本开头，或前一个字符是空白/句读标点/收尾引号。
        # 不这样限定的话，会误伤"不**但**""之**所以**"这类词内部的连接词。
        if index == 0 or (index > 0 and text[index - 1] in _BOUNDARY_PREV):
            for word in _ORDERED:
                if text.startswith(word, index) and index + len(word) <= stop:
                    out.append(index)
                    break
        index += 1
    return out
