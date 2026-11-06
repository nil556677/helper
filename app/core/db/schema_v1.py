"""全新 v1 schema（净室重写版）。

与旧 schema（v20）的关系：**不兼容、不迁移**。旧库会被 `scripts/reset_workspace.py`
显式重置；这里只提供"从零建一个 v1 库"与"读版本号"两个能力。

设计要点（见 docs/LangChain-LangGraph改造方案-v2.md §3）：

- SQLite 是事实来源：文档、parent/child 全文、会话、消息、trace 都在这里；
- Qdrant 只存 child 的 dense+sparse 向量与最小 payload，**不再有 parent 集合/alias**；
- `chunks`：parent/child 同表，`parent_id` 回填；`version + status` 支持重建原子切换；
- `jobs`/`job_events`：沿用旧任务队列的形状（队列本身就是表），`error_kind` 并入建表语句；
- `llm_slots`：只存 DPAPI 密文与尾号；
- `trace_runs/spans/hits/events`：检索追踪落库。

**建表注释约定（必须遵守）**：
- 每个表：表名前一行写 `-- 表 <表名>：<一句话中文说明>`；
- 每个字段：行尾用 `-- <中文说明>` 注释；
- **状态/枚举字段必须逐值写中文注释**（如 `pending 待处理 / indexed 已索引 / failed 失败`），
  不能只写代码不带含义；
- 索引同样加注释。所有注释一律中文。
"""

from __future__ import annotations

import sqlite3

#: 当前 schema 版本（净室基线，不做迁移链）。
SCHEMA_VERSION = 1

