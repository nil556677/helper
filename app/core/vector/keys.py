"""向量 key 的**唯一定义**（`doc:version:seq`）：写入、回填、切换都按它拼与拆。

为什么单拎出来：这个形状以前在 ``ingest/embedding.py``（拼 key）和
``vector/qdrant_store.py``（把 key 换成 point id）各写一遍。两处漂移的后果很隐蔽——
向量**写进去了**，但检索/切换按另一种形状去找，永远对不上，而且不报错。
低层（vector）不能 import 高层（features.rag），所以约定放在这里，两边都往下看。
"""

from __future__ import annotations

KEY_SEP = ":"


def make_key(doc_id: int, version: int, seq: int) -> str:
    """拼向量 key：``doc:version:seq``（三段都是十进制整数）。"""
    return f"{int(doc_id)}{KEY_SEP}{int(version)}{KEY_SEP}{int(seq)}"


def parse_key(key: str) -> tuple[int, int, int]:
    """拆向量 key；形状不对抛 ``ValueError``（调用方要么修数据、要么跳过并告警）。"""
    parts = str(key).split(KEY_SEP)
    if len(parts) != 3:
        raise ValueError(f"向量 key 形状不对（期望 doc:version:seq）：{key!r}")
    doc_id, version, seq = (int(part) for part in parts)
    return doc_id, version, seq
