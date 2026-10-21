"""**仅用于基准脚本**的进程内矩阵打分（生产检索不经过这里）。

历史背景：这里曾是实现"整块 float32 矩阵 + 一次 BLAS 乘法"的生产打分缓存。向量层重构后
（docs/向量层重构设计.md，schema v11 起 ``matrices`` / ``vector_keys`` 两张表已被 DROP），
生产路径只认 Qdrant：``app/core/vector/api.py`` 是唯一入口，检索走 ``query_points``。

保留这个模块的唯一理由：``scripts/bench_models.py`` 要复现"一次矩阵乘法 10 万条只要 ~7 ms"
这组实测数字（文章与选型依据都引用了它）。因此这里**只有纯内存实现**，
任何 ``SQLite`` 相关函数都已移除——别再往这里加读写库的东西。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: 一份矩阵的内存估算（字节）：数据 + 键表的大致开销（键是字符串，按 30 字节/条估）。
BYTES_PER_KEY = 30


@dataclass
class Matrix:
    """内存里的一份打分矩阵（不可变语义：换内容请重新取）。"""

    collection: str
    model: str
    dims: int
    vectors: np.ndarray
    """形状 ``(n, dims)``、float32、**逐行单位向量**。"""

    keys: list[str] = field(default_factory=list)
    """第 i 行对应哪个 key（= Qdrant 的 point id 字符串形式）。"""

    payloads: list[dict[str, object]] = field(default_factory=list)
    """第 i 行的过滤字段（只存 `status/doc_id/version/content_type/page` 这几个）。"""

    updated_at: float = 0.0

    @property
    def count(self) -> int:
        """行数。"""
        return int(self.vectors.shape[0])

    @property
    def nbytes(self) -> int:
        """大致占用（数据 + 键表），用于界面上的"矩阵 293 MB"那类显示。"""
        return int(self.vectors.nbytes) + len(self.keys) * BYTES_PER_KEY

    def index_of(self, key: str) -> int | None:
        """key → 行号；没有就 None（用 dict 而不是 ``list.index()``：后者是 O(n)）。"""
        return self._key_index().get(key)

    def _key_index(self) -> dict[str, int]:
        cached = getattr(self, "_index_cache", None)
        if cached is None:
            cached = {key: position for position, key in enumerate(self.keys)}
            object.__setattr__(self, "_index_cache", cached)
        return cached

    def column(self, field: str) -> list[object] | None:
        """取某个 payload 字段的**一整列**（掩码就是拿它比的）。"""
        if not self.payloads:
            return None
        if not any(field in payload for payload in self.payloads):
            return None
        return [payload.get(field) for payload in self.payloads]

    def mask_equals(self, field: str, value: object) -> np.ndarray | None:
        """``field == value`` 的布尔掩码；没有这一列返回 None。"""
        column = self.column(field)
        if column is None:
            return None
        return np.array([item == value for item in column], dtype=bool)

    def mask_in(self, field: str, values: object) -> np.ndarray | None:
        """``field ∈ values`` 的布尔掩码；没有这一列返回 None。"""
        column = self.column(field)
        if column is None:
            return None
        wanted = set(values)  # type: ignore[arg-type]
        return np.array([item in wanted for item in column], dtype=bool)

    def scores(self, query: np.ndarray, *, mask: np.ndarray | None = None) -> np.ndarray:
        """算一遍分（余弦，因为两边都是单位向量）。被掩掉的位置是 ``-inf``。"""
        if self.count == 0:
            return np.zeros((0,), dtype=np.float32)
        vector = np.asarray(query, dtype=np.float32).reshape(-1)
        if vector.shape[0] != self.dims:
            raise ValueError(
                f"查询维度 {vector.shape[0]} 与矩阵 {self.dims} 不一致（模型换了？）"
            )
        scores = self.vectors @ vector
        if mask is not None:
            masked = np.asarray(mask, dtype=bool)
            if masked.shape[0] != self.count:
                raise ValueError(f"掩码长度 {masked.shape[0]} 与矩阵行数 {self.count} 不一致")
            scores = np.where(masked, scores, np.float32(-np.inf))
        return scores.astype(np.float32, copy=False)


def empty(collection: str, model: str, dims: int) -> Matrix:
    """建一份空矩阵（基准脚本起步用）。"""
    return Matrix(
        collection=collection,
        model=model,
        dims=dims,
        vectors=np.zeros((0, dims), dtype=np.float32),
        keys=[],
        payloads=[],
        updated_at=0.0,
    )


def search(
    matrix: Matrix,
    query: np.ndarray,
    *,
    top_k: int = 10,
    mask: np.ndarray | None = None,
    where: dict[str, np.ndarray] | None = None,
) -> list[tuple[str, float]]:
    """在矩阵里找最像的若干条（``argpartition`` 取候选再排序，只给基准用）。

    参数：
        matrix: 打分矩阵。
        query: ``(dims,)`` 单位向量。
        top_k: 要几条。
        mask: 布尔数组，等价于"按 status=active 过滤"，**先算分再掩码**。
        where: 若干布尔数组，会**与**进同一个掩码（多条件过滤）。
    返回：``[(key, score), ...]``，按分数降序；分数为 -inf 的行不会返回。
    """
    if matrix.count == 0:
        return []

    combined = mask
    if where:
        for condition in where.values():
            array = np.asarray(condition, dtype=bool)
            combined = array if combined is None else (combined & array)

    scores = matrix.scores(query, mask=combined)
    wanted = max(1, int(top_k))
    if wanted >= matrix.count:
        order = np.argsort(-scores)
    else:
        candidates = np.argpartition(-scores, wanted - 1)[:wanted]
        order = candidates[np.argsort(-scores[candidates])]

    results: list[tuple[str, float]] = []
    for position in order:
        score = float(scores[position])
        if not np.isfinite(score):
            continue  # 被掩掉的
        results.append((matrix.keys[int(position)], score))
    return results