#: 建表语句（幂等：全用 IF NOT EXISTS）。
DDL_SQL = """
-- 表 schema_meta：整库的元信息（键值对；schema 版本号就存在这里）。
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,        -- 键名（如 schema_version）
    value TEXT NOT NULL            -- 键值
);

-- 表 settings：应用设置（键值对；值统一存 JSON 字符串）。
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,   -- 设置键名
    value_json TEXT NOT NULL,      -- 设置值（JSON 字符串）
    updated_at REAL NOT NULL       -- 更新时间戳（秒）
);

-- 表 llm_slots：大模型槽位配置（只存 DPAPI 密文与密钥尾号，不存明文）。
CREATE TABLE IF NOT EXISTS llm_slots (
    slot           TEXT PRIMARY KEY,  -- 槽位键（如 llm / llm-rewrite-v1）
    provider       TEXT    NOT NULL DEFAULT 'openai_compatible',  -- 提供方类型（默认 OpenAI 兼容接口）
    base_url       TEXT    NOT NULL DEFAULT '',  -- 兼容接口地址
    model          TEXT    NOT NULL DEFAULT '',  -- 模型名
    temperature    REAL    NOT NULL DEFAULT 0.2,  -- 采样温度
    timeout_s      REAL    NOT NULL DEFAULT 60,   -- 单次请求超时（秒）
    max_tokens     INTEGER NOT NULL DEFAULT 2048, -- 最大输出 token 数
    api_key_cipher BLOB,               -- DPAPI 加密后的密钥（密文；不存明文）
    key_tail       TEXT,               -- 密钥尾号（只用于展示，不能反推密钥）
    updated_at     REAL    NOT NULL    -- 更新时间戳（秒）
);

-- 表 documents：资料/文档主表（一份文件一行；同文件多次导入用 version 区分）。
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    name        TEXT    NOT NULL,                   -- 原文件名（展示用）
    source_path TEXT    NOT NULL DEFAULT '',        -- 原始位置（可为空）
    stored_path TEXT    NOT NULL,                   -- data/files/ 下副本的相对路径
    file_hash   TEXT    NOT NULL,                   -- 文件内容哈希（唯一判重）
    text_hash   TEXT    NOT NULL DEFAULT '',        -- 解析后文本哈希（内容判重）
    size        INTEGER NOT NULL DEFAULT 0,         -- 文件字节大小
    doc_title   TEXT    NOT NULL DEFAULT '',        -- 解析出的文档标题
    pages       INTEGER,                            -- 页数（可空）
    version     INTEGER NOT NULL DEFAULT 1,         -- 内容版本（同文件重导 +1；旧版仍服务）
    status      TEXT    NOT NULL DEFAULT 'pending', -- 状态（pending 待处理 / indexing 索引中 / indexed 已索引 / failed 失败）
    error_kind  TEXT    NOT NULL DEFAULT '',        -- 失败类型（解析失败分类）
    error       TEXT    NOT NULL DEFAULT '',        -- 失败原文（保留排查线索）
    created_at  REAL    NOT NULL,                   -- 创建时间戳（秒）
    updated_at  REAL    NOT NULL                    -- 最后更新时间戳（秒）
);
-- 索引：按文件哈希唯一（去重导入）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_file_hash ON documents(file_hash);
-- 索引：按状态查（列表/清理用）。
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

-- 表 chunks：片段（parent/child 同表）。child 是检索单位，parent 是摘要/整行父块。
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,  -- 所属资料（删资料级联删片段）
    version     INTEGER NOT NULL DEFAULT 1,         -- 所属内容版本
    parent_id   INTEGER REFERENCES chunks(id) ON DELETE CASCADE,  -- 父块 id（child 指向其摘要父块；可空=是父块）
    is_parent   INTEGER NOT NULL DEFAULT 0,         -- 是否父块（0 child / 1 parent）
    seq         INTEGER NOT NULL DEFAULT 0,         -- 在资料内按解析顺序的序号（与版本定唯一）
    kind        TEXT    NOT NULL DEFAULT 'text',    -- 内容类型（text 文本 / table 表格 / code 代码）
    status      TEXT    NOT NULL DEFAULT 'pending', -- 状态（pending 待用 / active 生效 / superseded 已被新版替代）
    text        TEXT    NOT NULL,                   -- 片段全文
    heading     TEXT    NOT NULL DEFAULT '',        -- 所属标题（检索可带）
    section_json TEXT   NOT NULL DEFAULT '[]',      -- 章节路径（JSON 数组）
    loc_json    TEXT    NOT NULL DEFAULT '{}',      -- 定位信息（页/行/列等，JSON）
    token_count INTEGER NOT NULL DEFAULT 0,         -- token 数（切分/预算用）
    hard_split  INTEGER NOT NULL DEFAULT 0,         -- 是否硬切（0 否 / 1 是，句子被打断）
    role        TEXT    NOT NULL DEFAULT 'text',    -- 结构角色（text 文本 / section_parent 章节父块 / table_parent 表格父块 / table_row 行 / table_row_segment 行内段）
    fetch_mode  INTEGER NOT NULL DEFAULT 1,         -- 回填模式（0 自己 / 1 父块 / 2 父块+兄弟）
    table_uid   TEXT,                               -- 所属表格的唯一号（表结构连贯性）
    row_uid     TEXT,                               -- 所属行的唯一号
    row_no      INTEGER,                            -- 表格行号（从表头后第一行起）
    segment_index INTEGER,                          -- 行内段序号（0 起）
    segment_count INTEGER NOT NULL DEFAULT 1,       -- 行内段总数
    merged_json TEXT    NOT NULL DEFAULT '[]',      -- 合并的相邻片（JSON 数组）
    created_at  REAL    NOT NULL                    -- 创建时间戳（秒）
);
-- 索引：child 在 (doc,version) 内按 seq 唯一（一个片段一个座位）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_child_unique ON chunks(doc_id, version, seq)
    WHERE is_parent = 0;
-- 索引：按父块查它的 child（回填）。
CREATE INDEX IF NOT EXISTS idx_chunks_doc_parent ON chunks(doc_id, version, is_parent);
-- 索引：parent_id 反查。
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
-- 索引：按状态查（重建切换用）。
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);
-- 索引：表结构行内段的连贯性。
CREATE INDEX IF NOT EXISTS idx_chunks_row ON chunks(doc_id, version, table_uid, row_uid, segment_index);
-- 索引：按结构角色查。
CREATE INDEX IF NOT EXISTS idx_chunks_role ON chunks(doc_id, version, role);
-- 索引：表格父块唯一（一个表格只认一个父块）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_table_parent_unique
    ON chunks(doc_id, version, table_uid)
    WHERE is_parent = 1 AND role = 'table_parent';
-- 索引：行内段唯一（行内段位置唯一）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_segment_unique
    ON chunks(doc_id, version, table_uid, row_uid, segment_index)
    WHERE role = 'table_row_segment';

-- 表 sparse_terms：BM25 词项表（词 ↔ uint32 id；命中次数用 df）。
CREATE TABLE IF NOT EXISTS sparse_terms (
    term       TEXT PRIMARY KEY,        -- 词项（原词）
    term_id    INTEGER NOT NULL UNIQUE, -- 词项对应的 uint32 id（稀疏向量坐标）
    df         INTEGER NOT NULL DEFAULT 0, -- 文档频率（出现在几篇文档）
    updated_at REAL    NOT NULL         -- 更新时间戳（秒）
);

-- 表 sparse_meta：BM25 的词表级元信息（键值对：N / total_len / avgdl 等）。
CREATE TABLE IF NOT EXISTS sparse_meta (
    key        TEXT PRIMARY KEY,   -- 键名
    value      TEXT NOT NULL,      -- 键值
    updated_at REAL NOT NULL       -- 更新时间戳（秒）
);

-- 表 conversations：会话（一列一排）。
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,   -- 会话 id（uuid）
    title      TEXT NOT NULL DEFAULT '',  -- 会话标题（按第一句问题命名）
    created_at REAL NOT NULL,      -- 创建时间戳（秒）
    updated_at REAL NOT NULL       -- 最后更新时间戳（秒）
);

-- 表 messages：会话下的消息（按 seq 排序）。
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,  -- 所属会话（删会话级联删消息）
    seq             INTEGER NOT NULL,                   -- 会话内序号（唯一排序）
    role            TEXT    NOT NULL,                   -- 角色（user 用户 / assistant 助手）
    channel         TEXT    NOT NULL DEFAULT 'chat',    -- 轨迹类别（chat 对话消息 / tool 工具轨迹消息）
    turn_id         TEXT,                               -- 逻辑轮次 id（= 该轮 user 消息 id；整轮配对/幂等/按轮加载）
    run_id          TEXT,                               -- 运行 id（发送即落库时，user 消息标记进行中 run；finalize 补 assistant）
    tool_call_id    TEXT,                               -- 工具结果消息对应哪次调用（trajectory 轨迹）
    name            TEXT,                               -- 工具名（tool 消息）
    status          TEXT    NOT NULL DEFAULT 'ok',      -- 轨迹消息状态（ok 成功 / error 失败）
    payload_json    TEXT,                               -- assistant 的 tool_calls 等结构化附加
    content         TEXT    NOT NULL,                   -- 消息文本
    turn_json       TEXT,                               -- assistant 的完整 Turn（process/fragments/note，JSON）
    created_at      REAL    NOT NULL                    -- 创建时间戳（秒）
);
-- 索引：同一会话内 seq 唯一（顺序写入）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conv_seq ON messages(conversation_id, seq);
-- 索引：按轮次取整轮（load last N 轮的加载路径）。
CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(conversation_id, turn_id);
-- 索引：按轨迹类别过滤（展示只看 chat / 审计看 tool）。
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(conversation_id, channel);

-- 表 turn_commits：一轮问答只提交一次的幂等标记（键为会话 + 轮次；防止恢复重放重复写）。
CREATE TABLE IF NOT EXISTS turn_commits (
    conversation_id TEXT NOT NULL,   -- 会话
    turn_id         TEXT NOT NULL,   -- 轮次 id
    created_at      REAL NOT NULL,   -- 提交时间戳（秒）
    PRIMARY KEY (conversation_id, turn_id)
);

-- 表 conversation_summaries：会话压缩摘要（滚动式，每会话一条；原文仍在 messages，可回放）。
CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id  TEXT PRIMARY KEY,  -- 会话 id
    until_message_id INTEGER NOT NULL,  -- 该摘要覆盖到的最后一条消息 id
    summary          TEXT NOT NULL,     -- 摘要正文（<compacted-summary> 之外的核心文本）
    created_at       REAL NOT NULL      -- 创建时间戳（秒）
);

-- 表 index_meta：索引指纹/元信息（键值对；向量指纹、词法指纹都存这里）。
CREATE TABLE IF NOT EXISTS index_meta (
    key        TEXT PRIMARY KEY,   -- 键名
    value      TEXT NOT NULL,      -- 键值
    updated_at REAL NOT NULL       -- 更新时间戳（秒）
);

-- 表 token_dict：分词词库（用户全局自定义词，影响 jieba 分词）。
CREATE TABLE IF NOT EXISTS token_dict (
    term       TEXT PRIMARY KEY,   -- 词项
    freq       INTEGER NOT NULL DEFAULT 1,   -- 词频
    enabled    INTEGER NOT NULL DEFAULT 1,   -- 是否启用（1 启用 / 0 停用）
    updated_at REAL    NOT NULL    -- 更新时间戳（秒）
);

-- 表 jobs：任务队列（队列本身就是表；认领/租约/取消都靠字段）。
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    kind             TEXT    NOT NULL,                   -- 任务类型（对应任务注册表的 key）
    payload          TEXT    NOT NULL DEFAULT '{}',      -- 任务参数（JSON）
    payload_version  INTEGER NOT NULL DEFAULT 1,         -- 参数结构版本
    dedupe_key       TEXT,                               -- 去重键（幂等任务用；空=不去重）
    status           TEXT    NOT NULL DEFAULT 'pending', -- 状态（pending 待执行 / running 执行中 / waiting 等待重试 / done 完成 / failed 失败 / cancelled 取消）
    priority         INTEGER NOT NULL DEFAULT 0,         -- 优先级（越大越先）
    progress         REAL    NOT NULL DEFAULT 0,         -- 进度 0..1
    done             INTEGER NOT NULL DEFAULT 0,         -- 已完成单元数
    total            INTEGER NOT NULL DEFAULT 0,         -- 总单元数
    note             TEXT    NOT NULL DEFAULT '',        -- 进度说明
    runs             INTEGER NOT NULL DEFAULT 0,         -- 已执行次数
    fails            INTEGER NOT NULL DEFAULT 0,         -- 已失败次数（未达上限会重试）
    max_attempts     INTEGER NOT NULL DEFAULT 3,         -- 最大尝试次数
    cursor           TEXT,                               -- 断点游标（崩溃后从该处续）
    resource         TEXT,                               -- 排他资源（同资源串行）
    run_id           TEXT,                               -- 关联的 trace 运行 id（排障）
    cancel_requested INTEGER NOT NULL DEFAULT 0,         -- 是否请求取消（1 是 / 0 否）
    next_run_at      REAL    NOT NULL,                   -- 下次可执行时间（退避用）
    lease_until      REAL,                               -- 租约到期时间（认领后保活）
    worker           TEXT,                               -- 认领它的 worker 名
    error            TEXT,                               -- 失败原文
    error_kind       TEXT,                               -- 失败类型（分类，用于不可重试等）
    created_at       REAL    NOT NULL,                   -- 创建时间戳（秒）
    started_at       REAL,                               -- 开始执行时间戳（秒）
    finished_at      REAL,                               -- 结束时间戳（秒）
    updated_at       REAL    NOT NULL                    -- 最后更新时间戳（秒）
);
-- 索引：认领扫描（状态+优先级+可执行时间）。
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority DESC, next_run_at);
-- 索引：幂等去重（仅待执行/执行中的去重键唯一）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(kind, dedupe_key)
    WHERE status IN ('pending','running') AND dedupe_key IS NOT NULL;
-- 索引：按更新时间查（列表/清理）。
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
-- 索引：按 trace run 反查任务。
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);

-- 表 job_events：任务时间线（进度之外的结构化事件/诊断/告警）。
CREATE TABLE IF NOT EXISTS job_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,  -- 所属任务（删任务级联删事件）
    at      REAL    NOT NULL,                   -- 发生时间戳（秒）
    attempt INTEGER NOT NULL DEFAULT 0,         -- 第几次尝试（0 = 入队前）
    kind    TEXT    NOT NULL DEFAULT 'unknown', -- 事件类型（如 hard_cut / skip / warn）
    message TEXT    NOT NULL DEFAULT '',        -- 事件消息
    detail  TEXT    NOT NULL DEFAULT ''         -- 事件详情（JSON）
);
-- 索引：按任务查事件时间线。
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);

-- 表 trace_runs：检索追踪主表（一次提问/检索 = 一行）。
CREATE TABLE IF NOT EXISTS trace_runs (
    id               TEXT PRIMARY KEY,  -- 运行 id（uuid）
    parent_run_id    TEXT,              -- 父运行 id（子代理/子任务时非空）
    conversation_id  TEXT,              -- 所属会话 id
    kind             TEXT NOT NULL DEFAULT 'chat',  -- 运行类型（chat 聊天 / tool 工具 / task 任务）
    status           TEXT NOT NULL DEFAULT 'running',  -- 状态（running 运行中 / ok 成功 / error 失败 / waiting 等待 / cancelled 已取消）
    question         TEXT NOT NULL,     -- 用户问题
    normalized_query TEXT NOT NULL DEFAULT '',  -- 规范化后的问题
    rewritten_query  TEXT NOT NULL DEFAULT '',  -- 改写后的问题（空=未改写）
    route_json       TEXT NOT NULL DEFAULT '{}',  -- 路由信息（模式/原因，JSON）
    answer           TEXT NOT NULL DEFAULT '',  -- 生成/提取的答案
    model            TEXT NOT NULL DEFAULT '',  -- 模型槽位
    started_at       REAL NOT NULL,     -- 开始时间戳（秒）
    finished_at      REAL,              -- 结束时间戳（秒）
    duration_ms      INTEGER,           -- 总耗时（毫秒）
    ttft_ms          INTEGER,           -- 首 token 耗时（毫秒）
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,  -- 输入 token 数
    completion_tokens INTEGER NOT NULL DEFAULT 0, -- 输出 token 数
    total_tokens     INTEGER NOT NULL DEFAULT 0,  -- 总 token 数
    cache_hit        INTEGER NOT NULL DEFAULT 0,  -- 是否缓存命中（1 是 / 0 否）
    error_kind       TEXT NOT NULL DEFAULT '',  -- 失败类型
    error_detail     TEXT NOT NULL DEFAULT '',  -- 失败详情
    metadata_json    TEXT NOT NULL DEFAULT '{}'  -- 附加元信息（JSON）
);
-- 索引：按开始时间倒序（最近优先）。
CREATE INDEX IF NOT EXISTS idx_trace_runs_started ON trace_runs(started_at DESC);
-- 索引：按状态+开始时间（筛选）。
CREATE INDEX IF NOT EXISTS idx_trace_runs_status ON trace_runs(status, started_at DESC);
-- 索引：按会话查（会话历史）。
CREATE INDEX IF NOT EXISTS idx_trace_runs_conv ON trace_runs(conversation_id, started_at DESC);

-- 表 trace_spans：追踪的时间线段（一个节点/一次 LLM 调用/一次检索）。
CREATE TABLE IF NOT EXISTS trace_spans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    run_id        TEXT NOT NULL REFERENCES trace_runs(id) ON DELETE CASCADE,  -- 所属运行（删运行级联删线段）
    parent_span_id INTEGER,                        -- 父线段 id（嵌套调用）
    name          TEXT NOT NULL,                   -- 线段名（如 load_context / rag.dense）
    kind          TEXT NOT NULL,                   -- 类型（node 节点 / llm 模型 / retrieval 检索 / tool 工具 / db 数据库）
    status        TEXT NOT NULL DEFAULT 'running', -- 状态（running 运行中 / ok 成功 / error 失败 / skipped 跳过 / timeout 超时）
    started_at    REAL NOT NULL,                   -- 开始时间戳（秒）
    duration_ms   INTEGER,                         -- 耗时（毫秒）
    input_json    TEXT,                            -- 输入摘要（JSON）
    output_json   TEXT,                            -- 输出摘要（JSON）
    error_json    TEXT                             -- 错误信息（JSON）
);
-- 索引：按运行查时间线。
CREATE INDEX IF NOT EXISTS idx_trace_spans_run ON trace_spans(run_id, started_at);

-- 表 trace_hits：一次召回里的一条命中。
CREATE TABLE IF NOT EXISTS trace_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    run_id      TEXT NOT NULL REFERENCES trace_runs(id) ON DELETE CASCADE,  -- 所属运行
    span_id     INTEGER,                        -- 关联线段 id
    stage       TEXT NOT NULL,                  -- 命中阶段（dense 语义 / sparse 词法 / fused 融合 / rerank 精排 / selected 入选）
    rank        INTEGER NOT NULL,               -- 名次（1 起）
    chunk_id    INTEGER,                        -- 命中的片段 id
    chunk_key   TEXT,                           -- 片段 key（doc:version:seq）
    doc_id      INTEGER,                        -- 所属资料 id
    source_name TEXT NOT NULL DEFAULT '',       -- 来源名（展示用）
    heading     TEXT NOT NULL DEFAULT '',       -- 所属标题
    loc_json    TEXT NOT NULL DEFAULT '{}',     -- 定位信息（页/行/列，JSON）
    score       REAL,                           -- 分数
    score_kind  TEXT NOT NULL DEFAULT '',       -- 分数类型（rrf / bm25 / cos / rerank 等）
    selected    INTEGER NOT NULL DEFAULT 0,     -- 是否最终入选（1 是 / 0 否）
    text        TEXT NOT NULL DEFAULT '',       -- 命中的原文
    snippet     TEXT NOT NULL DEFAULT ''        -- 命中片段摘要
);
-- 索引：按运行的阶段+名次查（时间线里的命中列表）。
CREATE INDEX IF NOT EXISTS idx_trace_hits_run ON trace_hits(run_id, stage, rank);
-- 索引：按片段反查（看某片段被哪些 run 命中）。
CREATE INDEX IF NOT EXISTS idx_trace_hits_chunk ON trace_hits(chunk_key);

-- 表 trace_events：追踪的结构化事件（超时/降级/缓存/告警等）。
CREATE TABLE IF NOT EXISTS trace_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    run_id      TEXT REFERENCES trace_runs(id) ON DELETE CASCADE,  -- 所属运行
    at          REAL NOT NULL,                   -- 发生时间戳（秒）
    level       TEXT NOT NULL DEFAULT 'info',    -- 级别（info 提示 / warning 告警 / error 错误）
    code        TEXT NOT NULL DEFAULT '',        -- 事件码（如 timeout / degraded）
    message     TEXT NOT NULL,                   -- 事件消息
    detail_json TEXT                             -- 事件详情（JSON）
);
-- 索引：按运行查事件时间线。
CREATE INDEX IF NOT EXISTS idx_trace_events_run ON trace_events(run_id, at);
"""


