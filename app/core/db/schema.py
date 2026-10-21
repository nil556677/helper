"""SQLite 表结构与迁移（**schema v5**）。

设计取舍写在代码里，避免后来者"顺手换个方案"：

- **一个库文件装全部数据**：资料、片段、会话、设置、模型槽位、任务都在一起。备份 = 复制一个文件。
- **原文与索引文本分列**：``chunks.text`` 是原文（给人看、给模型用），``chunks.tokens`` 是
  jieba 切好并用空格连起来的**索引文本**（给 FTS5 用）。两者必须同时写、同时更新，
  **写入口只能有一个函数**，否则一定会出现"改了正文忘了改索引"。
- **FTS5 用外部内容表 + 触发器**：因为 ``tokens`` 已经是切好的文本，触发器只需搬运，
  **不需要分词能力**——这是"预分词列"能安全用触发器的前提。正文只存一份。
  ``codes_fts``（v5）是例外：它用 ``trigram`` 分词器，**必须**由 SQLite 自己切，才能做子串匹配。
- **``llm_slots.api_key_cipher`` 只存密文**（Windows DPAPI），明文 key 绝不入库；
  ``key_tail`` 只给界面显示尾号，不参与解密。
- **失败只记错误码**：``documents.error_code`` 是机器判据（中文文案会改，子串匹配会静默失效），
  ``detail`` 是给人看的说明（写库前要过路径脱敏）。
- **``jobs``（v2）**：任务队列自己就是一张表（见技术方案 §13）。要点——``runs``（认领次数）
  与 ``fails``（业务失败次数）**分开**（崩溃不该耗掉重试机会）；``cursor`` 支持长任务断点续跑；
  ``dedupe_key`` 用**部分唯一索引**保证"活跃期不重复排队、历史仍留得住"；认领索引必须带
  ``priority DESC``，否则 SQLite 会为整个 pending 队列建临时 B 树（实测：队列 10000 条时
  认领从 1.02ms 变成 2.14ms）。迁移脚本见 :data:`MIGRATIONS`。
- **``chunks`` 父子两层同表（v4）**：parent 是送给模型的证据，child 是检索的单位；父子回填靠
  ``parent_id``。``version`` + ``status`` 让"同一份资料的新版本"能先写后台、再原子切换
  （检索永远只看 ``status='active'``）。
- **向量不在这个库里做事实来源（v5）**：Qdrant 存向量，``matrices`` 只是**可重建的打分缓存**
  （整块 float32，读一次全载进内存算分）。主键带 ``model``/``dims``，换模型后新旧两份能并存，
  不会互相覆盖；``vector_keys`` 是 ``key → 矩阵行号`` 的对照表（幂等 upsert 靠它找位置）。
- **索引指纹复用 v1 就有的 ``index_meta``**（``key/value/updated_at`` 正好就是分项指纹的形状），
  v5 不再建第二张同义表。
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
SCHEMA_VERSION = 18

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

-- 索引指纹：**分项**存（embed_model / chunk_params / terms_hash…），
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

#: v4：RAG 第一半——文档版本链、解析信息、父子两层块（技术方案 §6 的 v5 块，拆两半好回滚）。
#: **为什么拆**：documents/chunks 的 ALTER 一旦跑下去就没法简单回退，而向量/精确查找那几张新表
#: 是纯新增；拆开之后"迁移失败时到底改了哪一半"是看得出来的。
RAG_DOCUMENTS_SQL = """
-- 一份文件的多个版本共享 doc_key；superseded_at 非空 = 已被新版本取代（延迟 6 小时才物理删）
ALTER TABLE documents ADD COLUMN doc_key TEXT;
ALTER TABLE documents ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN superseded_at REAL;
ALTER TABLE documents ADD COLUMN index_fingerprint TEXT;
-- 解析摘要：知识库列表要能说清"这份到底是什么内容"（老库回填 NULL，界面按"未知"显示）
ALTER TABLE documents ADD COLUMN pages INTEGER;
ALTER TABLE documents ADD COLUMN ocr_pages TEXT;
ALTER TABLE documents ADD COLUMN chars INTEGER;

