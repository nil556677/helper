"""记忆服务门面：UI/设置页只经本模块访问记忆域（对齐"UI 不得直连领域包"铁律）。

当前接 `app/agent/memory/manager`（agent 内收口层）；P2 画像域包落位后，画像读写也在此扩展。
对外只暴露幂等、可观测的安全操作；不做任何 DB 直连。
"""

from __future__ import annotations  # 延迟值注解

__all__ = [  # 导出
    "delete_all",  # 清空全部
    "delete_for_conversation",  # 按会话删除
    "list_conflicts",  # 冲突条目
    "profile_confirm",  # 画像显式更正
    "profile_delete",  # 画像归档
    "profile_list",  # 画像条目
    "resolve_conflict",  # 冲突复核
    "stats",  # 记忆统计
]  # 结束


def stats() -> dict:  # 记忆统计（观测）
    """记忆条目规模 + 画像条目数 + 冲突治理统计（设置页/界面显示用）。"""
    from app.agent.memory import manager  # 统一入口
    from app.core.db.connection import get_connection  # DB 计数

    active = 0  # 活跃
    archived = 0  # 归档
    contested = 0  # 冲突
    profile_n = 0  # 画像条目
    try:  # 无表/坏库安全
        row = get_connection().execute(  # 按状态计数
            "SELECT status, COUNT(*) AS n FROM user_memories GROUP BY status"
        ).fetchall()
        for item in row:  # 逐状态
            if item["status"] == "active":  # 活跃
                active = int(item["n"])  # 计
            elif item["status"] == "archived":  # 归档
                archived = int(item["n"])  # 计
            elif item["status"] == "contested":  # 冲突
                contested = int(item["n"])  # 计
    except Exception:  # 未初始化
        pass  # 全 0（变量已在函数头初始化）
    try:  # 画像计数（画像表可能未建 → 默认 0；需保留默认值而非静默 suppress）  # noqa: SIM105
        profile_n = int(get_connection().execute(  # 查 active 画像数
            "SELECT COUNT(*) AS n FROM user_profile WHERE status = 'active'"
        ).fetchone()["n"] or 0)  # 计数
    except Exception:  # 画像表未建
        pass  # 0（保持默认）
    return {  # 汇总
        "active": active,  # 活跃
        "archived": archived,  # 归档
        "contested": contested,  # 冲突
        "profile_count": profile_n,  # 画像条目
        "conflict_rate": manager.conflict_stats(),  # 治理计数（进程内）
    }  # 结束


def list_conflicts(*, limit: int = 50) -> list[dict]:  # 冲突条目
    """contested 条目列表（键/值/置信度/来源；P4.1 复核 UI 消费）。"""
    from app.core.db.connection import get_connection  # DB

    try:  # 无表/坏库安全
        rows = get_connection().execute(  # 查冲突
            "SELECT memory_key, value, confidence, source, updated_at FROM user_memories"
            " WHERE status = 'contested' ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]  # 返回
    except Exception:  # 未初始化
        return []  # 空


def delete_all(*, conversation_id: str = "") -> int:  # 清空记忆
    """清空全部记忆（或某会话子集）；合规删除入口，写 trace 由调用方负责。"""
    from app.agent.memory import manager  # 统一入口

    return manager.delete_all(conversation_id=conversation_id)  # 委托


def delete_for_conversation(conversation_id: str) -> int:  # 按会话删除
    """删除某会话相关的记忆（P4 完善级联）。"""
    from app.agent.memory import manager  # 统一入口

    return manager.delete_for_conversation(conversation_id)  # 委托


def profile_list() -> list[dict]:  # 画像条目（设置页展示）
    """当前 active 画像条目（键/值/来源/置信度/更新时间）。"""
    from app.memory.model import default_namespace  # 画像作用域
    from app.memory.store import ProfileStore  # 画像仓储

    try:  # 未初始化安全
        entries = ProfileStore(default_namespace()).get_all()  # 取 active
        return [  # 转展示 dict
            {  # 一条
                "key": e.prop_key,  # 键
                "value": e.value,  # 值
                "source": e.source,  # 来源
                "confidence": float(e.confidence or 0),  # 可信度
                "version": int(e.version or 1),  # 版本
            }  # 结束
            for e in entries  # 逐条
        ]  # 返回
    except Exception:  # 未初始化
        return []  # 空


def profile_confirm(prop_key: str, value: object) -> None:  # 画像显式更正
    """用户显式更正某画像键（最高优先级覆盖；设置页编辑/确认用）。"""
    from app.agent.memory import manager  # 统一入口

    manager.confirm_profile(prop_key, value)  # 委托


def profile_delete(prop_key: str) -> None:  # 画像归档
    """归档某画像键（设置页删除用）。"""
    from app.agent.memory import manager  # 统一入口

    manager.delete_profile(prop_key)  # 委托


def resolve_conflict(memory_key: str, *, keep_new: bool) -> None:  # 冲突复核
    """人工复核 contested 条目：keep_new=True 采用新值强制覆盖；False 归档 contest。

    数据源 = `list_conflicts`（contested 行）。复核后 contested 行归档，不再悬置。
    """
    import time as _time  # 时间戳

    from app.core.db.connection import transaction  # 事务

    key = str(memory_key or "")  # 冲突键
    if not key:  # 防御
        return  # 跳过
    if not key.endswith("#contested"):  # 规范化冲突槽名
        key = f"{key}#contested"  # 补槽名
    base = key[: -len("#contested")]  # 对应 active 主键
    with transaction() as conn:  # 单事务
        rows = conn.execute(  # 取该 contested 行
            "SELECT memory_key, value, source, confidence, created_at FROM user_memories WHERE memory_key = ? AND status = 'contested' ORDER BY updated_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        if rows is None:  # 无冲突行
            return  # 无事
        if keep_new:  # 采用新值：归档旧 active，用 contest 值顶替
            conn.execute(  # 旧 active 标 superseded
                "UPDATE user_memories SET status = 'superseded' WHERE memory_key = ? AND status = 'active'",
                (base,),
            )  # 更新
            ns = conn.execute(  # 取原命名空间（对齐行）
                "SELECT namespace FROM user_memories WHERE memory_key = ? AND status = 'contested' LIMIT 1",
                (key,),
            ).fetchone()["namespace"]  # 作用域
            conn.execute(  # 新 active（沿用 contest 值/来源/置信）
                "INSERT OR REPLACE INTO user_memories(namespace, memory_key, value, source, created_at, updated_at,"
                " confidence, access_count, last_access, status, embedding, history_versions)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'active', NULL, '[]')",
                (ns, base, rows["value"], rows["source"] or "paraphrase", rows["created_at"], _time.time(),
                 float(rows["confidence"] or 0.3)),
            )  # 插入
        conn.execute(  # 冲突槽归档（无论是否采用）
            "UPDATE user_memories SET status = 'archived', archived_at = ? WHERE memory_key = ? AND status = 'contested'",
            (_time.time(), key),
        )  # 更新
