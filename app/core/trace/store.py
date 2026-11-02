"""trace 的 SQLite 存储（唯一读写入口）。

约定与 :mod:`app.rag.kb_store` 一致：写走 `transaction()`；读返回 ``sqlite3.Row`` 或
:mod:`app.core.trace.model` 的值对象。**本模块不捕获异常**——fail-open 由
recorder 层做（trace 写失败只记日志，不影响回答）。
"""

from __future__ import annotations

import json
import time

from app.core.db.aio import AsyncDB  # 异步连接（全库单异步写者方案）
from app.core.db.connection import get_connection, transaction  # 同步连接
from app.core.trace.model import TraceDetail, TraceEvent, TraceHit, TraceRun, TraceSpan  # 模型

#: 单字段 JSON 截断长度（后台展示够用，又不会把库撑爆）。
_JSON_CAP = 8000


def _json(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"error": "不可序列化"}, ensure_ascii=False)
    if len(text) > _JSON_CAP:
        text = text[:_JSON_CAP] + '...("truncated")'
    return text


def _cap(text: str | None, limit: int = _JSON_CAP) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def create_run(
    run_id: str,
    *,
    question: str,
    conversation_id: str = "",
    parent_run_id: str | None = None,
    kind: str = "chat",
    started_at: float | None = None,
    metadata: dict | None = None,
) -> None:
    stamp = time.time() if started_at is None else started_at
    with transaction() as conn:
        conn.execute(
            "INSERT INTO trace_runs(id, parent_run_id, conversation_id, kind, question,"
            " started_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                parent_run_id,
                conversation_id,
                kind,
                _cap(question, 4000),
                stamp,
                _json(metadata) or "{}",
            ),
        )


#: update_run 允许更新的列（白名单，防注入）。
_RUN_FIELDS: dict[str, str] = {
    "normalized_query": "normalized_query",
    "rewritten_query": "rewritten_query",
    "route_json": "route_json",
    "answer": "answer",
    "model": "model",
    "ttft_ms": "ttft_ms",
    "metadata_json": "metadata_json",
}


def update_run(run_id: str, **fields: object) -> None:
    """按白名单更新运行摘要字段（route/query/answer/model/metadata）。"""
    allowed = {key: value for key, value in fields.items() if key in _RUN_FIELDS}
    if not allowed:
        return
    sets = ", ".join(f"{_RUN_FIELDS[key]} = ?" for key in allowed)
    values = [str(value) for value in allowed.values()]
    with transaction() as conn:
        conn.execute(f"UPDATE trace_runs SET {sets} WHERE id = ?", [*values, run_id])


def finish_run(
    run_id: str,
    *,
    status: str = "ok",
    error_kind: str = "",
    error_detail: str = "",
    finished_at: float | None = None,
    ttft_ms: int | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    cache_hit: bool = False,
) -> None:
    stamp = time.time() if finished_at is None else finished_at
    with transaction() as conn:
        row = conn.execute(
            "SELECT started_at FROM trace_runs WHERE id = ?", (run_id,)
        ).fetchone()
        started = float(row["started_at"]) if row else stamp
        duration = int((stamp - started) * 1000)
        total = (
            total_tokens
            if total_tokens is not None
            else int(prompt_tokens) + int(completion_tokens)
        )
        conn.execute(
            "UPDATE trace_runs SET status = ?, error_kind = ?, error_detail = ?,"
            " finished_at = ?, duration_ms = ?, ttft_ms = COALESCE(?, ttft_ms), prompt_tokens = ?,"
            " completion_tokens = ?, total_tokens = ?, cache_hit = ? WHERE id = ?",
            (
                status,
                _cap(error_kind, 60),
                _cap(error_detail, 2000),
                stamp,
                duration,
                ttft_ms,
                int(prompt_tokens),
                int(completion_tokens),
                int(total),
                1 if cache_hit else 0,
                run_id,
            ),
        )


async def create_run_async(  # 异步建 run（全库单异步写者用）
    db: AsyncDB,  # 异步连接
    run_id: str,  # run id
    *,  # 仅关键字
    question: str,  # 问题
    conversation_id: str = "",  # 会话
    parent_run_id: str | None = None,  # 父 run
    kind: str = "chat",  # 类型
    started_at: float | None = None,  # 开始时间
    metadata: dict | None = None,  # 元数据
) -> None:  # 无返回
    """与同步 ``create_run`` 同 SQL；事务用 db.write_lock + BEGIN IMMEDIATE。"""
    stamp = time.time() if started_at is None else started_at  # 时间戳
    async with db.write_lock:  # 串行写
        conn = db.conn  # 连接
        try:  # 事务
            await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
            await conn.execute(  # 插入
                "INSERT INTO trace_runs(id, parent_run_id, conversation_id, kind, question,"
                " started_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, parent_run_id, conversation_id, kind, _cap(question, 4000), stamp, _json(metadata) or "{}"),
            )  # 插入结束
            await conn.commit()  # 提交
        except BaseException:  # 失败
            await conn.rollback()  # 回滚
            raise  # 重抛