-- 父子两层同表：parent 是给模型的证据，child 是检索的最小单位
ALTER TABLE chunks ADD COLUMN parent_id INTEGER REFERENCES chunks(id) ON DELETE CASCADE;
ALTER TABLE chunks ADD COLUMN is_parent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text';
ALTER TABLE chunks ADD COLUMN section_path TEXT;
ALTER TABLE chunks ADD COLUMN level INTEGER;
ALTER TABLE chunks ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE chunks ADD COLUMN text_hash TEXT;
ALTER TABLE chunks ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

-- 版本化 upsert 的锚点。**故意不建 UNIQUE**：老库的 chunks 只有 (doc_id, seq)，
-- version 一律 DEFAULT 1，与既有的 idx_chunks_doc_seq 是同一组值；这里再建一个 UNIQUE
-- 只是把同一条约束写两遍，却会在"老库里同一 doc 有重复 seq"时让整个迁移起不来。
CREATE INDEX IF NOT EXISTS idx_chunks_doc_ver_seq ON chunks(doc_id, version, seq);
-- 检索只找 active：先按状态掩码，再算分（技术方案 §9.1 第 8 条）
CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);
-- 按父块回填：命中 child 后要取它的 parent
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
"""

#: v5：RAG 第二半——向量打分缓存、精确查找、精排缓存（全部是新表，失败可直接重跑 v5）。
RAG_VECTOR_SQL = """
-- 精确查找：从正文里抽出来的编号/日期/金额/条款号（零 LLM，正则抽）
CREATE TABLE IF NOT EXISTS codes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    kind     TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    at       REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_codes_value ON codes(value);
CREATE INDEX IF NOT EXISTS idx_codes_chunk ON codes(chunk_id);

