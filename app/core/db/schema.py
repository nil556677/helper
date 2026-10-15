"""SQLite 表结构与迁移（**schema v2**）。

设计取舍写在代码里，避免后来者"顺手换个方案"：

- **一个库文件装全部数据**：资料、片段、会话、设置、模型槽位、任务都在一起。备份 = 复制一个文件。
- **原文与索引文本分列**：``chunks.text`` 是原文（给人看、给模型用），``chunks.tokens`` 是
  jieba 切好并用空格连起来的**索引文本**（给 FTS5 用）。两者必须同时写、同时更新，
  **写入口只能有一个函数**，否则一定会出现"改了正文忘了改索引"。
- **FTS5 用外部内容表 + 触发器**：因为 ``tokens`` 已经是切好的文本，触发器只需搬运，
  **不需要分词能力**——这是"预分词列"能安全用触发器的前提。正文只存一份。
- **``llm_slots.api_key_cipher`` 只存密文**（Windows DPAPI），明文 key 绝不入库；
  ``key_tail`` 只给界面显示尾号，不参与解密。
- **失败只记错误码**：``documents.error_code`` 是机器判据（中文文案会改，子串匹配会静默失效），
  ``detail`` 是给人看的说明（写库前要过路径脱敏）。
- **``jobs``（v2 新增）**：任务队列自己就是一张表（见技术方案 §13）。要点——``runs``（认领次数）
  与 ``fails``（业务失败次数）**分开**（崩溃不该耗掉重试机会）；``cursor`` 支持长任务断点续跑；
  ``dedupe_key`` 用**部分唯一索引**保证"活跃期不重复排队、历史仍留得住"；认领索引必须带
  ``priority DESC``，否则 SQLite 会为整个 pending 队列建临时 B 树（实测：队列 10000 条时
  认领从 1.02ms 变成 2.14ms）。迁移脚本见 :data:`MIGRATIONS`。
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.db.connection import get_connection

#: 当前 schema 版本。改表结构就 +1，并在 MIGRATIONS 里补一条。
SCHEMA_VERSION = 3

#: 建表语句（幂等：全用 IF NOT EXISTS，重复执行是安全的）。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 一份资料（导入进来的文件）
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT    NOT NULL,                    -- 原文件名，展示用
    stored_path TEXT    NOT NULL,                    -- data/files/ 下副本的相对路径
    file_hash   TEXT,                                -- 文件字节 sha256：判重第一级
    text_hash   TEXT,                                -- 正文指纹（去空白后 sha256）：判重第二级
    size        INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending / indexing / ready / failed
    error_code  TEXT,                                -- 失败分类（机器判据）
    detail      TEXT,                                -- 失败说明（给人看，写库前脱敏）
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents(file_hash);
CREATE INDEX IF NOT EXISTS idx_documents_text_hash ON documents(text_hash);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

-- 片段：检索的最小单位
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,        -- 文档内序号，0 基
    text       TEXT    NOT NULL,        -- 原文
    tokens     TEXT    NOT NULL,        -- jieba 预分词后的索引文本（给 FTS5）
    page       INTEGER,                 -- 页码（有就记）
    section    TEXT,                    -- 章节/标题（有就记）
    created_at REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_doc_seq ON chunks(doc_id, seq);

-- 词法索引：外部内容表，索引的是 chunks.tokens
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    tokens,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61'
);

-- 触发器：tokens 已切好，这里只做搬运（删/改都要先用旧值发 'delete' 指令）
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, tokens) VALUES (new.id, new.tokens);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, tokens) VALUES ('delete', old.id, old.tokens);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, tokens) VALUES ('delete', old.id, old.tokens);
    INSERT INTO chunks_fts(rowid, tokens) VALUES (new.id, new.tokens);
END;

-- 大模型槽位（key 只存 DPAPI 密文）
-- 槽位取值**故意不写死约束**：现在用两个（推理 / OCR），以后加槽位只加一行数据，不动表结构
CREATE TABLE IF NOT EXISTS llm_slots (
    slot           TEXT PRIMARY KEY,                 -- 现在：reasoning / ocr
    provider       TEXT    NOT NULL DEFAULT 'openai_compatible',
    base_url       TEXT    NOT NULL DEFAULT '',
    model          TEXT    NOT NULL DEFAULT '',
    temperature    REAL    NOT NULL DEFAULT 0.2,
    timeout_s      REAL    NOT NULL DEFAULT 120,
    max_tokens     INTEGER NOT NULL DEFAULT 2048,
    api_key_cipher BLOB,                             -- 密文；明文绝不入库
    key_tail       TEXT,                             -- 尾号，仅界面显示
    updated_at     REAL    NOT NULL
);

-- 会话与消息
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,                -- 会话内序号（写事务里取 MAX+1）
    role            TEXT NOT NULL,                   -- user / assistant / system
    content         TEXT NOT NULL,
    citations_json  TEXT,
    created_at      REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conv_seq ON messages(conversation_id, seq);

-- 其余配置与界面偏好（引导项之外的一切，见技术方案 §12）
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 索引指纹：**分项**存（embed_model / chunk_params / userdict_hash…），
-- 才能区分"只重算词法列"与"必须全量重建"
CREATE TABLE IF NOT EXISTS index_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

#: v2：任务队列（技术方案 §13）。**写法与 v1 的表保持一致**（IF NOT EXISTS，幂等）。
JOBS_SQL = """
-- 任务队列：队列本身就是这张表（见技术方案 §13）
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT    NOT NULL,                 -- 任务类型（功能注册的 kind）
    payload          TEXT    NOT NULL DEFAULT '{}',    -- JSON：只放引用（路径 / id），绝不放 bytes
    payload_version  INTEGER NOT NULL DEFAULT 1,       -- payload 结构以后变了，靠它识别老任务
    dedupe_key       TEXT,                             -- 活跃期同一件事只排一次
    status           TEXT    NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/cancelled
    priority         INTEGER NOT NULL DEFAULT 0,       -- 10 用户在等 / 0 默认 / -10 维护
    progress         REAL    NOT NULL DEFAULT 0,
    done             INTEGER NOT NULL DEFAULT 0,
    total            INTEGER NOT NULL DEFAULT 0,
    note             TEXT    NOT NULL DEFAULT '',      -- 给用户看的一句话：「48/120 片段」
    runs             INTEGER NOT NULL DEFAULT 0,       -- 认领次数（崩溃统计）
    fails            INTEGER NOT NULL DEFAULT 0,       -- 业务失败次数（重试判定只看它）
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    cursor           TEXT,                             -- 断点续跑位置（长任务用）
    resource         TEXT,                             -- 独占资源名：同名资源同时只能有一个在跑
    cancel_requested INTEGER NOT NULL DEFAULT 0,       -- 取消请求：running 的任务在批次边界响应
    next_run_at      REAL    NOT NULL,                 -- 到点才允许被认领（退避就往后推）
    lease_until      REAL,                             -- 认领者的租约，过期=卡死
    worker           TEXT,                             -- 谁在跑（线程名/进程号，排障用）
    error            TEXT,                             -- 最后失败原因（人话，界面直接显示）
    created_at       REAL    NOT NULL,
    started_at       REAL,
    finished_at      REAL,
    updated_at       REAL    NOT NULL
);
-- 索引必须带 priority DESC：否则 ORDER BY priority DESC, next_run_at 用不上它，
-- SQLite 会为整个 pending 队列建临时 B 树（实测：10000 条时认领 1.02ms → 2.14ms）
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority DESC, next_run_at);
-- 活跃期不重复排队；跑完之后还可以再排一次（历史留得住）
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(kind, dedupe_key)
    WHERE status IN ('pending','running') AND dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
