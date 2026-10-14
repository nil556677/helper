"""导入编排：文件字节 → 资料行 + 片段行（含索引文本）。

**三级判重**（方案 §9），顺序不能乱：

1. **文件字节哈希**命中 → 零解析跳过（最快的路径，连解码都不用做）；
2. 都没命中才解析，算**正文指纹**（去掉所有空白后的 sha256）→ 命中则跳过，只刷新 ``file_hash``。
   这一级是为了"重新排版 / 换个名字另存"的文件：字节变了、正文没变，只按字节判重会白重建一次；
3. 正文真变了才落库。

**成功的资料要留副本**（``data/files/<hash12>/``）：重建索引、追溯原文都靠它。
解析失败的不留副本——原文件还在用户手上，留一份用不上的副本只是占地方。

**失败的资料也进库**（``status='failed'`` + ``detail`` 写清原因）：用户需要在列表里看到
"哪一份失败了、为什么"。只弹一次通知的话，关掉就再也找不到了。
（``error_code`` 这个机器判据留给以后的异步入库管道：那时才需要按错误码决定重试策略。）
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import get_connection, transaction
from app.core.features.rag.ingest import parsers
from app.core.features.rag.ingest.parsers import ParseError
from app.core.features.rag.ingest.splitter import split_text
from app.core.features.rag.ingest.store import replace_chunks, write_chunks

#: 文件名里不能出现的字符（Windows）+ 路径分隔符
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class ImportResult:
    """一份文件的导入结果。"""

    status: str
    """``imported`` / ``skipped`` / ``failed``。"""

    source_name: str
    reason: str = ""
    """给人看的原因（跳过或失败时）。"""

    doc_id: int | None = None
    chunks: int = 0
    notes: list[str] = field(default_factory=list)
    """成功但有要提醒的事（比如"PDF 没有文字层，可能是扫描件"）。"""


def _safe_name(name: str) -> str:
    """把文件名净化成可落盘的形状（保留中文，去掉路径与非法字符）。"""
    cleaned = _UNSAFE_NAME.sub("_", Path(name).name).strip(" .")
    return cleaned or "unnamed"


def _fingerprint(text: str) -> str:
    """正文指纹：去掉所有空白后的 sha256（为空表示正文里没有有效内容）。"""
    stripped = re.sub(r"\s+", "", text)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest() if stripped else ""


def _existing_by(column: str, value: str) -> int | None:
    row = get_connection().execute(
        f"SELECT id FROM documents WHERE {column} = ? ORDER BY id LIMIT 1", (value,)  # noqa: S608 - 列名由本模块固定
    ).fetchone()
    return int(row["id"]) if row else None


def _refresh_file_hash(doc_id: int, file_hash: str) -> None:
    """第二级判重命中时把新文件的字节哈希补上（下次这份文件走最快的路径）。"""
    with transaction() as conn:
        conn.execute(
            "UPDATE documents SET file_hash = ?, updated_at = ? WHERE id = ?",
            (file_hash, time.time(), doc_id),
        )


def _record_failure(name: str, size: int, reason: str) -> int:
    """把失败也记进资料表（用户在列表里能看到它、也能删掉它）。"""
    now = time.time()
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO documents(source_name, stored_path, size, status, detail, created_at,"
            " updated_at) VALUES (?, '', ?, 'failed', ?, ?, ?)",
            (name, size, reason, now, now),
        )
        return int(cursor.lastrowid or 0)


def import_bytes(data: bytes, filename: str) -> ImportResult:
    """导入一份文件的内容。

    参数：
        data: 文件字节。
        filename: 原始文件名（判重、副本命名与展示都用它）。
    返回：:class:`ImportResult`（三种状态之一；失败时 ``reason`` 可直接给用户看）。
    """
    source_name = Path(filename).name
    if not data:
        return ImportResult("failed", source_name, reason="文件是空的")

    file_hash = hashlib.sha256(data).hexdigest()
    known = _existing_by("file_hash", file_hash)
    if known is not None:
        return ImportResult("skipped", source_name, reason="这份文件已经在资料库里了", doc_id=known)

    try:
        parsed = parsers.parse(data, source_name)
    except ParseError as exc:
        doc_id = _record_failure(source_name, len(data), str(exc))
        logger.info("导入失败：{}（{}）", source_name, exc)
        return ImportResult("failed", source_name, reason=str(exc), doc_id=doc_id)

    text_hash = _fingerprint(parsed.text)
    if not text_hash:
        reason = "没有可检索的文字"
        if parsed.notes:
            reason = f"{reason}：{parsed.notes[0]}"
        doc_id = _record_failure(source_name, len(data), reason)
        return ImportResult("failed", source_name, reason=reason, doc_id=doc_id)

    known = _existing_by("text_hash", text_hash)
    if known is not None:
        _refresh_file_hash(known, file_hash)
        return ImportResult("skipped", source_name, reason="同样的内容已经在资料库里了", doc_id=known)

    chunks = split_text(parsed.text)
    if not chunks:
        doc_id = _record_failure(source_name, len(data), "切不出可检索的片段")
        return ImportResult("failed", source_name, reason="切不出可检索的片段", doc_id=doc_id)

    folder = paths.files_dir() / file_hash[:12]
    stored = folder / _safe_name(source_name)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(data)
    except OSError as exc:
        doc_id = _record_failure(source_name, len(data), f"写副本失败：{exc}")
        logger.error("写资料副本失败：{}", exc)
        return ImportResult("failed", source_name, reason=f"写副本失败：{exc}", doc_id=doc_id)

    now = time.time()
    stored_path = f"{file_hash[:12]}/{stored.name}"
    try:
        with transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO documents(source_name, stored_path, file_hash, text_hash, size, status,"
                " detail, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)",
                (
                    source_name,
                    stored_path,
                    file_hash,
                    text_hash,
                    len(data),
                    parsed.notes[0] if parsed.notes else None,
                    now,
                    now,
                ),
            )
            doc_id = int(cursor.lastrowid or 0)
            write_chunks(conn, doc_id, chunks, now=now)
    except Exception as exc:  # noqa: BLE001 - 允许失败：单份文件失败不该中断整批导入
        logger.exception("资料入库失败（{}）：{}", source_name, exc)
        return ImportResult("failed", source_name, reason=f"入库失败：{exc}")

    logger.info("已入库：{}（{} 个片段）", source_name, len(chunks))
    return ImportResult(
        "imported",
        source_name,
        doc_id=doc_id,
        chunks=len(chunks),
        notes=list(parsed.notes),
    )


def import_file(path: Path) -> ImportResult:
    """导入磁盘上的一个文件（命令行/测试用；界面走上传，见 app/services/knowledge.py）。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ImportResult("failed", Path(path).name, reason=f"读不到文件：{exc}")
    return import_bytes(data, path.name)


