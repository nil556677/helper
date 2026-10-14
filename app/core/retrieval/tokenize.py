"""中文分词：写入与查询必须用**同一套**切法。

FTS5 索引的是 ``chunks.tokens``（jieba 预分词后用空格连起来），正文在 ``chunks.text``。
SQLite 的 ``unicode61`` 分词器把连续汉字当成一个 token——"合同违约金"整串是一个词，
搜"违约"一条都命中不了，所以必须预分词。

**为什么关掉 HMM**（``HMM=False``）：保证入库端与查询端切法稳定一致。开着 HMM 时新词发现
可能让同一个词在不同上下文里切法不同，表现就是"明明有却搜不到"。

**已知取舍**：单字查询、未登录词可能命不中（它们在索引里不是一个独立 token）。要治得靠
单字兜底或换 trigram 分词器——那属于换检索策略，等检索这一版开始做时再定。

分词器按**进程级复用**：jieba 首次加载词典约 1 秒，每切一次新建实例是不可接受的。
"""

from __future__ import annotations

import threading

import jieba

_lock = threading.Lock()
_tokenizer: jieba.Tokenizer | None = None


def tokenizer() -> jieba.Tokenizer:
    """取进程级分词器（首次调用会加载词典，约 1 秒）。"""
    global _tokenizer
    if _tokenizer is None:
        with _lock:
            if _tokenizer is None:
                instance = jieba.Tokenizer()
                instance.initialize()
                _tokenizer = instance
    return _tokenizer


def cut(text: str) -> list[str]:
    """切词（丢掉纯空白片段）。"""
    return [word for word in tokenizer().lcut(text, HMM=False) if word.strip()]


def to_tokens(text: str) -> str:
    """把正文变成索引文本（空格分隔）——写进 ``chunks.tokens`` 的那一列。"""
    return " ".join(cut(text))
