"""Qdrant 后端（本地模式）：**向量的事实来源**。

为什么是它、以及为什么不是它做打分（技术方案 §9.4 / §9.9）：

- **事实来源**：向量必须有个"丢了能从别处算出来"的权威副本。Qdrant 本地模式
  （``path=data/qdrant/``）满足这点：它是嵌入式文件库，不装服务、不出网。
- **检索也走它**（向量层重构后的唯一后端）：`api.search` 直接 `query_points`，过滤在
  payload 侧做。进程内矩阵打分（:mod:`app.core.vector.matrix`）**已不再是生产路径**，
  只保留给基准脚本复现实测数字。
- **写入慢，得用对的写法**：实测本地模式逐条 ``upsert`` 只有 149~162 条/秒
  （10 万条要 10~11 分钟），``upload_collection``（整批 numpy）是 **208~211 条/秒（≈8 分钟）**。
  差这一点点正好跨过交付计划的 10 分钟线，所以写入**一律走 upload_collection**
  （按批切，不是一次性把 10 万条塞进去——那样内存峰值太高）。

**单进程锁**：同一个 ``path`` 不能被两个 client 同时打开（实测第二个直接
``RuntimeError: Storage folder … is already accessed``）。这是 P0.7 单实例守卫存在的理由；
这里在拿不到时给一句人话，而不是把 Qdrant 的英文异常抛给用户。

**point id 的算法**（技术方案 §6）：``doc_id * 10^12 + version * 10^5 + seq``。
它让"同一份资料的同一版本同一块"有确定 id：重复导入是**幂等覆盖**，不需要先查再写。
三段各占独立的位宽（见 :data:`DOC_SLOT`），不会互相进位。
"""

from __future__ import annotations

import atexit
import sys
import threading
from collections.abc import Iterator
from functools import wraps
from inspect import isgeneratorfunction
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from app.core import paths
from app.core.config import store as params_store
from app.core.vector.keys import parse_key

#: point id 的进制（见模块说明）：``doc_id * 10^12 + version * 10^5 + seq``。
#: **三个槽位必须互不重叠**：一个 doc_id 占 ``VERSION_BASE * SEQ_BASE`` 个 id，
#: version 占 ``SEQ_BASE`` 个。原实现写成 ``doc_id * VERSION_BASE + version * SEQ_BASE``，
#: 而校验允许 ``version < VERSION_BASE``——version ≥ 100 就会撞进下一个 doc_id 的槽位
#: （实测 ``(0,100,0)`` 与 ``(1,0,0)`` 同为 10^7），Qdrant 按 id 静默覆盖 → 数据丢失。
SEQ_BASE = 100_000
VERSION_BASE = 10_000_000

#: 一个 doc_id 的 id 槽宽（= VERSION_BASE * SEQ_BASE）。
DOC_SLOT = VERSION_BASE * SEQ_BASE

#: Qdrant 的 point id 是 64 位无符号整数；留一点余量，超过就明确报错而不是溢出。
MAX_POINT_ID = (1 << 63) - 1

#: 一批写多少条。实测 5000 与 10000 的吞吐差不多（208 vs 211 条/秒），
#: 取 5000 是为了内存峰值更小（5000 × 768 × 4 B ≈ 15 MB，再加 payload）。
WRITE_BATCH = 5000

#: 断点续跑的游标列（`scroll` 返回的 next_page_offset）。
_client: Any = None
_client_path: str | None = None

#: **最终关闭闸**：`shutdown()` 置真后，`client()` 一律拒绝重开（退出过程中的
#: 界面定时器/残留线程再来查一次，也不会把连接打开——见 `client()` 的说明）。
_final: bool = False

#: 保护 client 的**创建/关闭**。全局单例是"检查后创建"（check-then-act）：
#: 主线程查询与 worker 线程写入并发首次调用时，两个线程会同时 `QdrantClient(path=...)`，
#: 本地嵌入式模式下第二个必抛 `Storage folder … is already accessed`——用户看到的是
#: "向量库被另一个进程占用"（误导）。用 RLock 是因为 client() 内部会调 close()。
_lock = threading.RLock()


