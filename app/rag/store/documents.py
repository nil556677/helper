"""``documents`` 资料行的写与读（插入、状态翻、判重、版本链查询、列表删除）。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from pathlib import Path

from app.core.db.connection import get_connection, transaction

from ._shared import _KEEP


def find_id_by(
    column: str,
    value: str,
    *,
    exclude_id: int | None = None,
    statuses: Sequence[str] | None = None,
    conn: object | None = None,
) -> int | None:
    """按某一列判重（``file_hash`` / ``text_hash``），返回最早那条的 id。

    **列名由调用方给**（两个固定的判重列），值一律走占位符——不存在拼接注入。
    ``exclude_id``：排除某一行（流式导入时"别把自己判成重复"——见 :func:`insert_ready`
    里用 NULL 占位的原因）。
    ``statuses``：只看这些状态的行。上传入口用它**排除 failed 占位行**——
    占位行也带 ``file_hash``，不排除的话「同一文件还在排队时再传一次」会被误判成
    "已经在资料库里"，而第一次若随后失败，本次上传就永久丢了。
    ``conn``：传了就**复用调用方的事务**（上传登记要把"查重 + 插占位"放进同一写事务，
    否则两个并发上传能在无锁窗口里各插一条——TOCTOU）。
    """
    runner = conn if conn is not None else get_connection()
    where = [f"{column} = ?"]
    params: list[object] = [value]
    if exclude_id is not None:
        where.append("id != ?")
        params.append(exclude_id)
    if statuses:
        where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    row = runner.execute(  # type: ignore[attr-defined]
        f"SELECT id FROM documents WHERE {' AND '.join(where)} ORDER BY id LIMIT 1",
        tuple(params),
    ).fetchone()
    return int(row["id"]) if row else None


def refresh_file_hash(doc_id: int, file_hash: str, *, now: float | None = None) -> None:
    """正文判重命中时把新文件的字节哈希补上（下次这份文件走最快的路径）。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        conn.execute(
            "UPDATE documents SET file_hash = ?, updated_at = ? WHERE id = ?",
            (file_hash, stamp, doc_id),
        )


def record_failure(name: str, size: int, reason: str, *, now: float | None = None) -> int:
    """把失败也记进资料表（用户在列表里能看到它、也能删掉它）。返回新的 doc id。"""
    stamp = time.time() if now is None else now
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO documents(source_name, stored_path, size, status, detail, created_at,"
            " updated_at) VALUES (?, '', ?, 'failed', ?, ?, ?)",
            (name, size, reason, stamp, stamp),
        )
        return int(cursor.lastrowid or 0)


def insert_ready(
    conn: object,
    *,
    source_name: str,
    stored_path: str,
    file_hash: str,
    text_hash: str | None,
    size: int,
    detail: str | None,
    pages: int | None = None,
    ocr_pages: Sequence[int] | None = None,
    chars: int | None = None,
    doc_key: str | None = None,
    version: int = 1,
    status: str = "ready",
    now: float | None = None,
) -> int:
    """插一条资料（**不自己开事务**：调用方接着还要写片段）。

    ``text_hash`` 可以是 ``None``——流式导入**读完全文才知道正文指纹**，所以先留 NULL，
    结束时由 :func:`finish_document` 补上。

    **为什么不能拿 ``file_hash`` 当占位值**（踩过的坑）：对于"不含任何空白"的文本文件
    （内容就是 `甲`、`#合同` 这种单行），文件字节与去空白后的正文 UTF-8 字节**完全相同**，
    于是 `file_hash == text_hash` → **判重会命中自己** → 整份资料被当成"重复"回滚掉、
    用户看到"已在库里了"却什么都没导入。NULL 永远不会等于一个真哈希，所以用它占位。

    ``pages`` / ``ocr_pages`` / ``chars`` 是解析摘要（v5 建的列）：界面要显示
    "共 N 页、其中 M 页待 OCR"、以及"这份资料有多少字"。

    ``doc_key`` / ``version`` / ``status`` 是版本链（P1.7）：

    - ``doc_key``：同一份资料的稳定标识（规范化文件名）；
    - ``version``：第几版；
    - ``status``：导入阶段写 ``indexing``（词法已可用、向量还没建完），向量化完成后才 ``ready``
      —— 技术方案 §9.7 的"先写块行（indexing）→ 分批写向量 → 全部完成才 ready"。

    返回：新的 doc id。
    """
    stamp = time.time() if now is None else now
    cursor = conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO documents(source_name, stored_path, file_hash, text_hash, size, status,"
        " detail, pages, ocr_pages, chars, doc_key, version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_name,
            stored_path,
            file_hash,
            text_hash,
            size,
            status,
            detail,
            pages,
            json.dumps(list(ocr_pages), ensure_ascii=False) if ocr_pages else None,
            chars,
            doc_key,
            version,
            stamp,
            stamp,
        ),
    )
    return int(cursor.lastrowid or 0)


