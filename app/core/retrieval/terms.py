"""全局领域词（自定义关键词）的**后台读写**：存 ``token_dict`` 表，一行一词。

为什么存库而不是用文本文件（方案 A）：
- **单一事实来源**：词条随库一起备份，不再依赖相对路径的文本文件；
- **可管理**：设置页能增 / 删 / 改 / 列，不用手改文件；
- **可对账**：词条内容能算稳定哈希（:func:`terms_hash`），已接进词法指纹。

**本模块只管"存 / 取 / 改 / 删"**，不负责切词。谁把词喂给 jieba 是
:mod:`app.core.retrieval.tokenize` 的事；词条真正进入关键词索引，还要等
sparse（词法路）落地（见 ``docs/检索器设计.md`` §6.4）。

约定：
- 词条**去首尾空白后原样保存**（不转小写：大小写归一属于检索侧，不该改用户输入）；
- 词条不允许为空、不允许含空白字符（与"一行一词"的约定一致）；
- ``freq`` 为 ``None`` 表示用 jieba 默认词频；``enabled=False`` 的词保留但不参与切词。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass

from loguru import logger

from app.core.db.connection import get_connection, transaction

#: 词条最大长度（字符）。领域词/公司名/项目名远小于这个数；防止误粘贴整段文本。
MAX_TERM_CHARS = 128

#: 更新时表示"这个字段不改"的哨兵（与"显式改成 None"区分开）。
_KEEP = object()

#: 进程内版本号：**每次增删改 +1**。分词器据此判断要不要重建（比每次读库算哈希便宜）。
#: 跨进程改库（另一个进程 / 手改 SQL）不会 bump，需重启；单实例守卫下这是可接受的。
_generation = 0


def generation() -> int:
    """当前词条版本号（分词器缓存键用）。"""
    return _generation


def _bump() -> None:
    global _generation
    _generation += 1



class TermError(ValueError):
    """词条非法或操作不允许（空词 / 重复 / 不存在）。"""


@dataclass(frozen=True)
class Term:
    """一条全局领域词。"""

    term: str
    freq: int | None
    enabled: bool
    updated_at: float


@dataclass(frozen=True)
class ImportResult:
    """批量导入的结果（界面据此说"新增 X / 更新 Y / 跳过 Z"）。"""

    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()


def _normalize(term: object) -> str:
    """校验并规整词条；非法输入抛 :class:`TermError`。"""
    text = str(term or "").strip()
    if not text:
        raise TermError("词条不能为空")
    if any(ch.isspace() for ch in text):
        raise TermError(f"词条不能含空白字符：{text!r}")
    if len(text) > MAX_TERM_CHARS:
        raise TermError(f"词条太长（{len(text)} > {MAX_TERM_CHARS}）：{text[:16]}…")
    return text


def _check_freq(freq: object) -> int | None:
    """校验词频：``None``（默认）或非负整数。"""
    if freq is None:
        return None
    if isinstance(freq, bool) or not isinstance(freq, int):
        raise TermError(f"词频必须是整数或空：{freq!r}")
    if freq < 0:
        raise TermError(f"词频不能为负数：{freq}")
    return int(freq)


def _freq_db(freq: int | None) -> int:
    """落库的词频：``None``（="用 jieba 默认"）按数据库默认 **1** 存。

    ``token_dict.freq`` 是 ``NOT NULL DEFAULT 1``——直接写 ``NULL`` 触发 IntegrityError，
    会被上层误判成"词条已存在"（实测：不带词频添加就一直报这个）。None 落成 1
    正好等价于列默认值，语义也一致。
    """
    return 1 if freq is None else freq


def _row_to_term(row: sqlite3.Row) -> Term:
    return Term(
        term=str(row["term"]),
        freq=None if row["freq"] is None else int(row["freq"]),
        enabled=bool(row["enabled"]),
        updated_at=float(row["updated_at"]),
    )


def list_terms(*, enabled_only: bool = False) -> list[Term]:
    """列出所有词条（按词条字典序；``enabled_only`` 只返回启用的）。"""
    sql = "SELECT term, freq, enabled, updated_at FROM token_dict"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY term"
    rows = get_connection().execute(sql).fetchall()
    return [_row_to_term(row) for row in rows]


def get_term(term: str) -> Term | None:
    """按词条精确查一条；没有返回 ``None``。"""
    row = get_connection().execute(
        "SELECT term, freq, enabled, updated_at FROM token_dict WHERE term = ?",
        (_normalize(term),),
    ).fetchone()
    return _row_to_term(row) if row is not None else None


def add_term(term: str, *, freq: int | None = None, enabled: bool = True) -> Term:
    """新增一条词条。

    参数：
        term: 词条正文（去首尾空白后保存）。
        freq: jieba 词频；``None`` = 默认。
        enabled: 是否参与切词。
    异常：
        TermError: 词条非法或**已存在**。
    """
    text = _normalize(term)
    freq = _check_freq(freq)
    db_freq = _freq_db(freq)  # None → 1（NOT NULL 列，直接写 None 会误判成"已存在"）
    now = time.time()
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO token_dict(term, freq, enabled, updated_at) VALUES (?, ?, ?, ?)",
                (text, db_freq, 1 if enabled else 0, now),
            )
    except sqlite3.IntegrityError as exc:
        raise TermError(f"词条已存在：{text}") from exc
    _bump()
    return Term(term=text, freq=db_freq, enabled=bool(enabled), updated_at=now)


def update_term(
    term: str,
    *,
    freq: object = _KEEP,
    enabled: object = _KEEP,
    new_term: object = _KEEP,
) -> Term:
    """修改一条词条（只改传入的字段）。

    参数：
        term: 现有词条。
        freq: 新词频；不传则保持原值。
        enabled: 新启用状态。
        new_term: 改名（主键变更）；不传则不改。
    异常：
        TermError: 词条不存在 / 新词条非法 / 改名后与已有词条重复。
    """
    text = _normalize(term)
    current = get_term(text)
    if current is None:
        raise TermError(f"词条不存在：{text}")

    target = current.term if new_term is _KEEP else _normalize(new_term)
    next_freq = current.freq if freq is _KEEP else _check_freq(freq)
    next_freq_db = _freq_db(next_freq)  # None → 1（NOT NULL）
    next_enabled = current.enabled if enabled is _KEEP else bool(enabled)
    now = time.time()

    try:
        with transaction() as conn:
            if target != current.term:
                conn.execute(
                    "UPDATE token_dict SET term = ?, freq = ?, enabled = ?, updated_at = ?"
                    " WHERE term = ?",
                    (target, next_freq_db, 1 if next_enabled else 0, now, text),
                )
            else:
                conn.execute(
                    "UPDATE token_dict SET freq = ?, enabled = ?, updated_at = ? WHERE term = ?",
                    (next_freq_db, 1 if next_enabled else 0, now, text),
                )
    except sqlite3.IntegrityError as exc:
        raise TermError(f"词条已存在：{target}") from exc

    _bump()
    return Term(term=target, freq=next_freq, enabled=next_enabled, updated_at=now)


def delete_term(term: str) -> bool:
    """删除一条词条；返回是否真的删掉了（不存在为 ``False``）。"""
    text = _normalize(term)
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM token_dict WHERE term = ?", (text,))
    deleted = int(cursor.rowcount or 0) > 0
    if deleted:
        _bump()
    return deleted


def load_terms(*, enabled_only: bool = True) -> list[tuple[str, int | None]]:
    """给分词器用：返回 ``(词, 词频)`` 列表。

    **库不可用 / 还没建库时返回空表**而不是抛异常、也不创建库文件：分词是切词的底层
    能力，纯文本单测 / CLI 场景不该因为"没建库"就被打断，更不该侧创建一个库。
    """
    from app.core import paths

    try:
        if not paths.db_file().is_file():
            return []
        return [(item.term, item.freq) for item in list_terms(enabled_only=enabled_only)]
    except sqlite3.Error as exc:
        logger.warning("词条表不可用，按空词表处理：{}", exc)
        return []


def terms_hash() -> str:
    """词条内容的稳定哈希（进词法指纹；空表/不可用返回确定的字面量）。

    为什么空表不返回 ``""``：空串容易被当成"没这一项"而漏判；
    ``"empty"`` / ``"unavailable"`` 都是明确的、能比较的状态。
    """
    from app.core import paths

    if not paths.db_file().is_file():
        return "unavailable"
    try:
        rows = get_connection().execute(
            "SELECT term, freq, enabled FROM token_dict ORDER BY term"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("词条表不可用，哈希记为 unavailable：{}", exc)
        return "unavailable"
    if not rows:
        return "empty"
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{row['term']}\x00{row['freq']}\x00{int(row['enabled'])}\n".encode()
        )
    return "sha256:" + digest.hexdigest()[:16]


def _parse_line(raw: str) -> tuple[str, int | None]:
    """解析一行 ``词`` 或 ``词 词频``；非法抛 :class:`TermError`。"""
    parts = raw.split()
    if len(parts) > 2:
        raise TermError(f"一行最多两列（词 [词频]）：{raw}")
    word = _normalize(parts[0])
    if len(parts) == 1:
        return word, None
    try:
        freq = int(parts[1])
    except ValueError as exc:
        raise TermError(f"词频不是整数：{raw}") from exc
    return word, _check_freq(freq)


def import_terms(text: str, *, enabled: bool = True) -> ImportResult:
    """批量导入词条（一行一词，``词 [词频]``；``#`` 注释与空行跳过）。

    语义：**upsert**——不存在就新增；已存在则启用它，且**只有该行带了词频时才改词频**
    （``词`` 一行不会把已有词频清空）。同一批里重复出现的词只取第一次，其余记为跳过。

    参数：
        text: 原文本（每行一个词条）。
        enabled: 导入的词条是否启用。
    返回：:class:`ImportResult`（新增/更新/跳过计数 + 逐行错误说明）。
    """
    added = updated = skipped = 0
    errors: list[str] = []
    seen: set[str] = set()

    for line_no, line in enumerate(str(text or "").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            word, freq = _parse_line(raw)
        except TermError as exc:
            skipped += 1
            errors.append(f"第 {line_no} 行：{exc}")
            continue
        if word in seen:
            skipped += 1
            errors.append(f"第 {line_no} 行：重复词条 {word}")
            continue
        seen.add(word)
        try:
            existing = get_term(word)
            if existing is None:
                add_term(word, freq=freq, enabled=enabled)
                added += 1
            else:
                update_term(
                    word,
                    freq=freq if freq is not None else _KEEP,
                    enabled=enabled,
                )
                updated += 1
        except TermError as exc:
            skipped += 1
            errors.append(f"第 {line_no} 行：{exc}")

    return ImportResult(added=added, updated=updated, skipped=skipped, errors=tuple(errors))


def export_terms(*, enabled_only: bool = True) -> str:
    """导出成可再导入的文本（一行一词，``词 [词频]``；带注释头）。

    默认**只导出启用的词条**：把停用词也导出，用户再导入时会被重新启用，
    与"我停用了它"的意图相反。
    """
    lines = [
        "# 全局领域词（一行一词；# 开头是注释）",
        "# 格式：词    或    词 词频",
        "",
    ]
    for item in list_terms(enabled_only=enabled_only):
        lines.append(item.term if item.freq is None else f"{item.term} {item.freq}")
    return "\n".join(lines) + "\n"