async def update_run_async(db: AsyncDB, run_id: str, **fields: object) -> None:  # 异步更新
    """与同步 ``update_run`` 同白名单；事务用异步连接。"""
    allowed = {key: value for key, value in fields.items() if key in _RUN_FIELDS}  # 白名单
    if not allowed:  # 空
        return  # 跳过
    sets = ", ".join(f"{_RUN_FIELDS[key]} = ?" for key in allowed)  # SET 子句
    values = [str(value) for value in allowed.values()]  # 值
    async with db.write_lock:  # 串行写
        conn = db.conn  # 连接
        try:  # 事务
            await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
            await conn.execute(f"UPDATE trace_runs SET {sets} WHERE id = ?", [*values, run_id])  # 更新
            await conn.commit()  # 提交
        except BaseException:  # 失败
            await conn.rollback()  # 回滚
            raise  # 重抛


async def finish_run_async(  # 异步收尾 run
    db: AsyncDB,  # 异步连接
    run_id: str,  # run id
    *,  # 仅关键字
    status: str = "ok",  # 状态
    error_kind: str = "",  # 失败类型
    error_detail: str = "",  # 失败详情
    finished_at: float | None = None,  # 结束时间
    ttft_ms: int | None = None,  # 首 token
    prompt_tokens: int = 0,  # 输入
    completion_tokens: int = 0,  # 输出
    total_tokens: int | None = None,  # 总数
    cache_hit: bool = False,  # 缓存命中
) -> None:  # 无返回
    """与同步 ``finish_run`` 同 SQL；事务用异步连接。"""
    stamp = time.time() if finished_at is None else finished_at  # 时间戳
    async with db.write_lock:  # 串行写
        conn = db.conn  # 连接
        try:  # 事务
            await conn.execute("BEGIN IMMEDIATE")  # 拿写锁
            row = await (  # 查开始时间
                await conn.execute(  # 查询
                    "SELECT started_at FROM trace_runs WHERE id = ?", (run_id,)
                )  # 查询结束
            ).fetchone()  # 取
            started = float(row["started_at"]) if row else stamp  # 开始时间
            duration = int((stamp - started) * 1000)  # 耗时
            total = (  # 总 token
                total_tokens  # 给定
                if total_tokens is not None  # 有
                else int(prompt_tokens) + int(completion_tokens)  # 计算
            )  # 结束
            await conn.execute(  # 更新终态
                "UPDATE trace_runs SET status = ?, error_kind = ?, error_detail = ?,"
                " finished_at = ?, duration_ms = ?, ttft_ms = COALESCE(?, ttft_ms), prompt_tokens = ?,"
                " completion_tokens = ?, total_tokens = ?, cache_hit = ? WHERE id = ?",
                (
                    status, _cap(error_kind, 60), _cap(error_detail, 2000), stamp, duration,
                    ttft_ms, int(prompt_tokens), int(completion_tokens), int(total),
                    1 if cache_hit else 0, run_id,
                ),
            )  # 更新结束
            await conn.commit()  # 提交
        except BaseException:  # 失败
            await conn.rollback()  # 回滚
            raise  # 重抛


def add_span(
    run_id: str,
    name: str,
    kind: str = "node",
    *,
    parent_span_id: int | None = None,
    input: dict | None = None,
    output: dict | None = None,
    status: str = "running",
    started_at: float | None = None,
) -> int:
    stamp = time.time() if started_at is None else started_at
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO trace_spans(run_id, parent_span_id, name, kind, status,"
            " started_at, input_json, output_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                parent_span_id,
                _cap(name, 120),
                _cap(kind, 40),
                _cap(status, 40),
                stamp,
                _json(input),
                _json(output),
            ),
        )
        return int(cursor.lastrowid)


