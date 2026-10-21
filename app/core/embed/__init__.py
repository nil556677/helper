"""嵌入：文本 → 768 维单位向量（bge-m3，本地 ONNX，不出网）。

对外只用三个函数：

```python
vector = embed_text("违约金怎么算")          # (768,) float32，范数 1.0
matrix = embed_texts(["第一句", "第二句"])   # (2, 768)，逐行归一
is_loaded()                                  # 界面据此决定要不要显示「正在加载模型…」
```

**尺寸换算用 0.69 token/字符**（中文实测）：估算耗时、切分预算、进度都靠它。

分层：``dense``（纯 numpy 的数学，可脱离模型单测）· ``tokenization``（文本 → id）·
``model``（ONNX 会话、线程、单例、锁）。**别在别处直接 import onnxruntime**。
"""

from app.core.embed.dense import EMBED_DIMS, NORM_TOLERANCE, l2_normalize, row_norms, to_dense
from app.core.embed.model import (
    DEFAULT_BATCH_SIZE,
    INTRA_OP_THREADS,
    MIN_BATCH_ROWS,
    MODEL_NAME,
    PAD_TO,
    EmbedError,
    embed_for_comparison,
    embed_text,
    embed_texts,
    is_loaded,
    set_load_hook,
    unload,
)

#: 中文字符 → token 的换算系数（实测 0.69），一切尺寸/耗时估算用它。
TOKENS_PER_CHAR = 0.69

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EMBED_DIMS",
    "INTRA_OP_THREADS",
    "MIN_BATCH_ROWS",
    "MODEL_NAME",
    "NORM_TOLERANCE",
    "PAD_TO",
    "TOKENS_PER_CHAR",
    "EmbedError",
    "embed_for_comparison",
    "embed_text",
    "embed_texts",
    "is_loaded",
    "l2_normalize",
    "row_norms",
    "set_load_hook",
    "to_dense",
    "unload",
]