def finish_document(
    conn: object,
    doc_id: int,
    *,
    text_hash: str,
    pages: int | None = None,
    ocr_pages: Sequence[int] | None = None,
    chars: int | None = None,
    detail: str | None = None,
    doc_title: str | None = None,
    now: float | None = None,
) -> int:
    """流式导入结束时**补齐**资料行（**不自己开事务**：还在导入的那个事务里）。

    返回：实际更新的行数。**0 表示资料行已被删除**——调用方必须放弃揭示，
    不能继续 promote/payload 切换（否则会留下 active 孤儿点）。

    为什么分两步：正文哈希要读完整个文档才算得出来，而流式导入是**单遍**的
    （不为了算哈希把 PDF 解析两遍——pdfplumber 那一步很贵）。所以插入时先放占位值，
    结束时在这里改成真值；同一事务内完成，库外看不到中间态。
    """
    return write_summary(
        conn,
        doc_id,
        text_hash=text_hash,
        pages=pages,
        ocr_pages=ocr_pages,
        chars=chars,
        detail=detail,
        doc_title=doc_title,
        now=now,
    )


def write_summary(
    conn: object,
    doc_id: int,
    *,
    text_hash: str | object = _KEEP,
    pages: int | object | None = _KEEP,
    ocr_pages: Sequence[int] | object | None = _KEEP,
    chars: int | object | None = _KEEP,
    detail: str | object | None = _KEEP,
    doc_title: str | object | None = _KEEP,
    now: float | None = None,
) -> int:
    """**写文档摘要列**（唯一入口）：``text_hash`` / ``pages`` / ``ocr_pages`` / ``chars`` / ``detail``。

    为什么要统一：这几个列以前散在 ``finish_document`` 与 ``set_parse_stats`` 两个函数里，
    字段顺序和``COALESCE`` 口径各写一遍，"漏写一个列"是迟早的事。
    现在：**传了才改**（``_KEEP`` 表示不动），``None`` 表示清空。

    ``detail`` 有两种语义，写库时靠调用方区分：失败原因（``mark_failed``）与进度提示
    （``mark_indexing``）——收尾时一律清空。
    """
    stamp = time.time() if now is None else now
    assignments = ["updated_at = ?"]
    values: list[object] = [stamp]
    if text_hash is not _KEEP:
        assignments.append("text_hash = ?")
        values.append(text_hash)
    if pages is not _KEEP:
        assignments.append("pages = ?")
        values.append(pages)
    if ocr_pages is not _KEEP:
        assignments.append("ocr_pages = ?")
        values.append(
            json.dumps(list(ocr_pages), ensure_ascii=False) if ocr_pages else None  # type: ignore[arg-type]
        )
    if chars is not _KEEP:
        assignments.append("chars = ?")
        values.append(chars)
    if detail is not _KEEP:
        assignments.append("detail = ?")
        values.append(detail)
    if doc_title is not _KEEP:
        assignments.append("doc_title = ?")
        values.append(doc_title)
    values.append(int(doc_id))
    cursor = conn.execute(  # type: ignore[attr-defined]
        f"UPDATE documents SET {', '.join(assignments)} WHERE id = ?", tuple(values)
    )
    return int(cursor.rowcount)


def set_status(
    conn: object | None,
    doc_id: int,
    status: str,
    *,
    detail: str | object | None = _KEEP,
    text_hash: str | object = _KEEP,
    now: float | None = None,
) -> None:
    """**翻资料状态**（唯一入口）：``conn=None`` 时自己开事务。

    为什么必须统一：以前有五个各写一遍的函数，其中三个自己开事务、两个要传 ``conn``，
    调用方记错一次就是 ``cannot start a transaction within a transaction``（本轮实测踩过）。
    现在"要不要用外层事务"只看一个参数。

    ``superseded_at`` 只在 ``status='superseded'`` 时一起写（版本链要它）。
    """
    stamp = time.time() if now is None else now
    assignments = ["status = ?", "updated_at = ?"]
    values: list[object] = [status, stamp]
    if status == "superseded":
        assignments.append("superseded_at = ?")
        values.append(stamp)
    if detail is not _KEEP:
        assignments.append("detail = ?")
        values.append(detail)
    if text_hash is not _KEEP:
        assignments.append("text_hash = ?")
        values.append(text_hash)
    values.append(int(doc_id))
    sql = f"UPDATE documents SET {', '.join(assignments)} WHERE id = ?"
    if conn is None:
        with transaction() as own:
            own.execute(sql, tuple(values))
    else:
        conn.execute(sql, tuple(values))  # type: ignore[attr-defined]


