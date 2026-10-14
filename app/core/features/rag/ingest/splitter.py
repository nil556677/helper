"""切分：把一篇正文变成片段（检索的最小单位）。

**当前是"先能用"的版本**（用户要求：切分与检索策略等界面交互定稿后再谈）：

- 空行分段 → 段内按句末标点收口 → 相邻句累计到不超过上限就并成一块；
- **跨段落不合并**：空行是作者给的语义边界，把两段并成一块正是"多主题混在一起、分数被稀释"
  的来源（参考 DSH 的实测口径：混在一起的页级块 0.039，只含相关句的块 0.26）；
- 单句超上限时硬切。

``CHUNK_MAX_CHARS`` 是**上限不是目标**，而且它是**索引期参数**：改了必须重建索引
（否则同一份资料里新老切法混在一起）。
"""

from __future__ import annotations

import re

#: 单个片段的字数上限（索引期参数）
CHUNK_MAX_CHARS = 300

#: 句末标点：这些字符之后是一个自然的断点
_SENTENCE_END = "。！？；!?;"

#: 段落分隔：一个或多个空行
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


def _hard_split(text: str, size: int) -> list[str]:
    """整段没有标点时的兜底：按固定字数硬切（总比切成几千字一块强）。"""
    return [text[i : i + size] for i in range(0, len(text), size)]


def _sentences(paragraph: str) -> list[str]:
    """把一个段落切成句子（保留标点）。"""
    parts: list[str] = []
    buffer = ""
    for char in paragraph:
        buffer += char
        if char in _SENTENCE_END:
            if buffer.strip():
                parts.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        parts.append(buffer.strip())
    return parts


def split_text(text: str, *, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """切分正文。

    参数：
        text: 原文（已解析成纯文本）。
        max_chars: 片段字数上限。
    返回：片段列表（已 strip，空片段不返回）。
    """
    chunks: list[str] = []

    for paragraph in _PARAGRAPH_SPLIT.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        current = ""
        for sentence in _sentences(paragraph):
            pieces = _hard_split(sentence, max_chars) if len(sentence) > max_chars else [sentence]
            for piece in pieces:
                if current and len(current) + len(piece) <= max_chars:
                    current += piece
                else:
                    if current:
                        chunks.append(current)
                    current = piece
        if current:
            chunks.append(current)

    return [chunk.strip() for chunk in chunks if chunk.strip()]
