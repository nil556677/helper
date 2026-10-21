"""稠密算法：``last_hidden_state`` → 归一后的 768 维向量。

**为什么这段单独一个文件、而且不 import onnxruntime**：它是整个检索的第一道数学，
也是最容易悄悄错的地方（漏掉 ``keepdims`` 就是一行之差），而它**纯 numpy**——
所以可以不起模型、秒级跑断言（见 `tests/unit/test_embed_dense.py`）。

技术方案 §7 定死的算法（**别顺手改**，检索阈值与跨批可比性都建立在这上面）：

```
CLS → 按行 L2 归一（keepdims）→ 截前 768 维 → 再按行 L2 归一
```

三个"为什么"：

1. **取 CLS 而不是 mean pooling**：bge-m3 的 CLS 位就是句向量（模型卡给的口径）。
   两者打分口径不同，混用会让"同一份资料前后两次检索分数不一样"。
2. **归一必须逐行（``axis=-1, keepdims=True``）**：漏了 keepdims 会把矩阵整体除以一个标量，
   行范数掉到 0.58 → 绝对分被缩放、**跨批不可比**（同一份资料单独查与批量查分数不同）、
   阈值整条链路失效。这是实测踩过的坑，所以 :func:`l2_normalize` 里带断言。
3. **截前 768 维之后再归一一次**：截断会破坏第一次归一的单位长度（截掉的那 256 维有能量），
   不补第二次归一就不是单位向量，余弦相似度会系统性偏小。
"""

from __future__ import annotations

import numpy as np

#: 保留的维度（bge-m3 原始 1024 维，取前 768）。
EMBED_DIMS = 768

#: 行范数的容许误差。float32 上 1.0±1e-3 是正常的（累加 1024 项）。
NORM_TOLERANCE = 1e-3


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """按行做 L2 归一（**逐行**，``keepdims=True``）。

    参数：
        vectors: 形状 ``(n, d)`` 或 ``(d,)``。会被当成 float32 处理。
    返回：同形状的单位向量；零向量原样返回（除零保护）。
    异常：
        ValueError: 维度不是 1 或 2（传错了形状要立刻炸，而不是算出一个怪结果）。
    """
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim not in (1, 2):
        raise ValueError(f"只接受 1 维或 2 维数组，收到 {array.ndim} 维：{array.shape}")

    norms = np.linalg.norm(array, axis=-1, keepdims=True)
    # 零向量的范数是 0：除以它会得到 nan/inf，整批打分全废。保底成 1（结果仍是零向量）。
    safe = np.where(norms > 0, norms, np.float32(1.0))
    return (array / safe).astype(np.float32, copy=False)


def row_norms(vectors: np.ndarray) -> np.ndarray:
    """按行取范数（断言与测试用）。"""
    return np.linalg.norm(np.asarray(vectors, dtype=np.float32), axis=-1)


def to_dense(last_hidden_state: np.ndarray, *, dims: int = EMBED_DIMS) -> np.ndarray:
    """``last_hidden_state [B, L, H]`` → 归一后的 ``[B, dims]`` 句向量。

    参数：
        last_hidden_state: 模型输出，形状 ``(B, L, H)``；也接受单条 ``(L, H)``。
        dims: 保留维度（默认 768）。
    返回：``(B, dims)`` 的 float32 单位向量。
    异常：
        ValueError: 形状不对，或 ``dims`` 超过了模型输出维度（那说明模型与配置不匹配，
            必须炸——静默补零会让所有向量都算错）。
    """
    hidden = np.asarray(last_hidden_state, dtype=np.float32)
    squeeze = hidden.ndim == 2
    if squeeze:
        hidden = hidden[np.newaxis, ...]
    if hidden.ndim != 3:
        raise ValueError(f"last_hidden_state 应该是 (B, L, H)，收到 {hidden.shape}")
    if hidden.shape[0] == 0:
        return np.zeros((0, dims), dtype=np.float32)
    if hidden.shape[2] < dims:
        raise ValueError(
            f"模型输出维度 {hidden.shape[2]} 小于要保留的 {dims} 维：模型与配置不匹配"
        )

    cls = hidden[:, 0, :]  # CLS 位
    normalized = l2_normalize(cls)
    truncated = normalized[:, :dims]
    return l2_normalize(truncated)