def mark_failed(doc_id: int, detail: str, *, now: float | None = None) -> None:
    """把一份资料标成失败（原因写进 ``detail``，界面上能看到）。"""
    set_status(None, doc_id, "failed", detail=detail, now=now)


def mark_indexing(doc_id: int, detail: str | None = None, *, now: float | None = None) -> None:
    """标成"正在建索引"（词法已可用、向量还没建完）；``detail=None`` 时保留原说明。"""
    set_status(None, doc_id, "indexing", detail=detail if detail is not None else _KEEP, now=now)


def mark_ready(
    conn: object | None,
    doc_id: int,
    *,
    text_hash: str | object = _KEEP,
    detail: str | None = _KEEP,  # type: ignore[assignment]
    now: float | None = None,
) -> None:
    """把一份资料标成就绪；``conn`` 给了就和调用方同事务（重建路径），否则自己开。"""
    set_status(conn, doc_id, "ready", detail=detail, text_hash=text_hash, now=now)


def mark_ready_simple(doc_id: int, *, now: float | None = None) -> None:
    """把资料标成就绪并**清空 detail**（向量化收尾用；不动 ``text_hash``）。"""
    set_status(None, doc_id, "ready", detail=None, now=now)


def doc_key_for(name: str) -> str:
    """资料的**稳定标识**：规范化后的文件名（"同一份资料的新版本"靠它串起来）。

    为什么用文件名：我们不知道"用户心里的同一份资料"到底是什么，文件名是最稳的线索
    （同一份合同改了几处还是那个名字）。内容相同但换了名字的，由 ``text_hash`` 判重兜住；
    名字相同但内容不同的，就当成新版本（P1.7）。
    """
    base = Path(name).name.strip()
    base = re.sub(r"\s+", " ", base)
    return base.lower()


def insert_pending(
    source_name: str,
    *,
    size: int,
    file_hash: str,
    now: float | None = None,
    conn: object | None = None,
) -> int:
    """**入队时先落一行占位**（P1.16）：状态 ``pending``、没有副本、没有片段。

    为什么需要它：``documents`` 行以前是导入事务里才插入的 → **排队中/正在解析的文件
    在知识库列表里根本不存在**（用户只看到"任务在跑"，看不到"哪一份资料在处理"）。
    入队时就落一行，列表立刻能显示"待处理 → 处理中 → 就绪/失败"。

    占位行的约定：
    - ``status='pending'``、``stored_path=''``、``text_hash=NULL``（**不参与正文判重**）；
    - ``doc_key``/``version`` 在事务内算好（供导入时认领、供版本链对齐）；
    - 导入任务**认领**它（同一个 id 继续写片段），跳过/取消则删掉它。

    ``conn``：传了就复用调用方事务（上传登记把"查重 + 插占位"放同一写事务，见
    :func:`find_id_by` 的 TOCTOU 说明）。
    """
    if conn is not None:
        return _insert_pending_with(conn, source_name, size=size, file_hash=file_hash,
                                    now=time.time() if now is None else now)
    with transaction() as own:
        return _insert_pending_with(own, source_name, size=size, file_hash=file_hash,
                                    now=time.time() if now is None else now)


def _insert_pending_with(
    conn: object, source_name: str, *, size: int, file_hash: str, now: float
) -> int:
    """``insert_pending`` 的事务内实现（调用方已持写锁）。"""
    key = doc_key_for(source_name)
    current = current_row_by_key(key, conn=conn)
    version = next_version(key, conn=conn) if current is not None else 1
    cursor = conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO documents(source_name, stored_path, file_hash, text_hash, size, status,"
        " detail, doc_key, version, created_at, updated_at)"
        " VALUES (?, '', ?, NULL, ?, 'pending', '排队中', ?, ?, ?, ?)",
        (Path(source_name).name, file_hash, int(size), key, version, now, now),
    )
    return int(cursor.lastrowid or 0)


def current_row_by_key(key: str, *, conn: object | None = None) -> object | None:
    """同一 ``doc_key`` 下**当前**的那一行（ready/indexing 里版本最大的）。"""
    runner = conn if conn is not None else get_connection()
    return runner.execute(  # type: ignore[attr-defined]
        "SELECT * FROM documents WHERE doc_key = ? AND status IN ('ready', 'indexing')"
        " ORDER BY version DESC LIMIT 1",
        (key,),
    ).fetchone()


def next_version(key: str, *, conn: object | None = None) -> int:
    """同一 ``doc_key`` 的下一个版本号（1 基）。"""
    runner = conn if conn is not None else get_connection()
    row = runner.execute(  # type: ignore[attr-defined]
        "SELECT COALESCE(MAX(version), 0) AS v FROM documents WHERE doc_key = ?", (key,)
    ).fetchone()
    return int(row["v"] or 0) + 1


