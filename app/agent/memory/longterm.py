"""长期记忆可信知识库：写回 + 衰减 + 冲突消解 + 召回。

- 因果：官方 `BaseStore` 语义（namespace/key 分层存取），sqlite 自建后端（无官方 sqlite store）。
- 治理：`reconcile` 确定性仲裁（软替代 / contest / fail closed），旧值永不物理删除。
- 衰减：`confidence × 时间半衰期 × 访问频率`，召回按分排序；只召回 active。
- 每条带 source / time / confidence / access_count / status / 历史版本。
"""

from __future__ import annotations  # 延迟值注解

import json  # 序列化
import time  # 时间戳
from collections.abc import Iterable  # 类型
from dataclasses import dataclass, field  # 结果容器
from typing import Any

from app.core.db.connection import get_connection, transaction  # DB

__all__ = ["MemoryFact", "MemoryStore", "confidence_for", "decay_score", "extract_eav"]  # 导出

#: 来源 → 基础可信度（① 来源可信度表）。
SOURCE_CONFIDENCE: dict[str, float] = {  # 来源表
    "user_explicit": 0.95,  # 用户显式陈述
    "user_confirmed": 0.90,  # 用户确认
    "verified": 0.85,  # 工具/检索验证
    "inferred_multi": 0.75,  # 多轮归纳
    "paraphrase": 0.60,  # 单轮转述
    "hedged": 0.40,  # 带含糊词
    "guess": 0.30,  # 无依据猜测
}  # 来源表结束

#: 混淆词（命中即把置信度压到 ≤0.5）。
_VAGUE_WORDS = ("好像", "可能", "也许", "大概", "不确定", "差不多")  # 含糊词

#: 时间半衰期（天）：可信度衰减到一半所需天数。
TIME_HALF_LIFE_S = 30 * 86400  # 30 天
#: 访问半衰期（秒）：近期未访问降权。
ACCESS_HALF_LIFE_S = 14 * 86400  # 14 天

#: 冲突治理计数（进程内累计，供观测/P5 指标；不持久化，重启清零可接受）。
_COUNTERS: dict[str, int] = {"contested": 0, "superseded": 0}  # 冲突/软替代 计数


def _inc(kind: str) -> None:  # 冲突计数 +1
    """进程内累计（contest/supersede 触发时调用）。"""
    _COUNTERS[kind] = _COUNTERS.get(kind, 0) + 1  # 累加


def conflict_stats() -> dict[str, int]:  # 冲突治理统计（观测/指标用）
    """返回进程内累计的冲突/替代发生次数（调用方=services/memory 与 P5 指标）。"""
    return dict(_COUNTERS)  # 拷贝


def _vector_retrieval_enabled() -> bool:  # 记忆向量检索开关
    """读配置 `memory.vector.enabled`（默认 True）；关闭则召回退回 sqlite 全扫。"""
    try:  # 配置可能未就绪
        from app.core.config.store import load as load_config  # 配置

        return bool(load_config().values.get("memory.vector.enabled", True))  # 读开关
    except Exception:  # 读失败
        return True  # 默认开


@dataclass(frozen=True)
class MemoryFact:  # 一条长期记忆
    """写入/仲裁用的结构化事实。"""

    key: str  # EAV：entity::attribute
    value: Any  # 事实值
    source: str = ""  # 来源类型
    confidence: float = 0.5  # 可信度 0..1
    time: float = field(default_factory=time.time)  # 事实时间


def confidence_for(  # 可信度检测
    fact: MemoryFact,  # 事实
    *,
    llm_conf: float | None = None,  # LLM 自带分（仅占 30% 权）
    evidence: int = 1,  # 独立提及次数
) -> float:  # 最终可信度
    """基础分×0.5 + LLM×0.3 + 证据×0.2；含糊词压限。"""
    base = SOURCE_CONFIDENCE.get(str(fact.source or ""), 0.5)  # 来源基础分
    llm = float(llm_conf if llm_conf is not None else base)  # LLM 分（缺省用基础）
    proof = min(1.0, 0.55 + 0.2 * (int(evidence) - 1)) if evidence >= 1 else 0.3  # 证据系数
    score = base * 0.5 + llm * 0.3 + proof * 0.2  # 加权合并
    text = str(fact.value if isinstance(fact.value, str) else (fact.value or ""))  # 值文本
    if any(word in text for word in _VAGUE_WORDS):  # 含糊词
        score = min(score, 0.5)  # 压限
    return round(max(0.0, min(1.0, score)), 4)  # 夹到 0..1


