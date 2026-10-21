"""精排：给候选文档按"与查询的相关性"重排（本地 ONNX，不出网）。

对外主要用两个调用：

```python
scores = score_pairs("违约金怎么算", [doc1, doc2, ...])   # (n,) float32，越大越相关
if is_worth_reranking(len(candidates)):                  # 候选 <2 时不必精排
    ...
```

**为什么值得花 2~5 秒**：两路召回（词法 + 向量）各自按自己的量纲排序，融合之后的 top-10
顺序并不精细；精排把"查询与文档一起过一遍模型"，实测能把真正相关的块提到前面去
（技术方案 §9.2 定的默认：候选 10 → 截断 256 → 送模型 top-5）。

**为什么它不在关键路径上**：精排是最慢的一环（10 对 ×256 ≈ 4.9 s）。所以：
失败/超时一律**退化为按融合分排序**（P2.7），而不是让用户看到错误；
候选 <2 直接跳过；同一 (query, chunk) 走 `rerank_cache` 不重复算。

分层：``model`` 管会话、线程、拼对、截断；上层只认"给查询与文档、拿回分数"。
**别在别处直接 import onnxruntime**。
"""

from app.core.rerank.model import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_TOKENS,
    INTRA_OP_THREADS,
    MODEL_NAME,
    RerankError,
    build_pair,
    is_loaded,
    is_worth_reranking,
    score_one,
    score_pairs,
    set_load_hook,
    unload,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_TOKENS",
    "INTRA_OP_THREADS",
    "MODEL_NAME",
    "RerankError",
    "build_pair",
    "is_loaded",
    "is_worth_reranking",
    "score_one",
    "score_pairs",
    "set_load_hook",
    "unload",
]
