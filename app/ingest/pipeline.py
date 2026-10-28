"""净室 v2 入库流水线：解析 → 结构 → 切分 → SQLite + Qdrant。

解析/切分仍用现有 `core.parse` / `core.chunk`；只替换"写哪里"：
- parent/child 全文写 SQLite `chunks`；
- child 向量写 Qdrant 单集合（由 writer 负责）；
- 成功后 `activate`，再删旧版本。

writer 可注入（测试用 FakeWriter），默认 `QdrantChunkWriter`。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Any, Protocol

from app.core import paths
from app.core.chunk import Splitter, estimate_tokens
from app.core.chunk.diagnostics import Diagnostic, DiagnosticSink, Severity
from app.core.db.connection import transaction
from app.core.parse import iter_units
from app.core.parse.model import TYPE_HEADING, TYPE_IMAGE, Block
from app.core.parse.structure import Structurer
from app.rag import kb_store

#: 一批攒多少 child 再嵌入/写 Qdrant（沿用旧经验值）。
WRITE_BATCH_CHILDREN = 32


class ChunkWriter(Protocol):
    def add(self, items: list[dict], ids: list[str], metas: list[dict]) -> None: ...

    def activate(self, doc_id: int, version: int) -> None: ...

    def delete_version(self, doc_id: int, version: int) -> None: ...


@dataclass
class ImportOutcome:
    status: str  # imported / skipped / failed
    doc_id: int | None = None
    chunks: int = 0
    notes: list[str] = field(default_factory=list)
    diagnostics: tuple[Diagnostic, ...] = ()
    error_kind: str = ""
    reason: str = ""
    doc_title: str = ""
    pages: int | None = None


class _NothingToIndex(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _text_hash(parts: Sequence[str]) -> str:
    text = re.sub(r"\s+", "", "\n".join(parts))
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _embed_text(child: Any, doc_title: str) -> str:
    """嵌入文本 = 标题链 + 正文；标题与正文在 SQLite 里仍分开存。"""
    path = [doc_title] if doc_title and doc_title not in child.section_path else []
    path.extend(child.section_path)
    prefix = " › ".join(str(item) for item in path if str(item).strip())
    if prefix and not child.text.startswith(prefix):
        return f"{prefix}\n{child.text}"
    return child.text


def _copy_to_files(path: Path, name: str, file_hash: str) -> str:
    folder = paths.files_dir() / file_hash[:12]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / paths.safe_filename(name)
    if not target.exists():
        shutil.copyfile(path, target)
    return f"{file_hash[:12]}/{target.name}"


def _write_batch(
    doc_id: int,
    version: int,
    groups: Sequence[Any],
    start_seq: int,
    *,
    writer: ChunkWriter,
    source_name: str,
    doc_title: str = "",
) -> tuple[int, int]:
    """写一批 parent/child：SQLite 事务内落全文，提交后写 Qdrant。返回 (child 数, 下一 seq)。"""
    docs: list[dict] = []
    ids: list[str] = []
    metas: list[dict] = []
    next_seq = start_seq
    with transaction() as conn:
        for group in groups:
            parent = group.parent
            parent_id = kb_store.insert_parent(
                conn,
                doc_id=doc_id,
                version=version,
                text=parent.text,
                heading=" › ".join(parent.section_path),
                section=parent.section_path,
                loc=parent.loc_json(),
                token_count=estimate_tokens(parent.text),
                kind=parent.content_type,
                role=parent.role,
                fetch_mode=parent.fetch_mode,
                table_uid=parent.table_uid,
                row_uid=parent.row_uid,
                row_no=parent.row_no,
                segment_index=parent.segment_index,
                segment_count=parent.segment_count,
                merged_ranges=parent.merged_ranges,
            )
            for child in group.children:
                seq = next_seq
                next_seq += 1
                chunk_id = kb_store.insert_child(
                    conn,
                    doc_id=doc_id,
                    version=version,
                    parent_id=parent_id,
                    seq=seq,
                    text=child.text,
                    heading=" › ".join(child.section_path),
                    section=child.section_path,
                    loc=child.loc_json(),
                    token_count=estimate_tokens(child.text),
                    kind=child.content_type,
                    hard_split=bool(child.hard_split),
                    role=child.role,
                    fetch_mode=child.fetch_mode,
                    table_uid=child.table_uid,
                    row_uid=child.row_uid,
                    row_no=child.row_no,
                    segment_index=child.segment_index,
                    segment_count=child.segment_count,
                    merged_ranges=child.merged_ranges,
                )
                key = f"{doc_id}:{version}:{chunk_id}"
                meta = {
                    "chunk_id": int(chunk_id),
                    "chunk_key": key,
                    "doc_id": int(doc_id),
                    "parent_id": int(parent_id),
                    "version": int(version),
                    "seq": int(seq),
                    "status": "pending",
                    "source_name": source_name,
                    "kind": child.content_type,
                    "role": child.role,
                    "fetch_mode": int(child.fetch_mode),
                    "table_uid": child.table_uid,
                    "row_uid": child.row_uid,
                    "row_no": child.row_no,
                    "segment_index": child.segment_index,
                    "segment_count": int(child.segment_count),
                    "heading": " › ".join(child.section_path),
                    "loc": child.loc_json(),
                }
                docs.append(
                    {
                        "text": child.text,
                        "embed_text": _embed_text(child, doc_title),
                        "metadata": meta,
                    }
                )
                ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"helper:{key}")))
                metas.append(meta)
    try:
        writer.add(docs, ids, metas)
    except Exception:
        with __import__("contextlib").suppress(Exception):
            writer.delete_version(doc_id, version)
        kb_store.delete_version(doc_id, version)
        raise
    return len(ids), next_seq


def _stream(
    path: Path,
    *,
    source_name: str,
    doc_id: int,
    version: int,
    writer: ChunkWriter,
    on_progress: Any = None,
) -> tuple[int, int, str, tuple[Diagnostic, ...], str]:
    """返回 (children, pages, doc_title, diagnostics, text_hash)。"""
    text_parts: list[str] = []
    notes: list[str] = []
    pages: int | None = None
    sink = DiagnosticSink()
    unit_iter = iter_units(path, name=source_name)
    first_unit = next(unit_iter, None)
    if first_unit is None:
        raise _NothingToIndex("文件里没有可解析的内容")

    suffix = Path(source_name).suffix.lower()
    native_h1 = bool(
        first_unit.blocks
        and first_unit.blocks[0].content_type == TYPE_HEADING
        and int(first_unit.blocks[0].level or 0) == 1
    )
    doc_title_from_h1 = (not native_h1) or suffix not in (".pptx", ".xlsx", ".xlsm")
    if not native_h1:
        title = Path(source_name).stem.strip() or source_name
        first_unit.blocks.insert(0, Block(text=title, content_type=TYPE_HEADING, level=1))
    structure = Structurer(
        toc=first_unit.toc if first_unit is not None else None,
        doc_title_from_h1=doc_title_from_h1,
    )
    engine = Splitter.from_config()

    pending_groups: list[Any] = []
    pending_children = 0
    seq = 0
    total_children = 0
    unit_stream = chain([first_unit], unit_iter)

    for index, unit in enumerate(unit_stream, start=1):
        if unit.pages is not None:
            pages = unit.pages
        notes.extend(unit.notes)
        if unit.toc:
            structure = Structurer(toc=unit.toc, doc_title_from_h1=doc_title_from_h1)
        blocks = structure.feed(unit.blocks)
        for block in blocks:
            if block.content_type != TYPE_IMAGE:
                text_parts.append(block.text)
        if on_progress is not None:
            on_progress(index, pages or 0, f"解析第 {index} 个单元")
        engine.set_doc_title(structure.doc_title)
        doc_title = structure.doc_title or Path(source_name).stem
        groups = engine.feed(blocks)
        if groups:
            pending_groups.extend(groups)
            pending_children += sum(len(group.children) for group in groups)
            while pending_children >= WRITE_BATCH_CHILDREN:
                batch: list[Any] = []
                batch_children = 0
                while pending_groups:
                    group = pending_groups[0]
                    size = len(getattr(group, "children", ()))
                    if batch and batch_children + size > WRITE_BATCH_CHILDREN:
                        break
                    pending_groups.pop(0)
                    batch.append(group)
                    batch_children += size
                    if batch_children >= WRITE_BATCH_CHILDREN:
                        break
                if not batch:
                    break
                written, seq = _write_batch(
                    doc_id,
                    version,
                    batch,
                    seq,
                    writer=writer,
                    source_name=source_name,
                    doc_title=doc_title,
                )
                total_children += written
                pending_children -= batch_children
                if on_progress is not None:
                    on_progress(total_children, 0, f"向量化 {total_children} 个片段…")

    doc_title = structure.doc_title or Path(source_name).stem
    pending_groups.extend(engine.flush())
    sink.extend(engine.sink)
    if pending_groups:
        written, seq = _write_batch(
            doc_id,
            version,
            pending_groups,
            seq,
            writer=writer,
            source_name=source_name,
            doc_title=doc_title,
        )
        total_children += written
    if total_children == 0:
        raise _NothingToIndex("没有可检索的文字（可能是扫描件或空文档）")
    outcome_diag = tuple(sink.items())
    errors = [item for item in outcome_diag if item.severity is Severity.ERROR]
    if errors:
        raise ValueError(errors[0].message)
    return total_children, pages or 0, doc_title, outcome_diag, _text_hash(text_parts)


def import_path(
    path: Path,
    name: str | None = None,
    *,
    claim_doc_id: int | None = None,
    reuse_stored: bool = False,
    on_progress: Any = None,
    writer: ChunkWriter | None = None,
    embedding: Any = None,
    sparse_embedding: Any = None,
) -> ImportOutcome:
    """导入一份文件；返回 Outcome（不抛业务异常）。"""
    source_name = name or path.name
    if not path.is_file():
        return ImportOutcome("failed", error_kind="not_found", reason="文件不存在")
    try:
        file_hash = _sha256(path)
    except OSError as exc:
        return ImportOutcome("failed", error_kind="io", reason=f"读文件失败：{exc}")

    existing = kb_store.find_by_file_hash(file_hash)
    if existing is not None and (
        claim_doc_id is None or int(existing["id"]) != int(claim_doc_id)
    ):
        return ImportOutcome(
            "skipped",
            doc_id=int(existing["id"]),
            reason="这份文件已经在资料库里了",
        )

    try:
        if reuse_stored:
            stored_path = str(path.relative_to(paths.files_dir())).replace("\\", "/")
        else:
            stored_path = _copy_to_files(path, source_name, file_hash)
    except (OSError, ValueError) as exc:
        return ImportOutcome("failed", error_kind="io", reason=f"写副本失败：{exc}")

    if claim_doc_id is not None:
        old = kb_store.get_document(int(claim_doc_id))
        if old is None:
            return ImportOutcome("failed", error_kind="not_found", reason="要重建的资料不存在")
        doc_id = int(claim_doc_id)
        version = int(old["version"] or 1) + 1
        kb_store.update_document(
            doc_id,
            status="pending",
            version=version,
            error_kind="",
            error="",
            stored_path=stored_path,
            file_hash=file_hash,
            size=path.stat().st_size,
        )
        old_version = int(old["version"] or 1)
    else:
        doc_id = kb_store.insert_document(
            name=source_name,
            stored_path=stored_path,
            file_hash=file_hash,
            size=path.stat().st_size,
            status="pending",
            version=1,
        )
        version = 1
        old_version = 0

    if writer is None:
        from app.ingest.qdrant_writer import QdrantChunkWriter

        writer = QdrantChunkWriter(embedding=embedding, sparse_embedding=sparse_embedding)

    try:
        children, pages, doc_title, diagnostics, text_hash = _stream(
            path,
            source_name=source_name,
            doc_id=doc_id,
            version=version,
            writer=writer,
            on_progress=on_progress,
        )
    except _NothingToIndex as exc:
        kb_store.mark_failed(doc_id, kind="unsupported", detail=str(exc))
        return ImportOutcome("failed", doc_id=doc_id, error_kind="unsupported", reason=str(exc))
    except Exception as exc:
        kb_store.mark_failed(doc_id, kind="bad_request", detail=str(exc))
        return ImportOutcome("failed", doc_id=doc_id, error_kind="bad_request", reason=str(exc))

    existing_text = kb_store.find_by_text_hash(text_hash) if text_hash else None
    if existing_text is not None and int(existing_text["id"]) != doc_id:
        with __import__("contextlib").suppress(Exception):
            writer.delete_version(doc_id, version)
        kb_store.delete_version(doc_id, version)
        kb_store.delete_document(doc_id)
        return ImportOutcome(
            "skipped",
            doc_id=int(existing_text["id"]),
            reason="同样的内容已经在资料库里了",
        )

    kb_store.update_document(doc_id, text_hash=text_hash, doc_title=doc_title)
    try:
        writer.activate(doc_id, version)
        kb_store.activate_version(doc_id, version, pages=pages or None)
    except Exception as exc:
        with __import__("contextlib").suppress(Exception):
            writer.delete_version(doc_id, version)
        kb_store.delete_version(doc_id, version)
        kb_store.mark_failed(doc_id, kind="io", detail=f"写入向量失败：{exc}")
        return ImportOutcome("failed", doc_id=doc_id, error_kind="io", reason=str(exc))
    if old_version and old_version != version:
        with __import__("contextlib").suppress(Exception):
            writer.delete_version(doc_id, old_version)
        kb_store.delete_version(doc_id, old_version)
    return ImportOutcome(
        "imported",
        doc_id=doc_id,
        chunks=children,
        notes=[],
        diagnostics=diagnostics,
        doc_title=doc_title,
        pages=pages or None,
    )


def import_stored(path: Path, name: str | None = None, **kwargs: Any) -> ImportOutcome:
    """重置/重导：文件已在 data/files/ 里，不再复制。"""
    return import_path(path, name=name, reuse_stored=True, **kwargs)