def rebuild_document(doc_id: int) -> ImportResult:
    """用**副本**重新解析并切分某份资料（换了切分参数、或解析器升级后用）。

    参数：
        doc_id: 资料 id。
    返回：:class:`ImportResult`（成功时 ``chunks`` 是新的片段数）。
    """
    row = get_connection().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return ImportResult("failed", "", reason="这份资料不在库里了")
    source_name = str(row["source_name"])
    stored = paths.files_dir() / str(row["stored_path"]) if row["stored_path"] else None
    if stored is None or not stored.is_file():
        return ImportResult("failed", source_name, reason="找不到副本，无法重建", doc_id=doc_id)

    try:
        parsed = parsers.parse(stored.read_bytes(), source_name)
    except (ParseError, OSError) as exc:
        with transaction() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', detail = ?, updated_at = ? WHERE id = ?",
                (f"重建失败：{exc}", time.time(), doc_id),
            )
        return ImportResult("failed", source_name, reason=f"重建失败：{exc}", doc_id=doc_id)

    chunks = split_text(parsed.text)
    now = time.time()
    with transaction() as conn:
        replace_chunks(conn, doc_id, chunks, now=now)
        conn.execute(
            "UPDATE documents SET status = 'ready', text_hash = ?, detail = ?, updated_at = ?"
            " WHERE id = ?",
            (_fingerprint(parsed.text), parsed.notes[0] if parsed.notes else None, now, doc_id),
        )
    logger.info("已重建：{}（{} 个片段）", source_name, len(chunks))
    return ImportResult("imported", source_name, doc_id=doc_id, chunks=len(chunks))


def delete_documents(doc_ids: list[int]) -> int:
    """删掉若干份资料（片段与索引由外键级联 + 触发器一起清掉），并尽力删副本。

    参数：
        doc_ids: 资料 id 列表。
    返回：实际删掉的资料条数。
    """
    if not doc_ids:
        return 0
    placeholders = ",".join("?" for _ in doc_ids)
    rows = get_connection().execute(
        f"SELECT id, stored_path FROM documents WHERE id IN ({placeholders})",  # noqa: S608 - 占位符由本模块生成
        tuple(doc_ids),
    ).fetchall()
    with transaction() as conn:
        conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", tuple(doc_ids))  # noqa: S608

    for row in rows:
        stored = str(row["stored_path"] or "")
        if not stored:
            continue
        folder = (paths.files_dir() / stored).parent
        try:
            shutil.rmtree(folder, ignore_errors=True)  # 副本目录按哈希分，删掉就够
        except OSError as exc:  # noqa: BLE001 - 允许失败：删不掉副本不影响库的一致性
            logger.warning("删副本失败（{}）：{}", folder, exc)
    logger.info("已删除 {} 份资料", len(rows))
    return len(rows)