def _serialized(fn: Any) -> Any:
    """把**所有 Qdrant 操作**串行化（不只是 client 创建/关闭）。

    为什么必须：本地嵌入式 Qdrant 客户端**不是线程安全的**。长任务在 worker 线程里
    ``upload_collection`` 的同时，界面线程 ``scroll`` 会读到一半旧一半新的内部数组，
    在 qdrant-client 的 ``calculate_payload_mask`` 里直接
    ``IndexError: index N is out of bounds for axis 0 with size N``（实测），
    异常冒到页面构建就是 500 错误页。

    用 ``RLock``：被装饰的函数内部还会调 ``client()``/``has_collection()`` 等，
    同一线程需要可重入。锁只包**单次 Qdrant 调用**；把读操作移出事件循环
    （见 UI 层）后，短暂等待不会冻结界面。

    参数：
        fn: 需要串行化的函数。
    返回：
        包了一层 ``with _lock`` 的函数（保留原签名元数据）。
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _lock:
            return fn(*args, **kwargs)

    # **生成器函数**（scroll_all）：``fn(...)`` 只创建生成器、不执行，锁会在迭代前就释放。
    # 必须在迭代期间持锁，否则并发写入照样能撞坏 scroll 的内部数组。
    if isgeneratorfunction(fn):

        @wraps(fn)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _lock:
                yield from fn(*args, **kwargs)

        return gen_wrapper
    return wrapper

#: 建过 payload 索引的集合（**进程级**）。`ensure_payload_indexes` 每次 ensure 都会
#: 尝试建 9 个索引，服务端模式下每批写入都重复一遍是纯浪费；建过一次就跳过。
_payload_indexed: set[str] = set()

#: 已确认建好的集合（进程级缓存）：`(集合名, 维度)`。
#: `upsert` 每次 flush 都会调 `ensure_collection`，而它要两次元数据往返（has + get）。
#: 缓存后只有进程内第一次（或集合被删后）才真正检查。**删除集合时必须失效**。
_collection_ready: set[tuple[str, int]] = set()


def qdrant_dir() -> Path:
    """Qdrant 的本地目录（``data/qdrant/``；跟着数据目录走、跟着一起备份）。"""
    return paths.data_dir() / "qdrant"


class VectorStoreError(RuntimeError):
    """向量后端不可用（目录被占用 / 写入失败 / 维度不符）。"""


def _models() -> Any:
    """惰性 import qdrant 的 models（没装 qdrant-client 时给出人话，而不是 ImportError）。"""
    try:
        from qdrant_client import models
    except ImportError as exc:  # pragma: no cover - 依赖缺失时才走到
        raise VectorStoreError(
            "没装 qdrant-client，装完再启动（见 requirements.lock.txt）"
        ) from exc
    return models


def client() -> Any:
    """取（必要时打开）Qdrant client —— **单例**。

    两种连接模式（由 ``vector.url`` 决定）：
    - 填了 ``vector.url`` → 连服务端（HNSW / payload 索引都生效）。
    - 留空 → 本地嵌入式（``path=data/qdrant``；无 HNSW，仅开发/测试）。

    **退出后不再重开**（P7 修复）：``shutdown()`` 会把 ``_final`` 置真，此后任何
    ``client()`` 调用直接报"应用正在退出"。没有这道闸，界面定时器在退出过程中还会
    触发一次查询 → 把刚关掉的客户端又打开 → 对象活到解释器卸载，``__del__`` 打出一屏
    ``ImportError: sys.meta_path is None``（实测日志）。

    返回：``QdrantClient``。
    异常：
        VectorStoreError: 本地模式目录被另一个进程占着、服务端连不上，或应用正在退出。
    """
    global _client, _client_path
    if _final or sys.is_finalizing():
        raise VectorStoreError("应用正在退出，向量库连接已关闭")
    config = params_store.load()
    url = str(config["vector.url"] or "").strip()
    api_key = str(config["vector.api_key"] or "").strip()
    timeout = float(config["vector.timeout"] or 30.0)
    # 服务端模式的连接标识：url，方便"换 url/切本地"时重开
    target = url or str(qdrant_dir())
    with _lock:
        if _client is not None and _client_path == target:
            return _client
        if _client is not None:
            # 换了连接目标（测试/切模式常见）：先关掉旧的，别让旧 client 指向不同的库
            _close_locked()
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover
            raise VectorStoreError("没装 qdrant-client（见 requirements.lock.txt）") from exc

        if url:
            # 服务端模式：请求级超时 + 可选鉴权
            kwargs: dict[str, Any] = {
                "url": url,
                "timeout": timeout,
                "check_compatibility": False,
            }
            if api_key:
                kwargs["api_key"] = api_key
            try:
                _client = QdrantClient(**kwargs)
            except Exception as exc:  # qdrant 异常类型不稳定，统一成人话
                raise VectorStoreError(f"连不上 Qdrant 服务（{url}）：{exc}") from exc
            _client_path = target
            return _client

        folder = qdrant_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            _client = QdrantClient(path=str(folder))
        except RuntimeError as exc:
            # 本地模式是**单进程**的：另一个 helper 还开着这个目录
            raise VectorStoreError(
                f"向量库被另一个进程占用（{folder}）：{exc}"
                "。请先关掉另一个 helper 窗口再试。"
            ) from exc
        _client_path = target
        logger.debug("Qdrant 本地模式已打开：{}", folder)
        return _client


def _close_locked() -> None:
    """关掉 client（**调用方必须已持有 ``_lock``**）。"""
    global _client, _client_path
    if _client is None:
        return
    try:
        _client.close()
    except Exception as exc:  # 关不掉的唯一后果是下次打开要等锁释放，不该挡住退出
        logger.warning("关闭 Qdrant client 失败（已忽略）：{}", exc)
    _client = None
    _client_path = None
    _payload_indexed.clear()
    _collection_ready.clear()


def close() -> None:
    """关掉 client（测试与"重启连接"用；不关会让下一次打开撞上单进程锁）。

    **退出进程请用 :func:`shutdown`**：`close()` 只关连接、之后 `client()` 还会重开；
    `shutdown()` 会把 `_final` 置真，退出过程中的任何查询都不会再把连接打开。
    """
    with _lock:
        _close_locked()


def shutdown() -> None:
    """**退出专用**：先置"最终关闭"闸，再关连接（之后 `client()` 一律报"正在退出"）。"""
    global _final
    _final = True
    with _lock:
        _close_locked()


def _atexit_close() -> None:
    """解释器退出兜底：**在模块卸载之前**把连接关掉（避免 __del__ 落到 meta_path 已清空的时刻）。"""
    global _final
    _final = True
    try:
        with _lock:
            _close_locked()
    except Exception:  # 退出路径不抛：关不掉也只是多等一个锁
        pass


atexit.register(_atexit_close)


def point_id(doc_id: int, version: int, seq: int) -> int:
    """算 point id（**确定性且单射**：同一份资料的同一版本同一块永远是同一个 id）。

    这样重复导入就是幂等覆盖，不需要"先查有没有、再决定插还是改"。

    编码：``doc_id * 10^12 + version * 10^5 + seq``。三个槽位互不重叠，
    因此 ``(doc_id, version, seq)`` ↔ id 是**一一对应**。

    异常：
        ValueError: seq/version 超出各自槽位，或 doc_id 过大导致结果超出 64 位 id 空间。
    """
    doc_id, version, seq = int(doc_id), int(version), int(seq)
    if doc_id < 0:
        raise ValueError(f"doc_id={doc_id} 不能为负")
    if seq < 0 or seq >= SEQ_BASE:
        raise ValueError(f"seq={seq} 超出 point id 空间（0 ≤ seq < {SEQ_BASE}）")
    if version < 0 or version >= VERSION_BASE:
        raise ValueError(f"version={version} 超出 point id 空间（0 ≤ version < {VERSION_BASE}）")
    number = doc_id * DOC_SLOT + version * SEQ_BASE + seq
    if number > MAX_POINT_ID:
        raise ValueError(
            f"doc_id={doc_id} 过大：point id {number} 超出 Qdrant 的 64 位 id 空间"
        )
    return number


def parse_point_id(value: int) -> tuple[int, int, int]:
    """把 point id 拆回 ``(doc_id, version, seq)``（排查与对账用）。"""
    number = int(value)
    return (
        number // DOC_SLOT,
        (number % DOC_SLOT) // SEQ_BASE,
        number % SEQ_BASE,
    )


@_serialized
def has_collection(name: str) -> bool:
    """集合在不在。"""
    store = client()
    return any(item.name == name for item in store.get_collections().collections)


@_serialized
def ensure_collection(name: str, dims: int) -> bool:
    """保证集合存在且维度正确。返回"这次是不是新建了"。

    参数：
        name: 集合名。
        dims: 向量维度。
    异常：
        VectorStoreError: 已有集合的维度与要求不符（换了模型但没重建 → 必须说清，
            否则写进去的点会被 Qdrant 拒绝，而错误信息会很难懂）。
    """
    models = _models()
    store = client()
    cache_key = (name, int(dims))
    if cache_key in _collection_ready:
        return False
    if has_collection(name):
        info = store.get_collection(name)
        params = info.config.params.vectors
        size = getattr(params, "size", None)
        if size is not None and int(size) != int(dims):
            raise VectorStoreError(
                f"集合 {name} 的维度是 {size}，当前模型是 {dims} 维："
                "换了嵌入模型，需要全量重建（或换一个集合名）"
            )
        _collection_ready.add(cache_key)
        return False
    try:
        store.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=int(dims),
                distance=models.Distance.COSINE,
                # HNSW：服务端生效；本地模式不建索引、语法上接受（见模块说明）。
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
            ),
        )
    except Exception as exc:
        # **并发建集合**：两个线程同时"判定不存在 → 创建"，第二个必然报 already exists。
        # 这不是错误（集合已经在、维度也刚校验过），吞掉即可；其他异常照抛。
        if has_collection(name):
            logger.debug("集合 {} 已被并发创建（忽略重复创建）", name)
            _collection_ready.add(cache_key)
            return False
        raise VectorStoreError(f"建集合 {name} 失败：{exc}") from exc
    logger.info("已建向量集合：{}（{} 维，余弦）", name, dims)
    _collection_ready.add(cache_key)
    return True


#: 固定的 payload 索引（每次检索都要过滤/排序热字段）。动态属性索引见 §7，灌数据前建。
FIXED_PAYLOAD_INDEXES: dict[str, str] = {
    "documents_id": "integer",
    "version": "integer",
    "status": "keyword",
    "content_type": "keyword",
    "page": "integer",
    # 动态字段（P6）：attrs 数组的 key/value 与 attrs_flat
    "attrs[].key": "keyword",
    "attrs[].value": "keyword",
    "attrs_num[].key": "keyword",
    "attrs_num[].value": "float",
    "attrs_date[].key": "keyword",
    "attrs_date[].value": "keyword",
    # 日期范围用 epoch 整数（value 是 ISO 串，仅精确匹配）
    "attrs_date[].epoch": "integer",
    "attrs_flat": "keyword",
    # 动态字段提取版本：backfill 按它筛"落后"的点
    "attrs_version": "integer",
}


@_serialized
def ensure_payload_indexes(collection: str) -> None:
    """给集合建固定的 payload 索引（幂等）。

    **必须早于灌数据**调用，否则 HNSW 的 filterable 边不会带上这些字段。
    服务端生效；本地模式无索引（会 WARNING，忽略即可）。

    **进程内缓存**：建过就跳过——`ensure()` 会在每批写入路径上被调用，服务端模式下
    每次都重复十几次建索引请求是纯浪费（实测每批多出几十毫秒）。
    """
    if collection in _payload_indexed:
        return
    models = _models()
    store = client()
    for field, schema in FIXED_PAYLOAD_INDEXES.items():
        try:
            store.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=getattr(models.PayloadSchemaType, schema.upper()),
            )
        except Exception as exc:  # 索引建失败不该挡写入：只是变慢
            logger.warning("建 payload 索引失败（已忽略，{}/{}）：{}", collection, field, exc)
    _payload_indexed.add(collection)


@_serialized
def ensure_vectorless_collection(name: str) -> bool:
    """建**无向量**集合（parent 用；``vectors_config={}``，纯 payload），返回是否新建。

    Qdrant 的 point 必须带向量字段，但传 `vector={}`（空字典）即为"payload-only 点"，
    只能按 id/filter/scroll/count 取，不能做相似度检索——正是 parent 需要的。
    """
    store = client()
    if has_collection(name):
        return False
    store.create_collection(collection_name=name, vectors_config={})
    logger.info("已建无向量集合：{}", name)
    return True


@_serialized
def upsert(
    collection: str,
    dims: int,
    ids: list[int],
    vectors: np.ndarray,
    payloads: list[dict[str, Any]],
) -> int:
    """批量写入（幂等：同 id 覆盖）。返回写了几条。

    参数：
        collection: 集合名。
        dims: 维度（用来确保集合建对）。
        ids: point id 列表（见 :func:`point_id`）。
        vectors: ``(len(ids), dims)`` 单位向量。
        payloads: 每条一份过滤/回填字段（``chunk_id/doc_id/version/status/content_type/page``）。
    异常：
        VectorStoreError: 数量对不上、维度不符、或写入失败。
    """
    if not ids:
        return 0
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.shape[0] != len(ids) or len(payloads) != len(ids):
        raise VectorStoreError(
            f"ids/vectors/payloads 数量不一致：{len(ids)} / {matrix.shape[0]} / {len(payloads)}"
        )
    if matrix.shape[1] != int(dims):
        raise VectorStoreError(f"向量维度 {matrix.shape[1]} 与集合维度 {dims} 不一致")

    ensure_collection(collection, dims)
    store = client()
    written = 0
    # 分批：一次塞 10 万条会让内存峰值很高，而吞吐几乎一样（实测 208 vs 211 条/秒）
    for start in range(0, len(ids), WRITE_BATCH):
        stop = start + WRITE_BATCH
        window = matrix[start:stop]
        # **用 upload_collection 而不是逐条 PointStruct**：实测 208 条/秒 vs 162，
        # 正好跨过"10 万条 <10 分钟"这条线（见模块说明）。
        try:
            store.upload_collection(
                collection_name=collection,
                vectors=window,
                payload=payloads[start:stop],
                ids=ids[start:stop],
            )
        except Exception as exc:  # qdrant 异常类型不稳定，统一成人话
            raise VectorStoreError(f"写入向量失败（第 {start} 条起）：{exc}") from exc
        written += len(ids[start:stop])
    return written


@_serialized
def set_payload(collection: str, ids: list[int], patch: dict[str, Any]) -> int:
    """给已有的 point 打 payload 补丁（P1.7 版本切换用：旧版本→superseded、新版本→active）。

    参数：
        collection: 集合名。
        ids: point id 列表。
        patch: 要合并进 payload 的字段（Qdrant 的 ``set_payload`` 是**合并**，不是替换）。
    返回：处理了几条。
    异常：
        VectorStoreError: 写入失败。
    """
    if not ids:
        return 0
    store = client()
    written = 0
    for start in range(0, len(ids), WRITE_BATCH):
        window = ids[start : start + WRITE_BATCH]
        try:
            store.set_payload(collection_name=collection, payload=patch, points=window)
        except Exception as exc:
            raise VectorStoreError(f"更新向量 payload 失败（第 {start} 条起）：{exc}") from exc
        written += len(window)
    return written


@_serialized
def query(
    collection: str,
    vector: np.ndarray,
    *,
    limit: int = 50,
    where: dict[str, Any] | None = None,
    dims: int | None = None,
) -> list[dict[str, Any]]:
    """按向量找最近的若干条（**带过滤**）。

    参数：
        collection: 集合名。
        vector: ``(dims,)`` 查询向量（单位向量）。
        limit: 要几条。
        where: 过滤条件，如 ``{"status": "active", "doc_id": 3}``；
            值是标量时用等值匹配，是列表时用"任一个"匹配（``MatchAny``）。
        dims: 期望维度；给了就先校验（**空向量/维度不符在这里明确报错**，
            而不是把一坨形状不对的数字丢给 Qdrant，让用户看底层英文异常）。
    返回：``[{"id", "score", "payload"}, ...]``，按相似度降序。
    """
    if not has_collection(collection):
        return []
    flat = np.asarray(vector, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        raise VectorStoreError(f"查询向量是空的（{collection}）")
    if dims is not None and flat.shape[0] != int(dims):
        raise VectorStoreError(
            f"查询向量维度 {flat.shape[0]} 与配置的 {int(dims)} 不一致（模型换了？）"
        )
    models = _models()
    store = client()

    conditions = []
    for key, value in (where or {}).items():
        if isinstance(value, (list, tuple, set)):
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=list(value)))
            )
        else:
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
    query_filter = models.Filter(must=conditions) if conditions else None

    try:
        response = store.query_points(
            collection_name=collection,
            query=flat.tolist(),
            limit=max(1, int(limit)),
            query_filter=query_filter,
            with_payload=True,
        )
    except Exception as exc:
        raise VectorStoreError(f"向量查询失败（{collection}）：{exc}") from exc

    return [
        {"id": int(point.id), "score": float(point.score), "payload": dict(point.payload or {})}
        for point in response.points
    ]


@_serialized
def count(collection: str) -> int:
    """集合里的点数（**精确**：对账要的是真数，不是估算）。"""
    if not has_collection(collection):
        return 0
    return int(client().count(collection_name=collection, exact=True).count)


@_serialized
def delete_points(collection: str, ids: list[int]) -> int:
    """按 id 删点（删资料/删版本时用）。返回请求删除的条数。

    **分批**：服务端模式下单请求点数有上限（实测几千条就会 413/超时），
    按 1000 一批删，失败抛给调用方（调用方据此决定要不要继续删 DB 行）。
    """
    if not ids or not has_collection(collection):
        return 0
    models = _models()
    ids = list(ids)
    for start in range(0, len(ids), 1000):
        client().delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=ids[start : start + 1000]),
        )
    return len(ids)


@_serialized
def delete_points_where(collection: str, where: dict[str, object]) -> int:
    """按 **payload 过滤**删点（Qdrant 服务端筛选，免去调用方先 scroll 一遍预扫描）。

    与 :func:`delete_points` 的差别：不需要先知道 point id，直接按 payload 字段
    （如 ``documents_id``）在服务端匹配删除。本地模式同样支持（逐点判断由客户端模拟）。
    """
    if not has_collection(collection):
        return 0
    models = _models()
    conditions = []
    for key, value in where.items():
        if isinstance(value, (list, tuple, set)):
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=list(value)))
            )
        else:
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
    if not conditions:
        raise VectorStoreError("delete_points_where 需要至少一个过滤条件（防误删全集合）")
    before = int(client().count(collection_name=collection, exact=True).count)
    client().delete(
        collection_name=collection,
        points_selector=models.FilterSelector(filter=models.Filter(must=conditions)),
    )
    after = int(client().count(collection_name=collection, exact=True).count)
    return max(0, before - after)


@_serialized
def delete_collection(name: str) -> bool:
    """删掉整个集合（"清空某集合"那个二次确认的动作）。"""
    if not has_collection(name):
        return False
    client().delete_collection(collection_name=name)
    # 缓存失效：否则后续写入会以为集合还在，直到重启（实测过这个坑）
    _collection_ready.difference_update({key for key in _collection_ready if key[0] == name})
    _payload_indexed.discard(name)
    logger.info("已删除向量集合：{}", name)
    return True


@_serialized
def scroll_all(
    collection: str, *, page_size: int = 1000, with_vectors: bool = True
) -> Iterator[list[Any]]:
    """分页遍历集合（对账/重建用）。

    参数：
        collection: 集合名。
        page_size: 每页多少条。
        with_vectors: 要不要取回向量本体（只要 payload 时可以省掉，快很多）。
    产出：每页的 record 列表。
    """
    if not has_collection(collection):
        return
    store = client()
    offset = None
    while True:
        records, offset = store.scroll(
            collection_name=collection,
            limit=page_size,
            offset=offset,
            with_vectors=with_vectors,
            with_payload=True,
        )
        if not records:
            return
        yield records
        if offset is None:
            return


@_serialized
def scroll_filter(
    collection: str,
    where: dict[str, Any] | None = None,
    *,
    page_size: int = 1000,
    with_vectors: bool = False,
) -> list[Any]:
    """按条件遍历集合（按资料删/枚举用）。``where`` 语义与 :func:`query` 相同。"""
    if not has_collection(collection):
        return []
    models = _models()
    store = client()
    conditions = []
    for key, value in (where or {}).items():
        if isinstance(value, (list, tuple, set)):
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=list(value)))
            )
        else:
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
    query_filter = models.Filter(must=conditions) if conditions else None
    out: list[Any] = []
    offset = None
    while True:
        records, offset = store.scroll(
            collection_name=collection,
            scroll_filter=query_filter,
            limit=page_size,
            offset=offset,
            with_vectors=with_vectors,
            with_payload=True,
        )
        out.extend(records)
        if not records or offset is None:
            return out


@_serialized
def upsert_points_vectorless(collection: str, entries: list[tuple[str, dict[str, Any]]]) -> int:
    """写**无向量**点（parent / documents 集合用）：`vector={}` payload-only。

    参数：
        collection: 集合名。
        entries: ``[(key, payload), ...]``。
    返回：写了几条。
    """
    if not entries:
        return 0
    if not has_collection(collection):
        ensure_vectorless_collection(collection)
    models = _models()
    store = client()
    points = [
        models.PointStruct(
            # 把 key 写进 payload：按资料枚举/删除时靠它还原 key（否则会回退成数字 id，
            # 而 delete_keys 又对数字 id 走稳定哈希 → 删错点）。
            id=stable_point_id(key),
            vector={},
            payload={"key": key, **dict(payload)},
        )
        for key, payload in entries
    ]
    try:
        store.upsert(collection_name=collection, points=points)
    except Exception as exc:
        # 与 child 的 write_vectors 一致：包装成 VectorStoreError，避免原始 qdrant 英文串直通界面
        raise VectorStoreError(f"写入 {collection} 失败：{exc}") from exc
    return len(points)


@_serialized
def info(collection: str, *, with_bytes: bool = True) -> dict[str, Any]:
    """集合的概况（界面「集合与占用」那张卡：条数、维度、点数量、目录占用）。

    ``with_bytes=False``：跳过目录占用统计。``folder_bytes`` 要 rglob 整个 Qdrant 目录，
    一次页面刷新若对 3 个集合各算一次就是 3 遍全盘遍历——调用方（`api.stats`）算一次即可。
    """
    if not has_collection(collection):
        return {"exists": False, "name": collection, "count": 0, "dims": 0, "bytes": 0}
    details = client().get_collection(collection)
    params = details.config.params.vectors
    dims = int(getattr(params, "size", 0) or 0)
    points = int(getattr(details, "points_count", 0) or 0)
    return {
        "exists": True,
        "name": collection,
        "count": points,
        # 本地模式不建索引（HNSW 段数为 0），这条数字就是"它不做打分"的现场证据
        "indexed": int(getattr(details, "indexed_vectors_count", 0) or 0),
        "dims": dims,
        "bytes": folder_bytes(qdrant_dir()) if with_bytes else 0,
        "status": str(getattr(details, "status", "")),
    }


def folder_bytes(folder: Path) -> int:
    """目录占用（只算文件；Qdrant 的段文件不大，但报出来用户才安心）。"""
    total = 0
    try:
        for item in folder.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except OSError as exc:
        logger.debug("统计 Qdrant 目录占用失败（{}）：{}", folder, exc)
    return total


def build_payload(
    *,
    chunk_id: int,
    doc_id: int,
    version: int = 1,
    status: str = "active",
    content_type: str = "text",
    page: int | None = None,
    collection: str = "rag_chunks",
) -> dict[str, Any]:
    """组装一条 payload（过滤字段就这几个，多了会拖慢过滤）。

    对齐技术方案 §6 的约定：``chunk_id / doc_id / version / status / content_type / page``。
    """
    payload: dict[str, Any] = {
        "chunk_id": int(chunk_id),
        "doc_id": int(doc_id),
        "version": int(version),
        "status": status,
        "content_type": content_type,
        "collection": collection,
    }
    if page is not None:
        payload["page"] = int(page)
    return payload


@_serialized
def write_vectors(
    collection: str,
    dims: int,
    entries: list[tuple[str, np.ndarray, dict[str, Any]]],
) -> int:
    """把 ``(key, vector, payload)`` 写进集合，**同时**分配确定性 point id。

    参数：
        collection: 集合名。
        dims: 维度。
        entries: 每项 ``(key, 向量, payload)``；key 形如 ``"12:1:0"``（doc:version:seq）
            或任意字符串——不是数字组合时按 hash 生成稳定 id（记忆集合用它）。
    返回：写入条数。

    为什么 key 要能是任意字符串：``memory`` 集合的一条记忆没有 doc/version/seq，
    但它同样需要**幂等**（同一句话不重复入库），所以也要个稳定 id。
    """
    if not entries:
        return 0
    ids: list[int] = []
    vectors: list[np.ndarray] = []
    payloads: list[dict[str, Any]] = []
    for key, vector, payload in entries:
        ids.append(stable_point_id(key))
        vectors.append(np.asarray(vector, dtype=np.float32).reshape(-1))
        # **key 也写进 payload**：point id 是个大整数，而调用方手里的是 `"12:1:0"` 这种 key。
        # 不存它的话，从 Qdrant 查回来的结果没法映射回业务行（删除/切换/对账都会失准）。
        stored = dict(payload)
        stored["key"] = key
        payloads.append(stored)
    return upsert(collection, dims, ids, np.vstack(vectors), payloads)


def stable_point_id(key: str) -> int:
    """字符串 key → 稳定的正整数 point id。

    - ``"doc:version:seq"``（三段都是数字）→ 用 :func:`point_id` 的算法，可读、可拆；
    - 其它字符串（记忆、能力说明）→ 用 blake2b 取前 8 字节（**不用** Python 的 ``hash()``：
      它带进程随机种子，重启就变了，会导致重复入库）。
    """
    try:
        doc_id, version, seq = parse_key(key)
    except ValueError:
        pass  # 不是 doc:version:seq（记忆/能力说明那类 key）→ 走下面的稳定哈希
    else:
        return point_id(doc_id, version, seq)
    import hashlib

    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    # 最高位清零：Qdrant 的 id 必须是无符号 64 位整数。
    # **不能写 >> 1**：那会把最低位丢掉，让"只有最低位不同"的两个哈希撞成同一个 id
    # （碰撞概率翻倍；虽然仍可忽略，但没有理由这么做）。
    return int.from_bytes(digest, "big") & MAX_POINT_ID