def decay_score(  # 衰减分
    confidence: float,  # 可信度
    updated_at: float,  # 最近时间
    access_count: int = 0,  # 访问次数
    last_access: float = 0.0,  # 最近访问
    *,
    now: float | None = None,  # 观测时刻
) -> float:  # 相关性分
    """时间半衰期 × 访问频率，乘可信度。"""
    now_t = float(now) if now is not None else time.time()  # 当前
    age = max(0.0, now_t - float(updated_at))  # 已老化秒
    time_factor = 0.5 ** (age / TIME_HALF_LIFE_S)  # 时间衰减（遗忘曲线）
    access_delta = max(0.0, now_t - float(last_access)) if last_access else float("inf")  # 距最近访问
    recency = 0.5 ** (access_delta / ACCESS_HALF_LIFE_S) if last_access else 0.05  # 访问新颖度
    freq = min(1.0, 0.2 + 0.1 * float(access_count))  # 访问频率饱和
    return round(float(confidence) * time_factor * (0.3 + 0.7 * recency) * freq, 6)  # 综合分


def _ns_store(namespace: Iterable[str]) -> str:  # 命名空间序列化
    """元组/列表 → 稳定 JSON（与 BaseStore 的命名空间语义对齐）。"""
    return json.dumps([str(part) for part in (namespace or ())], ensure_ascii=False, sort_keys=False)  # 序列化