def latest_ready_row(key: str, *, before_version: int) -> object | None:
    """同一 ``doc_key`` 下**上一个就绪版本**（``before_version`` 之前、版本最大的那个）。

    为什么不是"最大版本"：中间可能有一次向量化失败（那一版不是 ready），
    切换要找的是"当前对外服务的那一版"。
    """
    return get_connection().execute(
        "SELECT * FROM documents WHERE doc_key = ? AND status = 'ready' AND version < ?"
        " ORDER BY version DESC LIMIT 1",
        (key, before_version),
    ).fetchone()


def superseded_rows(older_than_s: float, *, now: float | None = None) -> list[object]:
    """**到期可物理删除**的退场版本（P1.9）：``superseded`` 且退场超过 ``older_than_s``。

    "6 小时回滚窗口"：用户发现新版本不对，窗口内还能 `rollback_to`；过了窗口才真删。
    """
    stamp = time.time() if now is None else now
    return list(
        get_connection().execute(
            "SELECT id, version, source_name, stored_path FROM documents"
            " WHERE status = 'superseded' AND COALESCE(superseded_at, updated_at) <= ?"
            " ORDER BY id",
            (stamp - float(older_than_s),),
        ).fetchall()
    )


def versions_of(doc_key: str) -> list[object]:
    """同一份资料的**全部版本**（新的在前；含已退场，供详情页展示与回滚）。"""
    if not doc_key:
        return []
    return list(
        get_connection().execute(
            "SELECT id, version, status, size, pages, chars, created_at, updated_at,"
            " superseded_at FROM documents WHERE doc_key = ? ORDER BY version DESC",
            (doc_key,),
        ).fetchall()
    )


def delete_many(doc_ids: Sequence[int]) -> list[str]:
    """删掉若干份资料（片段与索引由外键级联 + 触发器一起清）。

    返回：被删资料的 ``stored_path``（调用方据此清副本目录）——**副本是文件系统的事，
    存储层只负责库**。
    """
    ids = [int(doc_id) for doc_id in doc_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with transaction() as conn:
        rows = conn.execute(
            f"SELECT id, stored_path FROM documents WHERE id IN ({placeholders})", tuple(ids)
        ).fetchall()
        conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(ids))
    return [str(row["stored_path"] or "") for row in rows]


def get(doc_id: int) -> object | None:
    """按 id 取一行（重建要用它的 ``source_name`` 与 ``stored_path``）。

    返回：``sqlite3.Row``；不存在返回 ``None``。
    """
    return get_connection().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def all_stored_paths() -> list[str]:
    """全部资料的副本相对路径（清理孤儿副本时用它算"哪些还被引用着"）。"""
    rows = get_connection().execute(
        "SELECT stored_path FROM documents WHERE stored_path IS NOT NULL AND stored_path != ''"
    ).fetchall()
    return [str(row["stored_path"]) for row in rows]


def paths_in_use() -> set[str]:
    """所有**在册**资料引用的副本路径（清理副本时判断"这个目录还有没有人用"）。"""
    rows = get_connection().execute(
        "SELECT stored_path FROM documents WHERE stored_path != ''"
    ).fetchall()
    return {str(row["stored_path"]) for row in rows}


def clear_rerank_cache() -> int:
    """清空重排缓存（``rerank_cache`` 只记 ``chunk_id``，没有 ``doc_id``——
    删资料时整体清掉最安全，避免把已删片段的分数带回来）。返回清掉的条数。"""
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM rerank_cache")
        return int(cursor.rowcount or 0)


def set_pipeline_versions(
    conn: object,
    doc_id: int,
    *,
    parser_version: int,
    chunk_policy: int,
    embedding_version: int,
    verified_at: float | None = None,
    now: float | None = None,
) -> None:
    """记录这份资料是**哪套 pipeline** 产的，以及写入校验通过的时间。

    三个版本分开存（改切分不必重跑解析；换嵌入模型不必重解析），变化时据此决定是否重跑。
    """
    stamp = time.time() if now is None else now
    conn.execute(  # type: ignore[attr-defined]
        "UPDATE documents SET parser_version = ?, chunk_policy = ?, embedding_version = ?,"
        " verified_at = ?, updated_at = ? WHERE id = ?",
        (
            int(parser_version),
            int(chunk_policy),
            int(embedding_version),
            verified_at,
            stamp,
            int(doc_id),
        ),
    )


def _active_doc_ids() -> set[int]:
    """在册资料（ready/indexing）的 id 集合——Qdrant key 对账当"期望集合"用。"""
    rows = get_connection().execute(
        "SELECT id FROM documents WHERE status IN ('ready', 'indexing')"
    ).fetchall()
    return {int(row["id"]) for row in rows}