def read_version(conn: sqlite3.Connection) -> int:
    """读 ``schema_meta.schema_version``；库不存在/没记录返回 0。"""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return 0


def _ensure_chunks_columns(conn: sqlite3.Connection) -> None:
    """给旧 v1 库补上关系字段（幂等；当前数据可为空）。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    additions = {
        "role": "TEXT NOT NULL DEFAULT 'text'",
        "fetch_mode": "INTEGER NOT NULL DEFAULT 1",
        "table_uid": "TEXT",
        "row_uid": "TEXT",
        "row_no": "INTEGER",
        "segment_index": "INTEGER",
        "segment_count": "INTEGER NOT NULL DEFAULT 1",
        "merged_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}")
    conn.executescript(
        "CREATE INDEX IF NOT EXISTS idx_chunks_row"
        " ON chunks(doc_id, version, table_uid, row_uid, segment_index);"
        "CREATE INDEX IF NOT EXISTS idx_chunks_role"
        " ON chunks(doc_id, version, role);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_table_parent_unique"
        " ON chunks(doc_id, version, table_uid)"
        " WHERE is_parent = 1 AND role = 'table_parent';"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_segment_unique"
        " ON chunks(doc_id, version, table_uid, row_uid, segment_index)"
        " WHERE role = 'table_row_segment';"
    )


def _ensure_summaries_table(conn: sqlite3.Connection) -> None:
    """给旧 v1 库幂等补摘要表（新表，不影响既有数据）。"""
    conn.executescript(  # 幂等建表
        # **注释行结尾必须有 \n**：SQLite 的 -- 注释吃到行尾，拼在一起会把 CREATE 一起吞掉
        # （线上踩过：静默不建表，直到运行期才报 no such table）。
        "-- 表 conversation_summaries：会话压缩摘要（滚动式，每会话一条）。\n"
        "CREATE TABLE IF NOT EXISTS conversation_summaries ("
        " conversation_id TEXT PRIMARY KEY,"
        " until_message_id INTEGER NOT NULL,"
        " summary TEXT NOT NULL,"
        " created_at REAL NOT NULL"
        ");"
    )


def _ensure_messages_columns(conn: sqlite3.Connection) -> None:
    """给 messages 表补消息模型的字段（附录 A.2，additive，幂等）。

    字段/索引的由来：
    - ``turn_id``：逻辑轮次 id（= 该轮 user 消息 id），配对/幂等、加载按轮取整轮；
    - ``channel``：对话消息 / 工具轨迹消息（chat 对话 / tool 轨迹）的区分；
    - ``tool_call_id`` / ``name``：工具结果消息对应哪次调用、哪个工具；
    - ``status``：轨迹消息状态（ok / error / pruned）；
    - ``payload_json``：assistant 消息的 tool_calls 等结构化附加。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    additions = {
        "turn_id": "TEXT",
        "run_id": "TEXT",
        "channel": "TEXT NOT NULL DEFAULT 'chat'",
        "tool_call_id": "TEXT",
        "name": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'ok'",
        "payload_json": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
    conn.executescript(  # 幂等索引
        "CREATE INDEX IF NOT EXISTS idx_messages_turn"
        " ON messages(conversation_id, turn_id);"
        "CREATE INDEX IF NOT EXISTS idx_messages_run"
        " ON messages(conversation_id, run_id);"
        "CREATE INDEX IF NOT EXISTS idx_messages_channel"
        " ON messages(conversation_id, channel);"
    )


def _ensure_turn_commits_table(conn: sqlite3.Connection) -> None:
    """幂等补轮次幂等表：防止恢复重放把同一轮重复写（附录 A.2 / §5.4）。"""
    conn.executescript(  # 幂等建表
        # **注释行结尾必须有 \n**：否则 -- 注释会把后面的 CREATE TABLE 一起吞掉，
        # 表现为"建表函数不报错、表却不存在"（线上踩过：persist 报 no such table: turn_commits）。
        "-- 表 turn_commits：一轮问答只提交一次的幂等标记（键为会话 + 轮次）。\n"
        "CREATE TABLE IF NOT EXISTS turn_commits ("
        " conversation_id TEXT NOT NULL,"
        " turn_id TEXT NOT NULL,"
        " created_at REAL NOT NULL,"
        " PRIMARY KEY (conversation_id, turn_id)"
        ");"
    )


def _ensure_audit_tables(conn: sqlite3.Connection) -> None:
    """幂等补审计/成本表（append-only 决策链 + LLM 成本台账）。"""
    conn.executescript(  # 幂等建表
        # 注释行结尾必须有 \n：否则 -- 注释会吞掉后面的 CREATE TABLE。
        "-- 表 run_audit：运行决策链（append-only，不可改/删）。\n"
        "CREATE TABLE IF NOT EXISTS run_audit ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"  # 主键
        " run_id TEXT NOT NULL,"  # 运行 id
        " seq INTEGER NOT NULL,"  # run 内序号
        " node TEXT NOT NULL,"  # 节点
        " action TEXT NOT NULL,"  # 动作
        " decision TEXT NOT NULL DEFAULT '',"  # 决策摘要
        " payload TEXT NOT NULL DEFAULT '{}',"  # 载荷（JSON）
        " audit_hash TEXT NOT NULL DEFAULT '',"  # 动作哈希（防篡改核对）
        " created_at REAL NOT NULL"  # 时间戳
        ");\n"
        "CREATE INDEX IF NOT EXISTS idx_run_audit_run ON run_audit(run_id, seq);\n"
        "-- 表 cost_ledger：LLM 成本台账（每次调用一行）。\n"
        "CREATE TABLE IF NOT EXISTS cost_ledger ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"  # 主键
        " run_id TEXT NOT NULL,"  # 运行 id
        " model TEXT NOT NULL DEFAULT '',"  # 模型
        " tokens_in INTEGER NOT NULL DEFAULT 0,"  # 输入 token
        " tokens_out INTEGER NOT NULL DEFAULT 0,"  # 输出 token
        " cost REAL NOT NULL DEFAULT 0,"  # 估算成本
        " created_at REAL NOT NULL"  # 时间戳
        ");\n"
        "CREATE INDEX IF NOT EXISTS idx_cost_ledger_run ON cost_ledger(run_id);\n"
    )


def _ensure_conversation_tombstones(conn: sqlite3.Connection) -> None:
    """幂等补"会话墓碑"表：删掉的会话不许被进行中的 run 重建（防复活）。"""
    conn.executescript(  # 幂等建表
        "-- 表 conversation_tombstones：已删会话的墓碑（禁止 ensure 重建）。\n"
        "CREATE TABLE IF NOT EXISTS conversation_tombstones ("
        " id TEXT PRIMARY KEY,"  # 会话 id
        " deleted_at REAL NOT NULL"  # 删除时间戳
        ");\n"
    )


def initialize(conn: sqlite3.Connection) -> int:
    """把一条连接初始化成 v1（幂等）。

    返回：初始化的版本号。
    异常：
        ValueError: 连接上已有**其它版本**的 schema（净室规则：不迁移，拒绝混合）。
    """
    conn.row_factory = sqlite3.Row
    current = read_version(conn)
    if current == SCHEMA_VERSION:
        _ensure_chunks_columns(conn)
        _ensure_summaries_table(conn)
        _ensure_messages_columns(conn)
        _ensure_turn_commits_table(conn)
        _ensure_audit_tables(conn)
        _ensure_conversation_tombstones(conn)
        return current
    if current != 0:
        raise ValueError(
            f"数据库 schema 版本是 v{current}，与净室 v{SCHEMA_VERSION} 不兼容；"
            "请先运行 scripts/reset_workspace.py 重置。"
        )
    conn.executescript(DDL_SQL)
    _ensure_audit_tables(conn)
    _ensure_conversation_tombstones(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return SCHEMA_VERSION
