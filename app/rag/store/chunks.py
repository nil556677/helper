"""``chunks`` 片段的写入/查询——**Qdrant 后端**（docs/向量层重构设计.md）。

child（dense）与 parent（无向量集合）都存 Qdrant。本模块：写一组 child+parent 点、
枚举 key、翻状态、删除、live/all key 集合。嵌入在 pipeline 统一做；这里只组装 payload + 写。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from loguru import logger

from app.core.chunk.index_text import index_text
from app.core.vector import api, qdrant_store
from app.core.versioning import CHUNK_POLICY_VERSION, EMBEDDING_VERSION

#: 集合/索引是否已就绪（进程级一次性；重复 ensure 每批都触发 11 次建索引 WARNING）
_ENSURED: set[str] = set()


def _ensure_once(collection: str, *, parents: bool = False) -> None:
    if collection in _ENSURED:
        return
    if parents:
        api.ensure_parents()
    else:
        api.ensure(collection)
    _ENSURED.add(collection)


def _json_path(section_path: Sequence[str] | None) -> str:
    return json.dumps(list(section_path or []), ensure_ascii=False)


def content_hash(text: str) -> str:
    """块级内容指纹（去空白后 sha256）——embedding 缓存复用的键。"""
    normalized = re.sub(r"\s+", "", text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _loc_with_row_base(loc: dict, table_ref: object | None) -> dict:
    """在定位信息里标注表格行号的**口径**，否则跨格式无法统一解释 ``row_from/row_to``。

    - ``sheet:…``（xlsx/xlsm）→ ``sheet-absolute``（sheet 内的绝对行号）；
    - 其它表格（csv/docx/pptx/md/pdf）→ ``table``（从 1 数的表内行号）。
    """
    if not isinstance(loc, dict):
        return loc
    if isinstance(table_ref, str) and table_ref.startswith("sheet:"):
        loc["row_base"] = "sheet-absolute"
    elif loc.get("kind") == "table":
        loc["row_base"] = "table"
    return loc


def page_no(record: object) -> int | None:
    p = getattr(record, "page", None)
    return int(p) if p is not None else None


def child_point_payload(
    *,
    documents_id: int,
    version: int,
    seq: int,
    text: str,
    content_type: str,
    page: int | None,
    section_path: Sequence[str] | None,
    level: int | None,
    parent_key: str | None,
    status: str,
    heading: str = "",
    content_hash: str = "",
    policy_version: int = 0,
    embedding_version: int = 0,
) -> dict:
    return {
        "key": f"{documents_id}:{version}:{seq}",
        "documents_id": int(documents_id),
        "version": int(version),
        "seq": int(seq),
        "status": status,
        "content_type": content_type,
        "text": text,
        "section_path": _json_path(section_path),
        # 标题**单独一个字段**（与正文分开存）：完整标题链，如 "2. 相关工作 › 2.1 …"
        **({"heading": heading} if heading else {}),
        **({"page": int(page)} if page is not None else {}),
        **({"level": int(level)} if level is not None else {}),
        **({"parent_key": str(parent_key)} if parent_key else {}),
        # 内容指纹 + 版本：块级 embedding 缓存靠它（同文本同模型 → 复用向量，不重嵌）
        **({"content_hash": content_hash} if content_hash else {}),
        **({"policy_version": int(policy_version)} if policy_version else {}),
        **({"embedding_version": int(embedding_version)} if embedding_version else {}),
    }


def parent_point_payload(**kwargs: object) -> dict:
    """parent 点 payload（无向量集合；父块按需含标题，不再额外 prepend）。"""
    return child_point_payload(**kwargs)  # type: ignore[arg-type]


def write_tree(
    doc_id: int,
    groups: Sequence[object],
    *,
    start_seq: int = 0,
    version: int = 1,
    status: str = "active",
    embed: object | None = None,
    prepend_section: bool | None = None,
    doc_title: str = "",
) -> tuple[int, int]:
    """写一组（child+parent）到 Qdrant，返回 ``(子块数, 下一个可用 seq)``。

    三个阶段（"嵌入 + 写"两步都在这里）：

    1. **组装 payload**：每个 child 记 ``key``（``doc:ver:seq``）、正文、章节路径、页码、
       ``parent_key`` 与 ``status``；顺带做规则提取（编号/日期/金额进 ``attrs*``，失败只丢属性，
       不挡入库）。
    2. **嵌入**：把本批所有 child 文本**一次性批量**交给嵌入模型，得到 dense 向量
       （不是逐条调用——批量才有吞吐）。
    3. **写 Qdrant**：child 连同向量写进向量集合；parent **只写文本**、进无向量集合。
       parent 的 key 复用该组首个 child 的 ``seq``——两个集合各自编号，同号不冲突。

    ``embed``：child 文本的嵌入函数（缺省真实模型；测试注入）。
    """
    if embed is None:
        from app.core.embed import embed_texts as _real

        embed = _real
    if prepend_section is None:
        from app.core.config import store as params_store

        prepend_section = bool(params_store.load()["chunk.prepend_section"])

    _ensure_once(api.COLLECTION_CHUNKS)
    _ensure_once(api.COLLECTION_PARENTS, parents=True)

    # ---- 阶段 1：组装 payload（child 记录 + parent 记录；这一步不碰网络/模型）----
    child_entries: list[dict] = []
    parent_entries: list[tuple[str, dict]] = []
    seq = start_seq
    child_count = 0
    # 标题路径**从 H1 起**：文档大标题作为路径第一段（无标题文档就是空）
    title_prefix = [str(doc_title).strip()] if str(doc_title).strip() else []
    # 父块**按内容去重**：同一篇里内容完全相同的父块只写一份，多个子块引用同一个 key。
    # 跨批次也要去重——先查同一 doc+version 已写入的父块（本次调用内用 seen_parents）。
    seen_parents: dict[str, str] = {}
    pending_hashes = [
        content_hash(str(getattr(group.parent, "text", "") or ""))
        for group in groups
        if getattr(group, "parent", None) is not None
    ]
    existing_parents = api.parent_keys_by_content_hash(
        doc_id, version, [digest for digest in pending_hashes if digest]
    )
    for group in groups:
        parent = group.parent  # type: ignore[attr-defined]
        children = group.children  # type: ignore[attr-defined]
        parent_key: str | None = None
        if parent is not None:
            parent_text = str(getattr(parent, "text", "") or "")
            parent_hash = content_hash(parent_text)
            # 内容完全相同的父块只建一份：同一次调用内用 seen，跨批次用 existing
            parent_key = seen_parents.get(parent_hash) or existing_parents.get(parent_hash)
            parent_path = title_prefix + [
                str(item).strip()
                for item in (getattr(parent, "section_path", None) or ())
                if str(item).strip()
            ]
            if parent_key is None:
                # **子块 seq 连续**（0..n-1）：父块不占子块序号；key 复用该组首个 child 的 seq。
                parent_key = f"{doc_id}:{version}:{seq}"
                parent_payload = parent_point_payload(
                    documents_id=doc_id,
                    version=version,
                    seq=seq,
                    text=parent_text,
                    content_type=getattr(parent, "content_type", "text") or "text",
                    page=page_no(parent),
                    section_path=parent_path,
                    level=getattr(parent, "level", None),
                    parent_key=None,
                    status=status,
                    # 父块正文不放标题，只把完整标题链存成元数据
                    heading=" › ".join(parent_path),
                    content_hash=parent_hash,
                )
                # 表格/代码 parent 的表身份与行区间也要进 payload
                parent_payload["loc"] = _loc_with_row_base(
                    parent.loc_json(), getattr(parent, "table_ref", None)
                )
                parent_entries.append((parent_key, parent_payload))
                seen_parents[parent_hash] = parent_key
            # 已存在（内容重复）→ 不重复建，子块直接引用旧 key
        for child in children:
            child_text = str(getattr(child, "text", "") or "")
            child_path = title_prefix + [
                str(item).strip()
                for item in (getattr(child, "section_path", None) or ())
                if str(item).strip()
            ]
            # **嵌入/精排用的是"完整标题路径 + 原文"**：标题词（含 H1）进向量，
            # 这样"搜文档标题/章节名"能召回正文。payload.text 仍是纯原文。
            heading = " › ".join(child_path)
            embed_text = index_text(child_text, child_path, prepend=bool(prepend_section))
            payload = child_point_payload(
                documents_id=doc_id,
                version=version,
                seq=seq,
                text=child_text,
                content_type=getattr(child, "content_type", "text") or "text",
                page=page_no(child),
                section_path=child_path,
                level=getattr(child, "level", None),
                parent_key=parent_key,
                status=status,
                heading=heading,
                # 指纹按**嵌入文本**算：同正文不同章节 → 向量不同，不能复用
                content_hash=content_hash(embed_text),
                policy_version=CHUNK_POLICY_VERSION,
                embedding_version=EMBEDDING_VERSION,
            )
            # 定位锚点（页/段/行/表/坐标）：docx 没有页码，靠 para_index 回原文
            payload["loc"] = _loc_with_row_base(
                child.loc_json(), getattr(child, "table_ref", None)
            )
            # 动态字段**不在这里提取**：统一由 `store_records` 在真正写入前补
            # （见 docs/动态字段与存储边界改造方案.md §4.4）。
            child_entries.append(
                {"key": f"{doc_id}:{version}:{seq}", "payload": payload, "embed_text": embed_text}
            )
            seq += 1
            child_count += 1

    if child_entries:
        import numpy as np

        # ---- 阶段 2：嵌入——**先按 content_hash 找可复用的旧向量**，只嵌"没命中"的 ----
        # 为什么值得：改标题/换版本时正文大多没变；全量重嵌是最贵的一步。
        hashes = [str(entry["payload"].get("content_hash") or "") for entry in child_entries]
        reused = api.vectors_by_content_hash(
            api.COLLECTION_CHUNKS,
            [digest for digest in hashes if digest],
            embedding_version=EMBEDDING_VERSION,
        )
        rows: list[object] = [None] * len(child_entries)
        missing: list[int] = []
        for index, digest in enumerate(hashes):
            vector = reused.get(digest) if digest else None
            if vector is None:
                missing.append(index)
            else:
                rows[index] = vector
        if missing:
            texts = [str(child_entries[index]["embed_text"]) for index in missing]
            fresh = [np.asarray(value, dtype=np.float32).reshape(-1) for value in embed(texts)]
            for index, vector in zip(missing, fresh, strict=True):
                rows[index] = vector
        # ---- 阶段 3：写 Qdrant——**统一写入器**先补动态字段，再连同向量写入 ----
        from app.core.vector.writer import StoredRecord, store_records

        store_records(
            api.COLLECTION_CHUNKS,
            [
                StoredRecord(
                    key=entry["key"],
                    vector=rows[index],
                    payload=entry["payload"],
                    text=str(entry["payload"].get("text") or ""),
                )
                for index, entry in enumerate(child_entries)
            ],
        )
    if parent_entries:
        # ---- 阶段 3：写 Qdrant——parent 只存文本（无向量）；检索命中 child 后按 parent_key 取它 ----
        qdrant_store.upsert_points_vectorless(api.COLLECTION_PARENTS, parent_entries)
    # 返回 (子块数, 下一个可用 seq)：子块数与 seq 分开，避免把父块算进"切出几个片段"
    # 又让下一批沿用正确的 seq（父块也占序位，key 才不撞）。
    return child_count, seq


def delete_chunks(doc_id: int) -> None:
    """按资料删 Qdrant 点（child + parent，所有版本）。

    用**服务端 payload 过滤**删（``documents_id``），免去"先 scroll 预扫描再逐点删"
    的两步——点数再多也是一次请求；老数据没有 ``payload.key`` 也不影响（过滤不看 key）。
    """
    for collection in (api.COLLECTION_CHUNKS, api.COLLECTION_PARENTS):
        api.delete_where(collection, {"documents_id": int(doc_id)})


def replace_tree(
    doc_id: int, groups: Sequence[object], *, version: int = 1
) -> tuple[int, int]:
    delete_chunks(doc_id)
    return write_tree(doc_id, groups, version=version)


def child_rows(doc_id: int, version: int) -> list[object]:
    return _rows_by_document(api.COLLECTION_CHUNKS, int(doc_id), version=int(version))


def child_keys(doc_id: int, version: int) -> list[str]:
    return _keys_by_document(api.COLLECTION_CHUNKS, int(doc_id), version=int(version))


def parent_keys(doc_id: int, version: int) -> list[str]:
    return _keys_by_document(api.COLLECTION_PARENTS, int(doc_id), version=int(version))


def _keys_by_document(
    collection: str, doc_id: int, *, version: int | None = None
) -> list[str]:
    out: list[str] = []
    rows = _scroll_filter_list(collection, {"documents_id": int(doc_id)})
    for r in rows:
        pl = dict(getattr(r, "payload", None) or {})
        if version is not None and int(pl.get("version", -1)) != int(version):
            continue
        key = pl.get("key")
        if not key:
            # **老数据没有 payload.key**：用三段字段还原（都是我们自己写的）。
            # 绝不能回退成数字 point id——`delete_keys` 会把它当字符串 key 再哈希一次，
            # 算出一个完全不同的点，结果是删错或删不掉（qdrant_store 里也注明过这个坑）。
            try:
                key = f"{int(pl['documents_id'])}:{int(pl['version'])}:{int(pl['seq'])}"
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "跳过没有 key 的向量点（{}，id={}）", collection, getattr(r, "id", "?")
                )
                continue
        out.append(str(key))
    return out


def _rows_by_document(collection: str, doc_id: int, *, version: int | None = None) -> list[object]:
    rows = _scroll_filter_list(collection, {"documents_id": int(doc_id)})
    return [
        r
        for r in rows
        if version is None
        or int((dict(getattr(r, "payload", None) or {})).get("version", -1)) == int(version)
    ]


def _scroll_filter_list(collection: str, where: dict) -> list[object]:
    return qdrant_store.scroll_filter(collection, where, with_vectors=False)


def _apply_document_status(doc_id: int, status: str) -> None:
    for collection in (api.COLLECTION_CHUNKS, api.COLLECTION_PARENTS):
        keys = _keys_by_document(collection, int(doc_id))
        if keys:
            api.set_payload(collection, keys, {"status": status})


def promote_chunks(_conn: object, doc_id: int) -> None:
    _apply_document_status(int(doc_id), "active")


def live_child_keys(*, active_only: bool = True) -> set[str]:
    from app.rag.store.documents import _active_doc_ids

    wanted = _active_doc_ids() if active_only else None
    out: set[str] = set()
    rows = _scroll_filter_list(api.COLLECTION_CHUNKS, {})
    for r in rows:
        pl = dict(getattr(r, "payload", None) or {})
        dig = int(pl.get("documents_id", -1))
        if wanted is not None and dig not in wanted:
            continue
        if active_only and pl.get("status") == "superseded":
            continue
        out.add(str(pl.get("key") or getattr(r, "id", "")))
    return out


def all_child_keys() -> set[str]:
    out: set[str] = set()
    for r in _scroll_filter_list(api.COLLECTION_CHUNKS, {}):
        pl = dict(getattr(r, "payload", None) or {})
        out.add(str(pl.get("key") or getattr(r, "id", "")))
    return out


def all_parent_keys() -> set[str]:
    out: set[str] = set()
    for r in _scroll_filter_list(api.COLLECTION_PARENTS, {}):
        pl = dict(getattr(r, "payload", None) or {})
        out.add(str(pl.get("key") or getattr(r, "id", "")))
    return out


def rebuild_tokens(_conn: object | None = None, *, _batch_size: int = 2000) -> int:
    """**只重算词法**：v12 后词法在 Qdrant sparse（阶段 3/P5 落地）。

    现阶段返回 0（无 SQLite tokens 可重算），调用方把它当"没有需要重算的"。
    """
    return 0