def finish_span(
    span_id: int,
    *,
    status: str = "ok",
    output: dict | None = None,
    error: BaseException | None = None,
) -> None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT started_at FROM trace_spans WHERE id = ?", (span_id,)
        ).fetchone()
        if row is None:
            return
        duration = int((time.time() - float(row["started_at"])) * 1000)
        error_json = _json({"message": str(error)}) if error is not None else None
        conn.execute(
            "UPDATE trace_spans SET status = ?, duration_ms = ?, output_json = ?,"
            " error_json = ? WHERE id = ?",
            (
                _cap(status, 40),
                duration,
                _json(output),
                error_json,
                span_id,
            ),
        )


_HIT_COLUMNS = (
    "run_id",
    "span_id",
    "stage",
    "rank",
    "chunk_id",
    "chunk_key",
    "doc_id",
    "source_name",
    "heading",
    "loc_json",
    "score",
    "score_kind",
    "selected",
    "text",
    "snippet",
)


def add_hits(run_id: str, hits: list[dict[str, object]], *, span_id: int | None = None) -> None:
    if not hits:
        return
    with transaction() as conn:
        for hit in hits:
            loc_raw = hit.get("loc_json") or hit.get("loc") or {}  # 兼容两种字段
            if isinstance(loc_raw, (dict, list)):  # 结构化
                loc_text = json.dumps(loc_raw, ensure_ascii=False)  # 转 JSON
            else:  # 字符串
                loc_text = str(loc_raw)  # 原样
            row = {
                "run_id": run_id,
                "span_id": span_id if span_id is not None else hit.get("span_id"),
                "stage": _cap(str(hit.get("stage", "")), 40),
                "rank": int(hit.get("rank") or 0),
                "chunk_id": hit.get("chunk_id"),
                "chunk_key": _cap(str(hit.get("chunk_key", "")), 160),
                "doc_id": hit.get("doc_id"),
                "source_name": _cap(str(hit.get("source_name", "")), 240),
                "heading": _cap(str(hit.get("heading", "")), 500),
                "loc_json": _cap(loc_text, 800),
                "score": hit.get("score"),
                "score_kind": _cap(str(hit.get("score_kind", "")), 20),
                "selected": 1 if hit.get("selected") else 0,
                "text": _cap(str(hit.get("text", "")), 4000),
                "snippet": _cap(str(hit.get("snippet", "")), 400),
            }
            conn.execute(
                "INSERT INTO trace_hits(" + ", ".join(_HIT_COLUMNS) + ") VALUES ("
                + ", ".join("?" for _ in _HIT_COLUMNS)
                + ")",
                tuple(row[column] for column in _HIT_COLUMNS),
            )


def add_event(
    run_id: str,
    *,
    level: str = "info",
    code: str = "",
    message: str,
    detail: dict | None = None,
    at: float | None = None,
) -> None:
    stamp = time.time() if at is None else at
    with transaction() as conn:
        conn.execute(
            "INSERT INTO trace_events(run_id, at, level, code, message, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stamp,
                _cap(level, 20),
                _cap(code, 80),
                _cap(message, 1000),
                _json(detail),
            ),
        )


def has_running() -> bool:
    row = get_connection().execute(
        "SELECT 1 FROM trace_runs WHERE status IN ('running', 'waiting') LIMIT 1"
    ).fetchone()
    return row is not None


def hit_counts(run_ids: list[str]) -> dict[str, int]:
    """一次查询批量取每个 run 的命中条数。"""
    run_ids = [run_id for run_id in run_ids if run_id]
    if not run_ids:
        return {}
    placeholders = ", ".join("?" for _ in run_ids)
    rows = get_connection().execute(
        f"SELECT run_id, COUNT(*) AS n FROM trace_hits WHERE run_id IN ({placeholders})"
        " GROUP BY run_id",
        run_ids,
    ).fetchall()
    return {str(row["run_id"]): int(row["n"]) for row in rows}


def _where(
    *,
    days: int,
    status: str,
    query: str,
    only_search: bool,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    days = max(0, int(days))  # 归一成非负整数：防字符串 days 让 cutoff 变成 float - str
    if days > 0:
        clauses.append("t.started_at >= ?")
        params.append(time.time() - days * 86400)
    if status and status != "all":
        clauses.append("t.status = ?")
        params.append(status)
    if query and query.strip():
        clauses.append("t.question LIKE ? ESCAPE '\\'")
        escaped = (
            str(query)
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        params.append(f"%{escaped}%")
    if only_search:
        clauses.append(
            "EXISTS (SELECT 1 FROM trace_hits h WHERE h.run_id = t.id)"
        )
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_runs(
    *,
    days: int = 30,
    status: str = "all",
    query: str = "",
    only_search: bool = False,
    page: int = 1,
    size: int = 20,
) -> tuple[list[TraceRun], int]:
    where, params = _where(
        days=days, status=status, query=query, only_search=only_search
    )
    conn = get_connection()
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM trace_runs t {where}", params
        ).fetchone()[0]
    )
    offset = max(0, (max(1, page) - 1) * max(1, size))
    rows = conn.execute(
        f"SELECT * FROM trace_runs t {where} ORDER BY t.started_at DESC, t.id DESC LIMIT ? OFFSET ?",
        [*params, max(1, size), offset],
    ).fetchall()
    return [TraceRun.from_row(row) for row in rows], total


