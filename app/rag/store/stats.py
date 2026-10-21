"""统计 / 覆盖 / 列表：向量覆盖率与字节、资料计数、分页列表。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from app.core.db.connection import get_connection
from app.core.vector import api as vector_api

#: 统计类查询的短 TTL 缓存：总览**每秒**刷新，而覆盖率/字节/进度都是 O(N) 的
#: Qdrant/文件系统查询。缓存 5–60 秒能把"每秒全量扫一遍"降成"偶尔扫一次"，
#: 长任务运行时界面不再被这些查询拖死（数值最多旧几秒，对概览足够）。
_CACHE: dict[tuple[object, ...], tuple[int, float, object]] = {}


def _cache_epoch() -> int:
    """跨进程缓存代：数据库/WAL 的 mtime。

    worker 进程导入/重建/删除后写 SQLite，UI 进程这里的代会变 → 下一次读不再用旧缓存。
    比"只靠 TTL"更及时，又不需要专门写一个版本表（避免自检/测试碰库）。
    """
    try:
        from app.core import paths

        base = paths.db_file()
        stamp = base.stat().st_mtime_ns
        wal = base.with_name(base.name + "-wal")
        if wal.exists():
            stamp = max(stamp, wal.stat().st_mtime_ns)
        return int(stamp)
    except OSError:
        return 0


def _cached(key: tuple[object, ...], ttl_s: float, compute: Callable[[], object]) -> object:
    """取缓存；**代没变**且没过期才命中，否则执行 ``compute`` 并回填。"""
    now = time.monotonic()
    epoch = _cache_epoch()
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == epoch and now - hit[1] < ttl_s:
        return hit[2]
    value = compute()
    _CACHE[key] = (epoch, now, value)
    return value


def clear_caches() -> None:
    """清空统计缓存（导入/重建/对账完成后调用，保证下一次读到新值）。"""
    _CACHE.clear()


#: 列表/分页共用的查询（分页与"全都要"共用一段 SELECT，避免两处漂移）。
#: 片段数不再存 SQLite（存在 Qdrant）；列表的 chunks 列先给 0，界面按需再查 Qdrant。
_LIST_SELECT = """
        SELECT d.id AS id, d.source_name AS name, d.size AS size, d.status AS status,
               d.detail AS detail, d.updated_at AS updated_at,
               d.pages AS pages, d.ocr_pages AS ocr_pages, d.chars AS chars,
               0 AS chunks
        FROM documents d
