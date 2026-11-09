"""长期记忆可信知识库：写回 + 衰减 + 冲突消解 + 召回。

- 因果：官方 `BaseStore` 语义（namespace/key 分层存取），sqlite 自建后端（无官方 sqlite store）。
- 治理：`reconcile` 确定性仲裁（软替代 / contest / fail closed），旧值永不物理删除。
- 衰减：`confidence × 时间半衰期 × 访问频率`，召回按分排序；只召回 active。
- 每条带 source / time / confidence / access_count / status / 历史版本。
"""

from __future__ import annotations  # 延迟值注解

import json  # 序列化
import time  # 时间戳
from dataclasses import dataclass, field  # 结果容器
from typing import Any, Iterable  # 类型

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
        kind: str = "memories",  # 子命名空间
    ) -> MemoryFact:  # 保留的事实
        emb = None  # 向量（规则抽取出暂不嵌）
        fact = MemoryFact(  # 构造
            key=str(key),  # key
            value=value,  # 值
            source=source,  # 来源
            confidence=float(confidence) if confidence is not None else confidence_for(MemoryFact(key=str(key), value=value, source=source)),  # 可信度
            time=float(time_s) if time_s is not None else time.time(),  # 时间
        )  # 结束
        self._reconcile(self._ns(kind), fact)  # 仲裁写回
        return fact  # 返回

    def _reconcile(self, namespace: str, new: MemoryFact) -> None:  # 确定性仲裁
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
                self._insert(conn, namespace, new.key, payload, new.source, new.confidence, stamp)  # 写
                return
            current_ts = float(row["updated_at"] or 0)  # 旧时间
            new_trusted = new.confidence > float(row["confidence"] or 0) and stamp >= current_ts  # 更可信且不旧
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
                    conn, namespace, new.key, payload, new.source, new.confidence, stamp, versions=versions,
                )  # 写
            elif new.confidence < float(row["confidence"] or 0) * 0.8:  # 冲突且更弱 → 进 contest（fail closed）
                conflict_key = f"{new.key}#contested"  # 冲突槽（非主键，避免覆盖活跃）
                self._insert(  # 冲突记录
                    conn, namespace, conflict_key, payload, new.source, min(new.confidence, 0.3), stamp,
                    status="contested",
                )  # 写
            # 否则：既不可信也不冲突 → 保留旧，忽略新（静默不覆盖）

    @staticmethod
    def _insert(  # 插入一行
        conn: Any, namespace: str, key: str, payload: str, source: str, conf: float, stamp: float,  # 参数
        *, status: str = "active", versions: list | None = None,  # 可选
    ) -> None:  # 结束
        conn.execute(  # 插
            "INSERT OR REPLACE INTO user_memories(namespace, memory_key, value, source, created_at, updated_at,"
            " confidence, access_count, last_access, status, superseded_by, history_versions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, NULL, ?)",
            (namespace, key, payload, source, stamp, stamp, conf, status,
             json.dumps(versions or [], ensure_ascii=False)),
        )  # 结束

    def get(self, key: str, *, kind: str = "memories") -> dict[str, Any] | None:  # 取活跃事实
        """按 key 取当前活跃；被替代/冲突不返回（历史版在 history_versions）。"""
        row = get_connection().execute(  # 查
            "SELECT value, source, confidence, history_versions FROM user_memories"
            " WHERE namespace = ? AND memory_key = ? AND status = 'active'",
            (self._ns(kind), key),
        ).fetchone()
        if row is None:  # 非活跃
            return None  # 无
        if row:  # 命中即记访问（衰减喂回）
            get_connection().execute(  # 记账
                "UPDATE user_memories SET access_count = access_count + 1, last_access = ? WHERE namespace = ? AND memory_key = ?",
                (time.time(), self._ns(kind), key),
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
    ) -> list[dict[str, Any]]:  # 按衰减分排序的活跃事实
        """只取 active；按 `decay_score` 排序；命中即记账。"""
        rows = get_connection().execute(  # 查该命名空间活跃
            "SELECT memory_key, value, source, confidence, updated_at, access_count, last_access FROM user_memories"
            " WHERE namespace = ? AND status = 'active'",
            (self._ns(kind),),
        ).fetchall()
        scored: list[dict[str, Any]] = []  # 带分
        for item in rows:  # 逐条
            score = decay_score(item["confidence"], item["updated_at"], item["access_count"], item["last_access"])  # 衰减分
            if item["access_count"] or True:  # 命中记访问（下次衰减用）
                get_connection().execute(  # 记账
                    "UPDATE user_memories SET access_count = access_count + 1, last_access = ? WHERE namespace = ? AND memory_key = ?",
                    (time.time(), self._ns(kind), item["memory_key"]),
                )  # 更新
            scored.append({"key": item["memory_key"], "value": json.loads(item["value"]), "source": item["source"], "confidence": item["confidence"], "score": score})  # 候选
        if scored:  # 有候选
            get_connection().commit()  # 提交记账
        return sorted(scored, key=lambda d: -d["score"])[:top_k]  # 排序取 top


def extract_eav(text: str) -> list[MemoryFact]:  # 规则抽 EAV 事实
    """从一句话里抽 订单/金额/日期 实体，作为长期事实候选（确定性，置信度默认转述级）。"""
    from app.agent.preprocess.entities import _ENTITY_RULES  # 复用规则表

    facts: list[MemoryFact] = []  # 结果
    for attr, pattern in _ENTITY_RULES:  # 逐规则
        match = pattern.search(str(text or ""))  # 搜索
        if match:  # 命中
            facts.append(MemoryFact(key=f"{attr}::{match.group(1)}", value=match.group(1), source="paraphrase"))  # 事实
    return facts  # 返回