def stats(days: int = 1) -> dict[str, object]:
    conn = get_connection()
    days = max(0, int(days))  # 归一成非负整数：字符串/浮点 days 会让 days*86400 变成字符串重复 → float-str 报错
    cutoff = time.time() - days * 86400
    runs = conn.execute(
        "SELECT status, duration_ms, cache_hit, total_tokens, question FROM trace_runs"
        " WHERE started_at >= ?",
        (cutoff,),
    ).fetchall()
    total = len(runs)
    ok = sum(1 for r in runs if r["status"] == "ok")
    failed = sum(1 for r in runs if r["status"] in ("error",))
    waiting = sum(1 for r in runs if r["status"] == "waiting")
    durations = sorted(
        int(r["duration_ms"]) for r in runs if r["duration_ms"] is not None
    )
    hit_count = int(
        conn.execute(
            "SELECT COUNT(DISTINCT h.run_id) FROM trace_hits h"
            " JOIN trace_runs t ON t.id = h.run_id WHERE t.started_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    )
    return {
        "total": total,
        "ok": ok,
        "failed": failed,
        "waiting": waiting,
        "search": hit_count,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "cache_rate": (
            round(
                sum(1 for r in runs if r["cache_hit"]) / total * 100, 1
            )
            if total
            else 0.0
        ),
        "tokens": sum(int(r["total_tokens"] or 0) for r in runs),
    }


def _percentile(sorted_values: list[int], q: float) -> int | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int((len(sorted_values) - 1) * q))
    return sorted_values[index]


def error_kinds(days: int = 7, limit: int = 5) -> list[tuple[str, int]]:
    """失败原因 Top N：`[(error_kind, 次数)]`，总览页用。"""
    days = max(0, int(days))  # 归一成非负整数（防字符串 days）
    limit = max(1, int(limit))  # 归一成正整数
    cutoff = time.time() - days * 86400
    rows = get_connection().execute(
        "SELECT error_kind, COUNT(*) AS n FROM trace_runs"
        " WHERE started_at >= ? AND status = 'error' AND error_kind <> ''"
        " GROUP BY error_kind ORDER BY n DESC LIMIT ?",
        (cutoff, int(limit)),
    ).fetchall()
    return [(str(row["error_kind"]), int(row["n"])) for row in rows]


def detail(run_id: str) -> TraceDetail | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM trace_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    run = TraceRun.from_row(row)
    spans = [
        TraceSpan.from_row(r)
        for r in conn.execute(
            "SELECT * FROM trace_spans WHERE run_id = ? ORDER BY started_at", (run_id,)
        ).fetchall()
    ]
    hits = [
        TraceHit.from_row(r)
        for r in conn.execute(
            "SELECT * FROM trace_hits WHERE run_id = ? ORDER BY stage, rank", (run_id,)
        ).fetchall()
    ]
    events = [
        TraceEvent.from_row(r)
        for r in conn.execute(
            "SELECT * FROM trace_events WHERE run_id = ? ORDER BY at", (run_id,)
        ).fetchall()
    ]
    return TraceDetail(run=run, spans=tuple(spans), hits=tuple(hits), events=tuple(events))


def prune(days: int = 30, max_runs: int = 10000) -> int:
    """删过期/超量运行；级联删 spans/hits/events。返回删除条数。"""
    days = max(1, int(days))  # 归一成正整数（防字符串 days）
    cutoff = time.time() - max(1, days) * 86400
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM trace_runs WHERE started_at < ?", (cutoff,))
        count = int(cursor.rowcount)
        rows = conn.execute("SELECT id FROM trace_runs ORDER BY started_at DESC").fetchall()
        for row in rows[max(1, max_runs):]:
            conn.execute("DELETE FROM trace_runs WHERE id = ?", (row["id"],))
            count += 1
        return count


def clear() -> int:
    """清空全部 trace（不碰知识库/会话/任务）。返回删除条数。"""
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM trace_runs")
        return int(cursor.rowcount)
