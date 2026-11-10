"""记忆统一管理入口：写入判定 / 去重 / PII 脱敏 / 遗忘 / 召回 / 蒸馏。

**为什么收口**：此前长期记忆写入散落在 summarizer（压缩顺手写）与 finalize（收尾写），
既不去重也不做 PII 合规。本模块是**唯一**对 `MemoryStore` 的读写口（写入纪律见包 docstring）；
上层（节点/任务）只依赖本模块的公开函数，不直连 store。

公开函数：
- `recall`             —— 召回可选事实（供上下文装配；P2 起并入画像）
- `record_facts`       —— 写一批事实（去重 + PII 脱敏 + 白名单画像分流预留）
- `forget_expired`     —— 过期归档（memory_cleanup 任务调用）
- `delete_for_conversation` —— 按会话删除记忆（合规遗忘入口）
- `delete_all`         —— 清空全部记忆（合规遗忘入口）
- `distill_conversation`—— 从某会话压缩段抽事实写回（规则抽取，T3.3 起接 LLM 结构化）
- `conflict_stats`     —— 冲突治理统计（转自 longterm，P5 指标读取）
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳

from app.core.db.connection import get_connection, transaction  # DB

__all__ = [  # 导出
    "RecallBundle",  # 召回结果
    "check_consistency",  # 一致性校验
    "confirm_profile",  # 画像显式更正
    "conflict_stats",  # 冲突统计
    "delete_all",  # 清空全部
    "delete_for_conversation",  # 按会话删除
    "delete_profile",  # 画像归档
    "distill_conversation",  # 蒸馏写回
    "forget_expired",  # 过期归档
    "recall",  # 召回
    "record_facts",  # 写事实
]  # 结束


class RecallBundle:  # 一次召回的结果
    """上下文装配读取的可选记忆（事实 + 画像行；P1 画像恒空，P2 填充）。"""

    __slots__ = ("facts", "profile_lines")  # 定形省内存

    def __init__(self, facts: list[dict], profile_lines: list[str] | None = None) -> None:  # 构造
        self.facts = list(facts)  # 长期事实（search 结果形状）
        self.profile_lines = list(profile_lines or [])  # 画像可渲染行（P2 起填充）


def _redact_value(value: object) -> tuple[str, int]:  # 脱敏一个事实值
    """对值文本做 PII 脱敏，返回（清洗后文本, 命中条数）。

    脱敏规则复用中间件 `app.agent.middleware.pii.redact`；命中>0 时由调用方记 trace 事件。
    """
    from app.agent.middleware.pii import redact  # 脱敏（与出站同规则）

    text = str(value if isinstance(value, str) else (value or ""))  # 文本形态
    cleaned, counts = redact(text)  # 脱敏（counts: 类型→次数）
    hits = sum(counts.values())  # 总命中
    return cleaned, int(hits)  # 返回


def _trace_redacted(conversation_id: str, hits: int) -> None:  # 记一条脱敏事件
    """蒸馏阶段 PII 被抓到 → 写 trace_events（可观测，best-effort）。"""
    try:  # 写失败不影响主体
        from app.core.trace import store as trace_store  # trace

        trace_store.add_event(  # 插入
            str(conversation_id or ""),  # run_id（无则空串）
            level="info",  # 等级
            code="memory_pii_redacted",  # 码
            message="记忆蒸馏触发了 PII 脱敏",  # 标题
            detail={"hits": int(hits)},  # 明细
        )  # 结束
    except Exception:  # trace 写入失败不阻断
        pass  # 忽略


def record_facts(  # 写一批事实（去重 + PII 脱敏）
    facts: list,  # MemoryFact 列表（从不发模型的内部数据模型）
    *,
    redact_pii: bool = True,  # 是否脱敏（默认开；测试可关）
    conversation_id: str = "",  # 来源会话（脱敏事件挂靠用）
) -> int:  # 实际写入条数
    """逐条去重（同 key 同值且 active 则跳过）+ PII 脱敏后写长期库。

    返回实际写入条数（跳过重复不计）。调用方：summarizer 蒸馏、finalize 收尾 promote。
    """
    from app.agent.memory.longterm import MemoryStore, confidence_for  # 长期库

    store = MemoryStore()  # 唯一仓储（经本模块）
    written = 0  # 写入计数
    for fact in facts:  # 逐条
        key = str(fact.key or "")  # key
        value = fact.value  # 原始值
        if not key:  # 无 key（防御）
            continue  # 跳过
        if redact_pii:  # 需脱敏
            value, hits = _redact_value(fact.value)  # 清洗值
            if hits:  # 抓到 PII
                _trace_redacted(conversation_id, hits)  # 记事件
        if _route_to_profile(fact):  # 画像键 → 画像层（模型推断，达阈值才写）
            if _write_profile_fact(fact.key, value, fact.source):  # 写画像
                written += 1  # 计
            continue  # 不再进长期事实层
        if _dup_active(key, value):  # 已存在同 key 同值 active → 去重
            continue  # 跳过
        store.put(  # 写（置信度用既有来源表估算）
            key=key,  # key
            value=value,  # 值（已脱敏）
            source=str(fact.source or "paraphrase"),  # 来源
            confidence=confidence_for(fact) if getattr(fact, "confidence", 0) <= 0 else float(fact.confidence),  # 可信度
            time_s=float(fact.time or time.time()),  # 时间
        )  # 写
        _sync_vector_fact(key, value)  # 向量镜像（best-effort 双写，Qdrant 可用时才生效）
        written += 1  # 计
    return written  # 返回


def _sync_vector_fact(key: str, value: object) -> None:  # 向量镜像同步
    """把单条事实镜像到 Qdrant（best-effort；读仍走 sqlite，向量为未来检索/换模型留底）。

    不阻塞主流程：Qdrant 未就绪或失败都安静跳过（降级保持 sqlite 可用）。
    """
    from contextlib import suppress  # 抑制

    try:  # 全局唤醒一次（幂等）
        from app.memory.vector import ensure_memory_health  # 向量模块

        ensure_memory_health()  # 探活/建集合
    except Exception:  # 不可用
        return  # 跳过
    with suppress(Exception):  # 写失败不影响
        from app.agent.memory.longterm import embed_text  # 嵌入
        from app.memory.vector import upsert_memory_points  # 向量写

        vec = embed_text(str(value if isinstance(value, str) else (value or "")))  # 向量（可能 None）
        if not vec:  # 无向量
            return  # 跳过
        upsert_memory_points(  # 镜像（单条）
            __import__("json").dumps(["default", "user"], ensure_ascii=False),  # 作用域
            [{"memory_key": str(key), "vector": vec, "value": value, "status": "active", "t": time.time()}],  # 单点
        )  # 写


def _write_profile_fact(key: str, value: object, source: str) -> bool:  # 写画像条目
    """把白名单键事实写入画像层（显式来源高可信直达，推断达阈值才生效）。

    返回是否成为 active。
    """
    from app.agent.memory.longterm import (
        MemoryFact,  # 事实形状（仅取可信度）
        confidence_for,  # 来源可信度
    )
    from app.memory.model import EXPLICIT, ProfileEntry, default_namespace  # 画像模型
    from app.memory.store import ProfileStore  # 画像仓储

    src = str(source or "inferred")  # 来源
    conf = confidence_for(MemoryFact(key=str(key), value=value, source=src))  # 按来源估可信度
    is_explicit = src in ("user_explicit", "user_correction", "confirmed")  # 是否是显式来源
    store = ProfileStore(default_namespace())  # 画像仓储（单租户默认作用域）
    entry = ProfileEntry(  # 构造条目
        prop_key=str(key),  # 键
        value=value,  # 值
        prop_kind=EXPLICIT if is_explicit else "inferred",  # 类型（显式走 explicit）
        source=src,  # 来源
        confidence=conf,  # 可信度（显式高、推断低）
    )  # 结束
    threshold = 0.0 if is_explicit else _profile_threshold()  # 显式不受阈值限制
    return store.upsert(entry, infer_threshold=threshold)  # 达阈值才写


def _dup_active(key: str, value: object) -> bool:  # 去重判定
    """同 key 同值且 active 的已存在 → True（跳过重复写，方案 6.5 幂等）。"""
    payload = __import__("json").dumps(value, ensure_ascii=False, default=str)  # 值的可比较序列化（与入库同口径）
    row = get_connection().execute(  # 查同 key 同值 active
        "SELECT 1 FROM user_memories WHERE memory_key = ? AND value = ? AND status = 'active' LIMIT 1",
        (str(key), payload),
    ).fetchone()
    return row is not None  # 命中即重复


def _profile_threshold() -> float:  # 画像推断阈值
    """读配置 `memory.profile.infer_threshold`（默认 0.7）。"""
    try:  # 配置可能未就绪
        from app.core.config.store import load as load_config  # 配置

        val = load_config().values.get("memory.profile.infer_threshold")  # 读
        return float(val) if val is not None else 0.7  # 有效用值，否则 0.7
    except Exception:  # 读失败
        return 0.7  # 默认


def _route_to_profile(fact) -> bool:  # 画像键白名单判定
    """命中白名单键的事实 → 画像层；否则长期事实层（T2.3 分流）。"""
    from app.memory.model import PROFILE_KEYS  # 画像键白名单

    return str(fact.key or "") in PROFILE_KEYS  # 命中白名单


def recall(  # 召回可选记忆
    question: str = "",  # 当前问题（嵌入查询用，空则纯衰减）
    *,
    top_k: int = 6,  # 事实上限
) -> RecallBundle:  # 装配用结果
    """召回长期事实（未过期/未归档）+ 画像可渲染行。"""
    from app.agent.memory.longterm import MemoryStore, embed_text  # 长期库/嵌入

    facts: list[dict] = []  # 事实
    profile_lines: list[str] = []  # 画像行
    try:  # 取数失败不阻断
        store = MemoryStore()  # 仓储
        qemb = embed_text(str(question)) if question else None  # 查询向量（空问题退纯衰减）
        facts = store.search(top_k=top_k, query_embedding=qemb)  # 召回事实
    except Exception:  # 无表/坏库
        facts = []  # 空
    try:  # 画像读取失败不阻断
        from app.memory.model import default_namespace  # 画像作用域
        from app.memory.render import render_profile_block  # 渲染
        from app.memory.store import ProfileStore  # 画像仓储

        profile_lines = list(  # 渲染 active 画像
            filter(None, [render_profile_block(ProfileStore(default_namespace()).get_all())])  # 非空段
        )
    except Exception:  # 画像未初始化
        profile_lines = []  # 空
    return RecallBundle(facts=facts, profile_lines=profile_lines)  # 返回


def confirm_profile(prop_key: str, value: object) -> None:  # 用户显式更正画像
    """用户显式更正某画像键（最高优先级覆盖）。调用方：设置页（经 services 门面）。"""
    from app.memory.model import default_namespace  # 画像作用域
    from app.memory.store import ProfileStore  # 画像仓储

    ProfileStore(default_namespace()).confirm(prop_key, value)  # 强制覆盖


def delete_profile(prop_key: str) -> None:  # 归档某画像键（用户删除）
    """归档某画像键（不再参与渲染）。调用方：设置页（经 services 门面）。"""
    from app.memory.model import default_namespace  # 画像作用域
    from app.memory.store import ProfileStore  # 画像仓储

    ProfileStore(default_namespace()).archive(prop_key)  # 归档


def forget_expired(*, now: float | None = None) -> int:  # 过期归档
    """把已过期的 active 记忆归档（status=archived，非物理删）。

    返回归档行数。调用方：`app/tasks/memory_cleanup` 任务。
    """
    nw = float(now) if now is not None else time.time()  # 观测时刻
    with transaction() as conn:  # 单事务
        cur = conn.execute(  # 归档过期行（保留数据，降权不出库）
            "UPDATE user_memories SET status = 'archived', archived_at = ?"
            " WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (nw, nw),
        )  # 更新
        return int(cur.rowcount)  # 归档数


def delete_for_conversation(conversation_id: str) -> int:  # 按会话删除记忆
    """删除与某会话相关的记忆条目（合规遗忘）。

    当前记忆按 key 存，无会话归属字段；这里删除该会话压缩段蒸馏可能产生的事实——
    以 key 前缀匹配为准（P4 完善：蒸馏时给记忆行补 conversation_id）。返回受影响行数。
    """
    del conversation_id  # 占位（P4 实现真正按会话级联删除）
    return 0  # 暂无实现（P4 补全）


def delete_all(*, conversation_id: str = "") -> int:  # 清空记忆（合规遗忘）
    """清空全部长期记忆 + 用户画像 + 向量点（合规遗忘，不留残留）。"""
    del conversation_id  # 占位（P4 接子集）
    from app.memory.vector import delete_memory_points  # 向量删除

    delete_memory_points(__import__("json").dumps(["default", "user"], ensure_ascii=False))  # 清 Qdrant（默认作用域）
    with transaction() as conn:  # 单事务
        cur = conn.execute("DELETE FROM user_memories")  # 清空长期事实
        conn.execute("DELETE FROM user_profile")  # 清空画像
        return int(cur.rowcount)  # 删除行数


def distill_conversation(conversation_id: str) -> int:  # 蒸馏写回
    """从会话最近压缩段读原文，抽规则 EAV 事实 + LLM 画像偏好，经 `record_facts` 写回。

    双通道：① 规则抽订单/金额/日期（确定性降级）；② LLM 结构化抽稳定偏好（默认模型，
    不可用则跳过）。返回写入条数。调用方：`app/tasks/memory_distill` 任务。
    """
    import json  # 覆盖消息 id 解析

    rows = get_connection().execute(  # 读最后一段 refs
        "SELECT source_refs FROM summary_segments"
        " WHERE conversation_id = ? ORDER BY seg_seq DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if rows is None:  # 无段
        return 0  # 无
    refs = json.loads(rows["source_refs"] or "[]")  # 覆盖消息 id
    if not refs:  # 空
        return 0  # 无
    msgs = get_connection().execute(  # 读这些消息内容
        "SELECT content FROM messages WHERE conversation_id = ? AND id IN (%s)"  # noqa: UP031
        % ",".join("?" * len(refs)),  # 动态占位符数量（refs 长度可变，不能用固定 f-string）
        (conversation_id, *[int(r) for r in refs]),
    ).fetchall()
    from app.agent.memory.longterm import MemoryFact, extract_eav  # 规则事实抽取

    facts: list = []  # 事实候选
    fragment_lines: list[str] = []  # 对话文本（LLM 抽取输入）
    for msg in msgs:  # 逐消息
        content = str(msg["content"] or "")  # 正文
        fragment_lines.append(content)  # 收对话文本
        for fact in extract_eav(content):  # 规则抽取
            facts.append(MemoryFact(key=fact.key, value=fact.value, source=fact.source, time=fact.time))  # 候选
    # —— ② LLM 画像通道（默认模型；best-effort，失败不阻断）——
    try:  # 模型抽取失败不影响规则通道
        from app.memory.extract import extract_profile  # LLM 画像抽取

        for cand in extract_profile("\n".join(fragment_lines), slot="llm"):  # 调默认模型
            facts.append(  # 画像候选 → 统一写（走 record_facts 白名单分流到画像层）
                MemoryFact(  # 事实形状
                    key=str(cand["key"]),  # 画像键
                    value=cand["value"],  # 值
                    source="inferred",  # 来源
                    confidence=float(cand.get("confidence") or 0.55),  # 可信度
                )  # 构造
            )  # 追加
    except Exception:  # 模型不可用/失败
        pass  # 降级纯规则
    # —— ③ 行为统计通道（零 LLM：回答长度偏好；样本≥5 才写）——
    try:  # 行为统计失败不影响
        from app.memory.behavior import signal_from_messages  # 行为信号

        recent_msgs = get_connection().execute(  # 取该会话最近 50 条消息（算回答长度）
            "SELECT role, content FROM messages WHERE conversation_id = ? AND channel = 'chat'"
            " ORDER BY seq DESC LIMIT 50",
            (conversation_id,),
        ).fetchall()
        signals = signal_from_messages([dict(r) for r in recent_msgs])  # 算行为信号
        if signals.verbosity:  # 达样本门槛
            facts.append(  # 画像候选（verbosity：回答长度偏好）
                MemoryFact(  # 事实形状
                    key="verbosity",  # 画像键
                    value=signals.verbosity,  # 值
                    source="confirmed",  # 行为统计视为"已确认接受形态"
                    confidence=0.65,  # 行为统计可行度（中等）
                )  # 构造
            )  # 追加
    except Exception:  # 行为统计失败
        pass  # 跳过
    return record_facts(facts, redact_pii=True, conversation_id=conversation_id)  # 统一写回（去重+脱敏+画像分流）


def conflict_stats() -> dict[str, int]:  # 冲突治理统计
    """进程内累计的冲突/替代统计（转自 longterm；P5 指标读取）。"""
    from app.agent.memory.longterm import conflict_stats as _lts  # 底层统计

    return _lts()  # 返回


def check_consistency() -> dict[str, int]:  # 一致性校验（T4.4）
    """三条治理规则在 memory_cleanup 任务里定期跑：

    - ① 长期悬置的 contested 条目（>7 天）→ 归档（防冲突堆积）；
    - ② 画像键与长期事实键重叠且值冲突 → 画像优先（显式>推断），事实降权 0.8；
    - ③ history_versions 超过 10 层 → 裁剪最旧（防版本膨胀）。

    返回每条规则的处理计数。
    """
    import json  # 版本历史解析
    import time  # 时间戳

    counts: dict[str, int] = {"stale_contested": 0, "fact_demoted": 0, "version_trimmed": 0}  # 计数
    now = time.time()  # 观测时刻
    # ① 过期悬置 contested 归档
    old = get_connection().execute(  # 找出超过 7 天的 contested
        "SELECT memory_key, updated_at FROM user_memories WHERE status = 'contested' AND updated_at < ?",
        (now - 7 * 86400,),
    ).fetchall()
    for row in old:  # 逐条归档
        get_connection().execute(  # 归档
            "UPDATE user_memories SET status = 'archived', archived_at = ? WHERE memory_key = ? AND status = 'contested'",
            (now, row["memory_key"]),
        )  # 更新
        counts["stale_contested"] += 1  # 计
    # ② 降权与画像冲突的老事实（画像 active 键）
    try:  # 画像表可能未建
        profile_keys = {r["prop_key"] for r in get_connection().execute(  # 画像 active 键
            "SELECT prop_key FROM user_profile WHERE status = 'active'"
        ).fetchall()}  # 键集合
    except Exception:  # 画像未建
        profile_keys = set()  # 空
    if profile_keys:  # 有画像键
        fact_rows = get_connection().execute(  # 长期事实里与画像键重叠的
            "SELECT memory_key FROM user_memories WHERE status = 'active'"
        ).fetchall()
        for row in fact_rows:  # 逐条
            key = str(row["memory_key"] or "")  # 键
            if key in profile_keys:  # 与画像键重叠 → 画像优先，事实降权
                get_connection().execute(  # 降权 0.8（不删，可回查）
                    "UPDATE user_memories SET confidence = MAX(0.1, confidence * 0.8) WHERE memory_key = ? AND status = 'active'",
                    (key,),
                )  # 更新
                counts["fact_demoted"] += 1  # 计
    # ③ 裁剪超层版本历史
    show = get_connection().execute(  # 取带套嵌历史的事实行
        "SELECT memory_key, history_versions FROM user_memories WHERE status = 'active' AND history_versions != '[]'"
    ).fetchall()
    for row in show:  # 逐行
        versions = json.loads(row["history_versions"] or "[]")  # 解析
        if len(versions) > 10:  # 超 10 层
            trimmed = versions[-10:]  # 留最近 10
            get_connection().execute(  # 写回
                "UPDATE user_memories SET history_versions = ? WHERE memory_key = ? AND status = 'active'",
                (json.dumps(trimmed, ensure_ascii=False), row["memory_key"]),
            )  # 更新
            counts["version_trimmed"] += 1  # 计
    if any(counts.values()):  # 有动作
        get_connection().commit()  # 提交
    return counts  # 返回