@dataclass
class MemoryStore:  # 长期记忆仓储（sqlite 后端）
    """namespaced 读写 + 确定性仲裁 + 软替代 + 召回。"""

    namespace_base: tuple[str, ...] = ("default", "user", "default")  # 租户/用户占位（单租户）

    def _ns(self, kind: str = "memories") -> str:  # 组装命名空间
        return _ns_store((*self.namespace_base, kind))  # 拼

    def put(  # 写入（经仲裁，不进热路径）
        self,
        key: str,  # EAV key
        value: Any,  # 值
        *,
        source: str = "user_explicit",  # 来源
        confidence: float | None = None,  # 可选覆盖可信度
        time_s: float | None = None,  # 可选事实时间
        embedding: Iterable[float] | None = None,  # 可选向量（语义召回用）
        kind: str = "memories",  # 子命名空间
    ) -> MemoryFact:  # 保留的事实
        # 未显式给向量则自动补算（语义召回前提）；嵌入不可用返回 None → 存 NULL（召回退纯衰减）。
        val_text = value if isinstance(value, str) else str(value if not isinstance(value, dict) else "")  # 向量用文本形态
        safe_emb = embedding if embedding is not None else embed_text(val_text)  # 自动补向量
        emb_bytes = (json.dumps(list(safe_emb), ensure_ascii=False).encode("utf-8") if safe_emb is not None else None)  # 向量序列化
        fact = MemoryFact(  # 构造
            key=str(key),  # key
            value=value,  # 值
            source=source,  # 来源
            confidence=float(confidence) if confidence is not None else confidence_for(MemoryFact(key=str(key), value=value, source=source)),  # 可信度
            time=float(time_s) if time_s is not None else time.time(),  # 时间
        )  # 结束
        self._reconcile(self._ns(kind), fact, emb_bytes)  # 仲裁写回
        return fact  # 返回

    def _reconcile(self, namespace: str, new: MemoryFact, embedding: bytes | None = None) -> None:  # 确定性仲裁
        """无活跃 → active；新且更旧更可信 → 旧软替代；冲突 → contest + fail closed。"""
        row = get_connection().execute(  # 查现有活跃
            "SELECT value, source, confidence, created_at, history_versions, updated_at FROM user_memories"
            " WHERE namespace = ? AND memory_key = ? AND status = 'active'",
            (namespace, new.key),
        ).fetchone()
        payload = json.dumps(new.value, ensure_ascii=False, default=str)  # 新值
        stamp = float(new.time)  # 新时间
        with transaction() as conn:  # 单事务幂等
            if row is None:  # 无活跃 → 直接 active
                self._insert(conn, namespace, new.key, payload, new.source, new.confidence, stamp, embedding=embedding)  # 写
                return
            current_ts = float(row["updated_at"] or 0)  # 旧时间
            # 时间优先：严格更强可替代；同样强但更新也替代（同来源新近优先，见方案 6.4.4）。
            new_trusted = (  # 新值是否够格替代
                (new.confidence > float(row["confidence"] or 0) and stamp >= current_ts)  # 更强且不旧
                or (new.confidence >= float(row["confidence"] or 0) and stamp > current_ts)  # 同样强且更新
            )  # 复合判定
            if new_trusted:  # 软替代：保留旧内容于历史
                versions = json.loads(row["history_versions"] or "[]")  # 历史
                versions.append(  # 追加旧版本
                    {"value": json.loads(row["value"]), "source": row["source"], "confidence": row["confidence"], "time": current_ts}  # 旧值
                )  # 结束
                conn.execute(  # 旧标 superseded
                    "UPDATE user_memories SET status = 'superseded', superseded_by = ? WHERE namespace = ? AND memory_key = ? AND status = 'active'",
                    (new.key, namespace, new.key),
                )  # 更新
                self._insert(  # 新 active（带版本历史）
                    conn, namespace, new.key, payload, new.source, new.confidence, stamp, versions=versions, embedding=embedding,
                )  # 写
                _inc("superseded")  # 计软替代
            elif new.confidence < float(row["confidence"] or 0) * 0.8:  # 冲突且更弱 → 进 contest（fail closed）
                conflict_key = f"{new.key}#contested"  # 冲突槽（非主键，避免覆盖活跃）
                self._insert(  # 冲突记录
                    conn, namespace, conflict_key, payload, new.source, min(new.confidence, 0.3), stamp,
                    status="contested", embedding=embedding,
                )  # 写
                _inc("contested")  # 计冲突
            # 否则：既不可信也不冲突 → 保留旧，忽略新（静默不覆盖）

    @staticmethod
    def _insert(  # 插入一行
        conn: Any, namespace: str, key: str, payload: str, source: str, conf: float, stamp: float,  # 参数
        *, status: str = "active", versions: list | None = None, embedding: bytes | None = None,  # 可选
    ) -> None:  # 结束
        conn.execute(  # 插
            "INSERT OR REPLACE INTO user_memories(namespace, memory_key, value, source, created_at, updated_at,"
            " confidence, access_count, last_access, status, superseded_by, embedding, history_versions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, NULL, ?, ?)",
            (namespace, key, payload, source, stamp, stamp, conf, status, embedding,
             json.dumps(versions or [], ensure_ascii=False)),
        )  # 结束

    def get(self, key: str, *, kind: str = "memories", now: float | None = None) -> dict[str, Any] | None:  # 取活跃事实
        """按 key 取当前活跃；被替代/冲突/过期/归档不返回（历史版在 history_versions）。"""
        nw = float(now) if now is not None else time.time()  # 观测时刻
        row = get_connection().execute(  # 查（排除过期/归档）
            "SELECT value, source, confidence, history_versions FROM user_memories"
            " WHERE namespace = ? AND memory_key = ? AND status = 'active'"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (self._ns(kind), key, nw),
        ).fetchone()
        if row is None:  # 非活跃/过期
            return None  # 无
        if row:  # 命中即记访问（衰减喂回）
            get_connection().execute(  # 记账
                "UPDATE user_memories SET access_count = access_count + 1, last_access = ? WHERE namespace = ? AND memory_key = ?",
                (nw, self._ns(kind), key),
            )  # 更新
            get_connection().commit()  # 提交
        return {  # 返回
            "key": key,  # key
            "value": json.loads(row["value"]),  # 值
            "source": row["source"],  # 来源
            "confidence": float(row["confidence"] or 0),  # 可信度
            "history_versions": json.loads(row["history_versions"] or "[]"),  # 历史
        }  # 结束

    def search(  # 召回
        self,
        *,  # 关键字
        kind: str = "memories",  # 子命名空间
        top_k: int = 10,  # 上限
        query_embedding: Iterable[float] | None = None,  # 可选查询向量（语义召回）
        now: float | None = None,  # 观测时刻（可注入，测试用）
    ) -> list[dict[str, Any]]:  # 按相关性分排序的活跃事实
        """只取 active 且未过期；命中（返回行）才记账。

        - 有查询向量 → 优先 Qdrant 余弦召回（回 sqlite 补全全量，score 用余弦）；
        - Qdrant 不可用/无点数/无向量 → 降级 sqlite 全扫（余弦×衰减，与旧实现一致）；
        - 无查询向量 → 纯衰减排序。
        """
        nw = float(now) if now is not None else time.time()  # 观测时刻
        qvec = list(query_embedding) if query_embedding is not None else None  # 查询向量
        qdrant_keys: list[tuple[str, float]] | None = None  # Qdrant 命中 (key, score)
        if qvec and _vector_retrieval_enabled():  # 给了查询向量且启用向量检索 → 优先 Qdrant
            try:  # Qdrant 失败降级 sqlite
                from app.memory.vector import ensure_memory_health, search_memory_points  # 向量查询

                ns = self._ns(kind)  # 命名空间
                if ensure_memory_health():  # Qdrant 可用
                    hits = search_memory_points(ns, qvec, top_k=int(top_k) * 4)  # 召回（放宽供内存补全）
                    refs = [h for h in hits if self._row_active(ns, str(h["memory_key"]), nw)]  # 只留仍 active 未过期的
                    if refs:  # 命中有效点
                        qdrant_keys = [(str(r["memory_key"]), float(r["score"])) for r in refs]  # 记录
            except Exception:  # 查询失败
                qdrant_keys = None  # 降级
        if qdrant_keys is not None:  # 走 Qdrant 结果
            return self._search_from_keys(qdrant_keys, kind, top_k, nw)  # 补全+记账
        # —— 降级：sqlite 全扫（原逻辑）——
        rows = get_connection().execute(  # 查该命名空间活跃且未过期（含向量）
            "SELECT memory_key, value, source, confidence, updated_at, access_count, last_access, embedding FROM user_memories"
            " WHERE namespace = ? AND status = 'active' AND (expires_at IS NULL OR expires_at > ?)",
            (self._ns(kind), nw),
        ).fetchall()
        scored: list[dict[str, Any]] = []  # 带分
        for item in rows:  # 逐条
            decay = decay_score(item["confidence"], item["updated_at"], item["access_count"], item["last_access"])  # 衰减分
            sim = 0.0  # 余弦
            if qvec:  # 给了查询向量
                raw = item["embedding"]  # 存的事实向量
                if raw:  # 有向量
                    try:  # 解析
                        sim = cosine_sim(qvec, json.loads(raw.decode("utf-8")))  # 余弦
                    except Exception:  # 坏向量
                        sim = 0.0  # 归零
            score = round(0.7 * sim + 0.3 * decay, 6) if qvec else decay  # 综合分
            scored.append({"key": item["memory_key"], "value": json.loads(item["value"]), "source": item["source"], "confidence": item["confidence"], "score": score})  # 候选
        ranked = sorted(scored, key=lambda d: -d["score"])[:top_k]  # 排序取 top
        if ranked:  # 有返回行
            now = time.time()  # 统一时间戳
            with transaction():  # 单事务批量记账
                for item in ranked:  # 只对返回行记账（喂回衰减）
                    get_connection().execute(  # 记账
                        "UPDATE user_memories SET access_count = access_count + 1, last_access = ? WHERE namespace = ? AND memory_key = ?",
                        (now, self._ns(kind), item["key"]),
                    )  # 更新
        return ranked  # 返回

    def _row_active(self, ns: str, key: str, nw: float) -> bool:  # 单行是否仍 active 未过期
        """Qdrant payload 的 status/t 可能有延迟；以 sqlite 为准判定该 key 是否仍可召回。"""
        row = get_connection().execute(  # 查活跃未过期
            "SELECT 1 FROM user_memories WHERE namespace = ? AND memory_key = ? AND status = 'active'"
            " AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            (ns, key, nw),
        ).fetchone()
        return row is not None  # 命中即可用

    def _search_from_keys(  # 按 Qdrant 命中的 key 补全
        self, refs: list[tuple[str, float]], kind: str, top_k: int, nw: float,
    ) -> list[dict[str, Any]]:  # 结果
        """按 key 批取 sqlite 行补全 value/source/confidence，score 用 Qdrant 余弦排序，且只对返回行记账。"""
        keys = [k for k, _s in refs]  # 键列表
        placeholders = ",".join("?" * len(keys))  # 占位
        rows = get_connection().execute(  # 批取行
            f"SELECT memory_key, value, source, confidence, updated_at, access_count, last_access FROM user_memories"
            f" WHERE namespace = ? AND status = 'active' AND memory_key IN ({placeholders})"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (self._ns(kind), *keys, nw),
        ).fetchall()
        by_key = {r["memory_key"]: r for r in rows}  # 建索引
        out: list[dict[str, Any]] = []  # 结果
        for key, score in refs:  # 按 Qdrant 顺序
            row = by_key.get(key)  # 取行
            if row is None:  # 行没了（并发删除）
                continue  # 跳过
            out.append({  # 组装
                "key": key,  # 键
                "value": json.loads(row["value"]),  # 值
                "source": row["source"],  # 来源
                "confidence": float(row["confidence"] or 0),  # 可信度
                "score": float(score),  # Qdrant 余弦分
            })  # 结束
        ranked = out[:top_k]  # 截断
        if ranked:  # 有返回行
            now = time.time()  # 统一时间戳
            with transaction():  # 单事务记账
                for item in ranked:  # 只对返回行记账
                    get_connection().execute(  # 记账
                        "UPDATE user_memories SET access_count = access_count + 1, last_access = ? WHERE namespace = ? AND memory_key = ?",
                        (now, self._ns(kind), item["key"]),
                    )  # 更新
        return ranked  # 返回