-- 子串查找（"只记得编号一半"）：trigram 分词器要 SQLite ≥3.34，启动自检会探它。
-- 用外部内容表跟着 codes 走，省一份存储；触发器由下面的 codes_* 三条维护。
CREATE VIRTUAL TABLE IF NOT EXISTS codes_fts USING fts5(
    value,
    content='codes',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS codes_ai AFTER INSERT ON codes BEGIN
    INSERT INTO codes_fts(rowid, value) VALUES (new.id, new.value);
END;
CREATE TRIGGER IF NOT EXISTS codes_ad AFTER DELETE ON codes BEGIN
    INSERT INTO codes_fts(codes_fts, rowid, value) VALUES ('delete', old.id, old.value);
END;
CREATE TRIGGER IF NOT EXISTS codes_au AFTER UPDATE ON codes BEGIN
    INSERT INTO codes_fts(codes_fts, rowid, value) VALUES ('delete', old.id, old.value);
    INSERT INTO codes_fts(rowid, value) VALUES (new.id, new.value);
END;

-- 向量：Qdrant 是事实来源，这里是**可重建的打分缓存**（整块 float32）。
-- 主键含 model/dims：同一个集合将来可能并存多种嵌入（换模型后重建期间两种都要能打分），
-- 只按 collection 做键会把新旧两份互相覆盖掉。
CREATE TABLE IF NOT EXISTS matrices (
    collection TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    dims       INTEGER NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    meta_json  TEXT,
    data       BLOB,
    updated_at REAL    NOT NULL,
    PRIMARY KEY (collection, model, dims)
);

-- key → 矩阵行号（幂等 upsert 要按它找位置；row 是**那个 model 的矩阵里**的行号）
CREATE TABLE IF NOT EXISTS vector_keys (
    collection TEXT    NOT NULL,
    model      TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    row        INTEGER NOT NULL,
    PRIMARY KEY (collection, model, key)
);

-- 精排缓存：同一 (query, chunk) 不重复算（精排一次 2 s，缓存命中 <50 ms）
CREATE TABLE IF NOT EXISTS rerank_cache (
    query_hash TEXT NOT NULL,
    chunk_id   INTEGER NOT NULL,
    score      REAL NOT NULL,
    at         REAL NOT NULL,
    PRIMARY KEY (query_hash, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_rerank_cache_at ON rerank_cache(at);
"""

#: v6：给键表补 payload（JSON），好让**矩阵打分那条路也能过滤**。
#: 为什么必须补：矩阵打分要比 Qdrant 查询快 10 倍以上，所以检索默认走矩阵；
#: 而"只找 status=active"这类过滤必须**在算分前生效**（技术方案 §9.1 第 8 条，
#: 实现上先算分再掩码）。掩码得知道每一行是什么状态——没有这一列就只能退化成
#: "先全算分、不过滤"，那会把已删/旧版本的块也排在前面。
#: 只存**过滤要用的少数几个字段**（不是整份 payload）：10 万行 × 小 JSON 才是可接受的开销。
VECTOR_PAYLOAD_SQL = """
ALTER TABLE vector_keys ADD COLUMN payload TEXT;
CREATE INDEX IF NOT EXISTS idx_vector_keys_collection ON vector_keys(collection, model);
"""

#: v7：给 chunks 补 ``loc_json``——**引用定位信息**（文件哪一页、哪一行、哪张表的行区间）。
#: 为什么单独一列 JSON 而不是分列：loc 只用于展示/跳转（"点引用 → 看原文位置"），
#: **不参与检索过滤**（检索过滤用 status/content_type/page 那些既有分列），
#: 所以整包存一个 JSON 列最省事，也避免给一张 10 万行的表再堆 7 个列。
#: 形状见 `app/core/parse/model.py` 的 ``Block.loc_json()``。
CHUNK_LOC_SQL = """
ALTER TABLE chunks ADD COLUMN loc_json TEXT;
"""

#: v8：给 ``vector_keys`` 加一个**能覆盖排序的索引**（``collection, model, row``）。
#: 为什么必须：读回键表/payload 要 ``ORDER BY row``（行号即位置，顺序不能乱）。
#: 没有这个索引时，10 万行排序会让 SQLite 去**临时目录**建临时 B 树——而临时目录默认是
#: 系统 TEMP；TEMP 不可写时报的是 ``unable to open database file``（SQLITE_CANTOPEN），
#: **看起来像库坏了，实际只是排序没地方落临时文件**（本机受限环境下必现，实测）。
#: 有了这个索引，这个查询变成索引顺序扫描，既不再依赖临时目录、也快。
VECTOR_KEYS_ORDER_SQL = """
CREATE INDEX IF NOT EXISTS idx_vector_keys_order ON vector_keys(collection, model, row);
"""

#: v9：同一份资料的版本号唯一（**部分索引**：``doc_key IS NULL`` 的老数据不参与，
#: SQLite 里 NULL 彼此不相等，所以这些行不受影响）。
#: 这是 P1.7 版本链的最后一道保险：并发同名导入若真算出同一个版本号，宁可**当场失败**，
#: 也不要悄悄出现"两条 v2"——那会让切换、回填、统计全部失去意义。
DOC_KEY_VERSION_UNIQUE_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_key_version
    ON documents(doc_key, version) WHERE doc_key IS NOT NULL;
"""

#: v11：移除进程内矩阵打分缓存。向量只存在 Qdrant（见 docs/向量层重构设计.md）。
#: ``matrices`` / ``vector_keys`` 都是**可重建的缓存**，删掉不丢数据；之后统计/对账改走 Qdrant。
MATRIX_DROP_SQL = """
DROP TABLE IF EXISTS vector_keys;
DROP TABLE IF EXISTS matrices;
"""

#: v12：片段/精确字段不再入 SQLite（config 3，见 docs/向量层重构设计.md）。
#: child/parent 正文与结构只存 Qdrant；词法用 sparse；精确字段进 attrs_flat。
#: documents 补 doc_title / index_text_hash；jobs 补 doc_id；rerank_cache 补 chunk_key。
VECTOR_ONLY_SQL = """
-- 先删触发器：它们引用被删的 FTS 表，先删表会让 DROP TABLE chunks 报 no such table。
DROP TRIGGER IF EXISTS chunks_ai;
DROP TRIGGER IF EXISTS chunks_ad;
DROP TRIGGER IF EXISTS chunks_au;
DROP TRIGGER IF EXISTS codes_ai;
DROP TRIGGER IF EXISTS codes_ad;
DROP TRIGGER IF EXISTS codes_au;

DROP TABLE IF EXISTS codes_fts;
DROP TABLE IF EXISTS codes;
DROP TABLE IF EXISTS chunks_fts;
DROP TABLE IF EXISTS chunks;

ALTER TABLE documents ADD COLUMN doc_title TEXT;
ALTER TABLE documents ADD COLUMN index_text_hash TEXT;

ALTER TABLE jobs ADD COLUMN doc_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(doc_id);

ALTER TABLE rerank_cache ADD COLUMN chunk_key TEXT;
CREATE INDEX IF NOT EXISTS idx_rerank_key ON rerank_cache(chunk_key);
"""

#: v10：给 ``codes``（v5 已建）**补上归属与原文**，并加唯一约束（P1.10 要用）。
#:
#: 为什么是"补列"不是"重建表"：v5 就把表、值索引、trigram 子串索引与三条触发器都建好了。
#: 重建会让老库里的数据凭空消失——而这次只需要 ``doc_id``/``version``（按资料查、版本链对齐）、
#: ``raw``（引用要展示原文）、``page``（引用跳转）。
#: 唯一索引用 ``NULLS distinct`` 的默认语义：老行 ``doc_id IS NULL`` 不会互相冲突。
CODES_SQL = """
ALTER TABLE codes ADD COLUMN doc_id INTEGER;
ALTER TABLE codes ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE codes ADD COLUMN raw TEXT;
ALTER TABLE codes ADD COLUMN page INTEGER;
CREATE INDEX IF NOT EXISTS idx_codes_doc ON codes(doc_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_codes_unique
    ON codes(doc_id, version, chunk_id, kind, value);
"""



#: v13：认领索引带上 ``kind``。
#: 原来的 ``idx_jobs_claim(status, priority, next_run_at)`` 不含 kind，而 `claim_next` 要按
#: 「本泳道的 kind」过滤——队列大时只能先扫一批 pending 再过滤。补上 kind 让过滤与排序
#: 都能走索引；再加一条 ``(status, kind)`` 给 `pending_of_unknown_kinds`（每 10 秒一次）。
JOBS_CLAIM_KIND_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_claim_kind
    ON jobs(status, kind, priority DESC, next_run_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status_kind ON jobs(status, kind);
"""


#: **迁移完的收尾清理**（每次启动都在 :func:`init_schema` 末尾跑，幂等）。
#:
#: 为什么需要：``SCHEMA_SQL`` 是 v1 的形状，每次启动都会 ``CREATE TABLE IF NOT EXISTS``；
#: 而 v11/v12 已经把这些表 DROP 了——于是"删掉又建回一个空表"，库里长期躺着幽灵
#: chunks/codes（还是缺列的旧结构），迁移意图被悄悄破坏。这里在"已到 v12+"的库上再删一次，
#: 保证最终状态与迁移声明一致。
DEPRECATED_TABLES_SQL = """
DROP TRIGGER IF EXISTS chunks_ai;
DROP TRIGGER IF EXISTS chunks_ad;
DROP TRIGGER IF EXISTS chunks_au;
DROP TRIGGER IF EXISTS codes_ai;
DROP TRIGGER IF EXISTS codes_ad;
DROP TRIGGER IF EXISTS codes_au;
DROP TABLE IF EXISTS codes_fts;
DROP TABLE IF EXISTS codes;
DROP TABLE IF EXISTS chunks_fts;
DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS matrices;
DROP TABLE IF EXISTS vector_keys;
"""


#: v14：pipeline 版本血缘 + 写入校验时间。
#: 为什么补列：换了解析器/切分策略/嵌入模型后，要能识别"这份资料是哪套 pipeline 产的"，
#: 从而决定是否需要重跑；``verified_at`` 记录"写入后校验通过"的时间（没校验过为 NULL）。
PIPELINE_VERSIONS_SQL = """
ALTER TABLE documents ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN chunk_policy INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN embedding_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN verified_at REAL;
"""


#: v15：批次回放（``run_id``）。
#: 一次批量导入/重建的所有任务共享一个 ``run_id``，失败后可整批按原参数重排。
RUN_ID_SQL = """
ALTER TABLE jobs ADD COLUMN run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id);
"""


#: v16：标题树落库（详情页"文档结构"用）。
#: 以前标题树只在流式过程里存在，导入结束就丢了；容器标题（只有下级、没有正文）
#: 因此既看不到也查不到。JSON 数组：[{"level","title","path","page"}, ...]。
STRUCTURE_JSON_SQL = """
ALTER TABLE documents ADD COLUMN structure_json TEXT;
"""


#: v17：撤销"单独存标题结构"。
#: 结构树（``structure_json``）不参与检索、只用于展示，而展示用每个片段的
#: ``heading``（完整标题链）就够；不再单独存一份文档级结构。
DROP_STRUCTURE_JSON_SQL = """
ALTER TABLE documents DROP COLUMN structure_json;
"""


#: v18：全局领域词（自定义关键词）**存库**，替代 `data/userdict.txt` 的手工维护。
#:
#: 为什么搬进库：单一事实来源、随库一起备份、可在设置页增删改，不必再依赖相对路径文件。
#: ``term`` 直接做主键（一行一词，天然去重）；``freq`` 为 NULL 时用 jieba 默认词频；
#: ``enabled=0`` 的词**保留但不参与切词**（停用比删掉更好回溯）。
TOKEN_DICT_SQL = """
CREATE TABLE IF NOT EXISTS token_dict (
    term       TEXT PRIMARY KEY,
    freq       INTEGER,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_dict_enabled ON token_dict(enabled);
"""


#: 迁移：``{目标版本: SQL}``。建库时从当前版本逐条往上跑。
#: v1 是初始版本（SCHEMA_SQL 就是它的形状），所以没有 v1 的迁移。
#: 注：`index_meta` 在 v1 就有了（key/value/updated_at），索引指纹直接复用它，不再建表。
MIGRATIONS: dict[int, str] = {
    2: JOBS_SQL,
    3: ERROR_SQL,
    4: RAG_DOCUMENTS_SQL,
    5: RAG_VECTOR_SQL,
    6: VECTOR_PAYLOAD_SQL,
    7: CHUNK_LOC_SQL,
    8: VECTOR_KEYS_ORDER_SQL,
    9: DOC_KEY_VERSION_UNIQUE_SQL,
    10: CODES_SQL,
    11: MATRIX_DROP_SQL,
    12: VECTOR_ONLY_SQL,
    13: JOBS_CLAIM_KIND_SQL,
    14: PIPELINE_VERSIONS_SQL,
    15: RUN_ID_SQL,
    16: STRUCTURE_JSON_SQL,
    17: DROP_STRUCTURE_JSON_SQL,
    18: TOKEN_DICT_SQL,
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

    # v12 起这些表已废弃；但 SCHEMA_SQL 每次启动都会把它们 IF NOT EXISTS 建回来。
    # 迁移结束后统一清一次，避免库里长期躺着幽灵表（见 DEPRECATED_TABLES_SQL 的说明）。
    # 用与 _apply_migration 相同的写法（BEGIN/COMMIT 写进脚本）：executescript 会自己提交，
    # 外面的 execute("BEGIN") 会被它顶掉。
    if schema_version(conn) >= 12:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + DEPRECATED_TABLES_SQL + "\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise RuntimeError(f"清理废弃表失败（已回滚）：{exc}") from exc

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