"""

#: v3：任务的**错误记录**（技术方案 §13.10）。
#: 为什么要单开一张事件表：`jobs.error` 只留得住"最后一次失败"，可重试的任务会失败好几次，
#: 而排查时要看的是**时间线**（哪几次失败、每次什么分类、有没有 traceback）。
ERROR_SQL = """
-- 失败分类：取 §10.1 的九类（config/auth/not_found/bad_request/rate_limit/server/timeout/network），
-- 外加 interrupted（关窗口/崩溃）、cancelled（用户取消）、unknown（没预料到）
ALTER TABLE jobs ADD COLUMN error_kind TEXT;

CREATE TABLE IF NOT EXISTS job_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at      REAL    NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,   -- 第几次尝试（0 = 与尝试无关的事件）
    kind    TEXT    NOT NULL DEFAULT 'unknown',
    message TEXT    NOT NULL DEFAULT '',  -- 给人看的一句话
    detail  TEXT    NOT NULL DEFAULT ''   -- 完整原因 / traceback（可能很长，界面折叠显示）
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);
"""

#: 迁移：``{目标版本: SQL}``。建库时从当前版本逐条往上跑。
#: v1 是初始版本（SCHEMA_SQL 就是它的形状），所以没有 v1 的迁移。
MIGRATIONS: dict[int, str] = {
    2: JOBS_SQL,
    3: ERROR_SQL,
}


def schema_version(conn: sqlite3.Connection) -> int:
    """读库里的 schema 版本。

    参数：
        conn: 已打开的连接。
    返回：版本号；全新的库返回 0。
    """
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        # 值被外部工具改坏了：当成 0 重新建表（建表本身是幂等的）。
        logger.warning("schema_meta.schema_version 不是整数，按 0 处理")
        return 0


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写一条 schema_meta（存在就覆盖）。"""
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