def extract_eav(text: str) -> list[MemoryFact]:  # 规则抽 EAV 事实
    """从一句话里抽 订单/金额/日期 实体，作为长期事实候选（确定性，置信度默认转述级）。

    键取属性名（如 ``order_id`` / ``amount`` / ``date``），值进 ``value`` 列——这样
    同一属性出现不同值时才可能被 `_reconcile` 识别为冲突（软替代/contest）。
    若键带值（如旧版 ``order_id::37979``），则每次都是不同 key，永远查不到"旧值"，
    矛盾事实会各自 active 共存。待引入实体绑定后键可升级为 ``实体::属性``。
    """
    from app.agent.preprocess.entities import _ENTITY_RULES  # 复用规则表

    facts: list[MemoryFact] = []  # 结果
    for attr, pattern in _ENTITY_RULES:  # 逐规则
        match = pattern.search(str(text or ""))  # 搜索
        if match:  # 命中
            facts.append(MemoryFact(key=str(attr), value=match.group(1), source="paraphrase"))  # 事实
    return facts  # 返回


def cosine_sim(a: Iterable[float], b: Iterable[float]) -> float:  # 余弦相似度
    """两个等长向量的余弦；空/坏输入返回 0。"""
    va = list(a)  # a
    vb = list(b)  # b
    if not va or not vb or len(va) != len(vb):  # 形状不对
        return 0.0  # 0
    dot = sum(x * y for x, y in zip(va, vb))  # 点积
    na = sum(x * x for x in va) ** 0.5  # 模 a
    nb = sum(y * y for y in vb) ** 0.5  # 模 b
    if na == 0 or nb == 0:  # 零向量
        return 0.0  # 0
    return dot / (na * nb)  # 余弦


def embed_text(text: str) -> list[float] | None:  # 文本 → 向量（复用本地 bge-m3）
    """嵌入不可用/失败返回 None（召回退回纯衰减）。"""
    try:  # 模型可能没配
        from app.rag.retrieval import embed_query  # 复用嵌入

        vec = embed_query(str(text or ""))  # 嵌入
        if vec is None:  # 空
            return None  # 无
        return list(vec)  # 列表
    except Exception:  # 任何失败
        return None  # 退回
