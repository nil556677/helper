"""索引文本（**给嵌入与精排看的那份文本**）：章节上下文 + 原文。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）→ 检索。
切分时用本模块的**预算口径**决定块边界；入库后嵌入/精排再用**同一个函数**拼出真正
送进模型的文本——同一份口径，才不会"切的时候按 300 算、嵌入时变成 330"。

## 输入 / 输出

- 输入：片段原文 + 标题链（``section_path``）+ 是否拼前缀。
- 输出：给嵌入/精排的字符串（**不用于展示**，展示永远用原文）。

## 为什么需要单独一份文本

检索的两条通路要的**不是同一份文本**：

| 用途 | 用哪份 | 理由 |
| --- | --- | --- |
| 引用展示 / 喂大模型 | **原文**（``chunks.text``） | 你定的准则：引用看到的必须是原文，一个字都不许改 |
| 词法（FTS5） | ``chunks.tokens`` | 索引文本本来就与正文分列（v1 的设计） |
| **嵌入 / 精排** | **本模块的** :func:`index_text` | 短块常常"没有主题"（正文只写"乙方应当…"，主题在标题里），拼上标题才有语境 |

**词法那侧标题一直都在**（写进了 ``tokens``）；这里解决的是**向量那侧看不见标题**的问题：
查询"违约金怎么算"如果正文没出现"违约金"三个字，靠正文向量是召不回的。

## 预算怎么算（P1.3 定稿：选项 1B）

**标题开销算进 child 预算**：切分时用的是
``有效预算 = child_size``（由模型常量 ``PAD_TO`` 推导，见 ``chunk/derived.py``），并**按
``index_text(候选)`` 判**——所以标题开销被自动扣掉、**标题永远进得了向量**，代价是正文容量小一点。

为什么不让这里"装不下就把标题砍掉"：那是**静默降级**（没人知道这条块的语境丢了）。
现在的约定是：**切分层保证不超预算**，这里只负责拼；真超了就是 bug，要在切分/入库处暴露。

## 其他约束

1. **用"根 › 叶"面包屑**：短块的主题常常在上层章节（``2.1`` 的正文没有"相关工作"四个字），
   只拼叶子时查询"相关工作/方法"匹配不到。超长时按"整条链 → 根+叶 → 只留叶子"退化；
2. **前缀有长度上限**（:data:`PREFIX_MAX_CHARS`）；
3. **开关进两个指纹**：开了前缀就会改变块的边界（预算变小）→ 改了要重新切分，
   不只是重算向量。

**不用于**：parent 正文（送模型的证据，由提示词组装时单独给 ``section_path``）、
以及任何展示路径。
"""

from __future__ import annotations

from collections.abc import Sequence

#: 前缀（章节面包屑）的字符上限：放得下 2~3 级中文标题，又不至于挤占嵌入窗口
PREFIX_MAX_CHARS = 48

#: 面包屑分隔符（比 "/" 稳：分词器都认，不会变成未知 token，也不会被当成路径）
BREADCRUMB_SEPARATOR = " › "

#: 语境与正文之间的分隔符（换行比特殊符号稳：分词器都认，不会变成未知 token）
SEPARATOR = "\n"


def section_prefix(section_path: Sequence[str] | None, *, max_chars: int = PREFIX_MAX_CHARS) -> str:
    """嵌入用的**语境前缀** = 章节面包屑（"根 › … › 叶"）。

    为什么不再只取最内层：短块常"没有主题"，而主题往往在**上层章节**里
    （``2.1`` 的正文写"该工具…"，"工具"是 H3 给的，但"相关工作"是 H2 给的）。
    只拼叶子时，查询"相关工作/方法"永远匹配不到这些块的标题词。

    超长时的退化顺序（保证前缀一定塞得进预算）：
    1. 整条链（根 → 叶）；
    2. 根 › 叶（去掉中间层）；
    3. 只留叶子（截到 ``max_chars``）。

    参数：
        section_path: 完整标题链；``None`` / 空表示无结构。
        max_chars: 前缀的字符上限。
    返回：面包屑字符串；没有标题时为 ``""``。
    """
    parts = [str(item).strip() for item in (section_path or ()) if str(item).strip()]
    if not parts:
        return ""
    full = BREADCRUMB_SEPARATOR.join(parts)
    if len(full) <= max_chars:
        return full
    if len(parts) == 1:
        return parts[0][:max_chars]
    root_leaf = BREADCRUMB_SEPARATOR.join((parts[0], parts[-1]))
    if len(root_leaf) <= max_chars:
        return root_leaf
    # 连根+叶都超：只能留叶子（截断），至少还有主题词
    return parts[-1][:max_chars]


def index_text(
    text: str,
    section_path: Sequence[str] | None = None,
    *,
    prepend: bool = True,
) -> str:
    """嵌入 / 精排要用的文本（**原文 + 章节前缀**）。

    参数：
        text: 片段原文（``chunks.text``）。
        section_path: 标题链（``chunks.section_path`` 解出来的列表）。
        prepend: 是否拼前缀（由参数 ``chunk.prepend_section`` 决定）。
    返回：前缀 + 分隔符 + 原文；不拼前缀时**原样返回** ``text``。

    **这里不做预算裁剪**：超预算是切分层的责任（见模块说明），在这里悄悄砍前缀
    等于"静默丢语境"——宁可让上层的断言炸出来。
    """
    prefix = section_prefix(section_path) if prepend else ""
    if not prefix:
        return text
    return prefix + SEPARATOR + text