"""


#: 排序白名单：``键 → 列表达式``。**列名绝不拼接用户输入**——只查这张表。
_SORT_COLUMNS = {
    "name": "d.source_name COLLATE NOCASE",
    "size": "d.size",
    "chunks": "d.id",
    "status": "d.status",
    "updated_at": "d.updated_at",
}

#: 状态筛选白名单（界面上的四个状态 + "全部"）
STATUS_FILTERS = ("pending", "indexing", "ready", "failed")


def _name_filter(
    keyword: str, *, exclude_superseded: bool = False, status: str | None = None
) -> tuple[str, tuple[object, ...]]:
    """名称筛选的 WHERE 片段；``exclude_superseded`` 时把退场版本挡在外面。

    为什么要挡：版本切换后旧版本行还留着（可回滚、可追查），但它**不是**用户眼里的一份资料——
    列表里出现两条同名资料会让用户以为导重了。
    """
    clauses: list[str] = []
    params: list[object] = []
    cleaned = keyword.strip()
    if cleaned:
        clauses.append("d.source_name LIKE ?")
        params.append(f"%{cleaned}%")
    if exclude_superseded:
        clauses.append("d.status != 'superseded'")
    if status:
        clauses.append("d.status = ?")
        params.append(status)
    if not clauses:
        return "", ()
    return "WHERE " + " AND ".join(clauses), tuple(params)


def counts() -> object:
    """概览那一行计数（可用份数 / 待建向量份数 / 失败份数 / 片段数 / 占用字节 / 最后更新）。

    **"在册"包含 pending 与 indexing**：排队中就落了占位行（P1.16）、入库后词法立刻可搜
    （P1.6 把导入与向量化拆开）——把它们排除在外会让用户刚导入就看到"0 份资料"。
    ``indexing`` 这个计数是**处理中/待处理**（界面上"处理中 N 份"用它）。
    """
    live = "('ready', 'indexing', 'pending')"
    row = get_connection().execute(
        f"""
        SELECT
          (SELECT count(*) FROM documents WHERE status IN {live}) AS documents,
          (SELECT count(*) FROM documents WHERE status IN ('pending', 'indexing')) AS indexing,
          (SELECT count(*) FROM documents WHERE status = 'failed') AS failed,
          (SELECT coalesce(sum(size), 0) FROM documents WHERE status IN {live}) AS bytes,
          (SELECT max(updated_at) FROM documents WHERE status IN {live}) AS updated_at
        """
    ).fetchone()
    # 片段数是 Qdrant 里的全局计数（SQLite 不再存片段）；Qdrant 不可用时给 0，概览不炸。
    from app.core.vector import api as vector_api

    out = dict(row)
    try:
        out["chunks"] = vector_api.count(vector_api.COLLECTION_CHUNKS)
    except Exception:
        out["chunks"] = 0
    return out


def list_all() -> list[object]:
    """全部**在册**资料（新的在前；不含退场版本）。

    列表页走 :func:`page` 分页，这个给测试、全量重建与统计用。
    "退场版本"（``superseded``）只在版本链里留着，不是用户眼里的一份资料。
    """
    where, params = _name_filter("", exclude_superseded=True)
    return list(
        get_connection().execute(
            f"{_LIST_SELECT} {where} ORDER BY d.updated_at DESC, d.id DESC", params
        ).fetchall()
    )


def count(keyword: str = "", *, status: str | None = None) -> int:
    """满足名称/状态筛选的资料条数（分页要它算总页数；**不含退场版本**）。"""
    where, params = _name_filter(
        keyword, exclude_superseded=True, status=status if status in STATUS_FILTERS else None
    )
    row = get_connection().execute(
        f"SELECT count(*) AS n FROM documents d {where}", params
    ).fetchone()
    return int(row["n"])


def page(
    keyword: str = "",
    *,
    size: int,
    offset: int,
    status: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
) -> list[object]:
    """一页资料。``size`` / ``offset`` 由调用方按页码算好。

    参数：
        status: 只看某个状态（``None`` = 全部；白名单见 :data:`STATUS_FILTERS`）。
        sort: 排序键（``name/size/chunks/status/updated_at``；非法值回默认）。
        order: ``asc`` / ``desc``（非法值回 ``desc``）。

    **排序与筛选都在服务端**：资料可能几千份，"全查出来再排"既慢又占内存；
    列名只从白名单里查，**绝不把用户输入拼进 SQL**。
    """
    where, params = _name_filter(
        keyword, exclude_superseded=True, status=status if status in STATUS_FILTERS else None
    )
    column = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["updated_at"])
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    # 次级排序固定按 id：同值行的顺序必须稳定，否则翻页会"跳行"
    return list(
        get_connection().execute(
            f"{_LIST_SELECT} {where} ORDER BY {column} {direction}, d.id DESC LIMIT ? OFFSET ?",
            (*params, int(size), int(offset)),
        ).fetchall()
    )


def vector_coverage() -> dict[str, int]:
    """**向量覆盖率**：``已向量化 / 应向量化`` 的子块数。

    分母 = 在册资料（ready/indexing）的 child 点数；分子同（单遍 ingest 下建完即就绪）。
    一次 Qdrant scroll 取回 key 集合并按 documents_id 归属，再与 SQLite 在册集合求交。

    带 10 秒缓存：总览每秒刷新，而这个查询是 O(全部片段) 的。
    """

    def _compute() -> dict[str, int]:
        from app.core.vector import qdrant_store
        from app.rag.store.documents import _active_doc_ids

        active = _active_doc_ids()
        total = 0
        done = 0
        for page in qdrant_store.scroll_all(vector_api.COLLECTION_CHUNKS, with_vectors=False):
            for record in page:
                payload = dict(getattr(record, "payload", None) or {})
                dig = int(payload.get("documents_id", payload.get("doc_id", -1)))
                if dig not in active:
                    continue
                total += 1
                if payload.get("status") != "superseded":
                    done += 1
        if total == 0:
            return {"done": 0, "total": 0, "ratio": 100}
        return {"done": done, "total": total, "ratio": round(done * 100 / total)}

    return _cached(("vector_coverage",), 10.0, _compute)  # type: ignore[return-value]


def vector_bytes() -> int:
    """向量占用字节（Qdrant 本地目录总量；服务端模式没有本地目录，返回 0）。

    **不要再对各集合的 bytes 求和**：`api.stats()` 里每个集合报的都是"同一个 Qdrant
    目录的总占用"（目录是共享的），求和会把总量放大成 N 倍。
    目录遍历很贵（尤其写入期间），缓存 60 秒。
    """
    from app.core.vector import qdrant_store

    def _compute() -> int:
        return qdrant_store.folder_bytes(qdrant_store.qdrant_dir())

    return _cached(("vector_bytes",), 60.0, _compute)  # type: ignore[return-value]


def stored_vector_keys() -> set[str]:
    """向量存储里现存的 key 集合（**读 Qdrant**，一次性 scroll）。

    配合覆盖率/对账：需要 key 集合就必须全量取一次；本地模式快，服务端稍慢，
    只在"点对账/看覆盖率"这种低频动作时调用，不做热路径。
    """
    from app.core.vector import api as vector_api

    return set(vector_api.keys(vector_api.COLLECTION_CHUNKS))


def stored_parent_keys() -> set[str]:
    """parents（无向量集合）里现存的 key 集合（对账用）。"""
    from app.core.vector import api as vector_api

    return set(vector_api.keys(vector_api.COLLECTION_PARENTS))


def vector_progress_by_doc(doc_ids: Sequence[int]) -> dict[int, tuple[int, int]]:
    """按资料算**向量化进度**（P1.11 行内"处理中 3/12"）。

    分母 = 该资料在 Qdrant 的点数；分子同（单遍 ingest 下"建完即就绪"）。
    只取关心的资料（``scroll_filter(documents_id in ids)``），不整集扫。

    **失败降级**：向量库被写锁占住/连接异常时返回 0，而不是把异常抛到页面构建
    （实测：长任务写入时这个查询一抛，整页就是 500）。缓存 3 秒，避免刷新时重复扫。
    """
    ids = [int(doc_id) for doc_id in doc_ids]
    if not ids:
        return {}

    def _compute() -> dict[int, tuple[int, int]]:
        from app.core.vector import qdrant_store

        counts: dict[int, int] = {}
        try:
            rows = qdrant_store.scroll_filter(
                vector_api.COLLECTION_CHUNKS, {"documents_id": ids}, with_vectors=False
            )
        except Exception as exc:  # 向量库不可用/并发异常：进度显示 0，不拖垮页面
            from loguru import logger

            logger.warning("读取向量进度失败（按 0 处理）：{}", exc)
            return dict.fromkeys(ids, (0, 0))
        for record in rows:
            payload = dict(getattr(record, "payload", None) or {})
            doc_id = payload.get("documents_id", payload.get("doc_id"))
            if doc_id is None:
                continue
            key_id = int(doc_id)
            counts[key_id] = counts.get(key_id, 0) + 1
        return {doc_id: (counts.get(doc_id, 0), counts.get(doc_id, 0)) for doc_id in ids}

    return _cached(("vector_progress", *ids), 3.0, _compute)  # type: ignore[return-value]