class SchemaTooNewError(RuntimeError):
    """库的结构版本比程序还新（例如用旧版程序打开了新版建的库）。"""


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str) -> None:
    """应用一条迁移：**整条脚本 + 版本号一起提交**（要么全成，要么全不成）。

    为什么不用裸 ``executescript``：它执行前会隐式提交，脚本里第二条 DDL 失败时会留下
    "版本没涨、表改了一半"的库，而重跑时因为版本对不上，那段脚本**再也不会被执行**。
    这里把脚本和版本号写包进一个显式事务。
    """
    logger.info("应用 schema 迁移 v{} → v{}", version - 1, version)
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{sql}\n"
        f"INSERT INTO schema_meta(key, value) VALUES ('schema_version', '{version}') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n"
        f"INSERT INTO schema_meta(key, value) VALUES ('migrated_to_{version}_at', '{time.time()}') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        with contextlib.suppress(sqlite3.Error):  # 允许失败：可能已经没有活动事务了
            conn.execute("ROLLBACK")
        raise RuntimeError(f"schema v{version} 迁移失败（已回滚）：{exc}") from exc


def init_schema(conn: sqlite3.Connection) -> int:
    """建表 / 升级到当前版本（幂等）。

    参数：
        conn: 已打开的连接（**不要**在 transaction() 里调用：executescript 会自己提交）。
    返回：执行后的 schema 版本。
    异常：
        sqlite3.Error: 建表失败（如库文件损坏）。
        SchemaTooNewError: 库的版本比程序还新（降级运行）——**拒绝启动**而不是继续写。
        RuntimeError: 缺少某条迁移脚本——**拒绝启动**，否则会造出"版本号涨了、表却没改"的库，
            那种库永远补不回来。
    """
    conn.executescript(SCHEMA_SQL)

    current = schema_version(conn)
    if current == 0:
        # 全新的库：SCHEMA_SQL 就是 v1 的形状，先记 v1，再往上跑迁移
        _set_meta(conn, "schema_version", "1")
        _set_meta(conn, "created_at", str(time.time()))
        current = 1

    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"库的结构版本是 v{current}，比这个程序（v{SCHEMA_VERSION}）还新；"
            "请用更新版本的 helper 打开它。"
        )

    for version in range(current + 1, SCHEMA_VERSION + 1):
        sql = MIGRATIONS.get(version)
        if sql is None:
            raise RuntimeError(
                f"缺少 schema v{version} 的迁移脚本（SCHEMA_VERSION={SCHEMA_VERSION}）："
                "补上 MIGRATIONS 再启动"
            )
        _apply_migration(conn, version, sql)

    return schema_version(conn)


def init_db(db_path: Path | None = None) -> int:
    """打开库并建表（启动路径的唯一入口）。

    参数：
        db_path: 库文件路径；默认 ``{数据目录}/helper.db``（测试传临时文件）。
    返回：schema 版本。
    """
    target = db_path or paths.db_file()
    conn = get_connection(target)
    version = init_schema(conn)
    logger.debug("数据库就绪：{}（schema v{}）", target, version)
    return version


def table_names(conn: sqlite3.Connection) -> set[str]:
    """列出库里的表名（自检与测试用）。"""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    return {row["name"] for row in rows}
