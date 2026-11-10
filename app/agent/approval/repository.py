"""审批落表：幂等插入 + 乐观决策 + 审计（sqlite）。"""

from __future__ import annotations  # 延迟值注解

import hashlib  # 稳定幂等键（sha256，跨进程重启一致）
import json  # 序列化明细
import time  # 时间戳
import uuid  # 请求 id
from typing import Any  # 任意值

from app.core.db.connection import get_connection, transaction  # DB

__all__ = ["ApprovalConflict", "create_request", "decide", "expire_pending", "insert_audit", "list_pending"]  # 导出


class ApprovalConflict(Exception):  # 并发/已处理
    """该请求已被处理或已超时（乐观并发）。"""


def _stamp() -> float:  # 当前秒
    return time.time()  # 秒


def create_request(  # 创建审批请求（幂等）
    *,  # 关键字
    thread_id: str,  # 线程
    tenant_id: str,  # 租户
    requester_id: str,  # 发起人
    tool_name: str,  # 工具
    args_redacted: dict[str, Any],  # 脱敏参数
    resource_summary: str,  # 人话摘要
    mutation: str,  # 变更画像
    risk: str,  # 风险
    approval_reason: str,  # 原因
    expires_at: float,  # 过期
    args_hash: str | None = None,  # 幂等键（缺省按内容算）
    checkpoint_ns: str = "",  # 检查点命名空间
    trace_id: str = "",  # 追踪
) -> str:  # 请求 id（新增或已有）
    """同 (thread, ns, 参数hash) 的 pending 只落一条；返回该 id（幂等）。"""
    # 幂等键必须跨进程重启稳定：Python 内置 hash() 受 PYTHONHASHSEED 随机化影响，
    # 重启后同参数会算出不同键 → 幂等去重失效；改用 sha256 保证文本指纹确定。
    hash_key = args_hash or hashlib.sha256(  # 参数指纹（确定、跨重启一致）
        json.dumps(args_redacted, sort_keys=True, ensure_ascii=False).encode("utf-8")  # 规范 JSON 的字节
    ).hexdigest()  # 十六进制串
    rid = str(uuid.uuid4())  # 请求 id
    now = _stamp()  # 时间
    with transaction() as conn:  # 单事务
        conn.execute(  # 幂等插入（冲突则不动）
            "INSERT OR IGNORE INTO approval_requests(request_id, thread_id, checkpoint_ns, trace_id, tenant_id,"
            " requester_id, tool_name, args_hash, args_redacted, resource_summary,"
            " mutation, risk, approval_reason, status, expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (rid, thread_id, checkpoint_ns, trace_id, tenant_id, requester_id, tool_name, hash_key,
             json.dumps(args_redacted, ensure_ascii=False), resource_summary, mutation, risk, approval_reason,
             expires_at, now, now),
        )  # 插入
        row = conn.execute(  # 读回（同幂等键已有则返回旧）
            "SELECT request_id FROM approval_requests"
            " WHERE thread_id=? AND checkpoint_ns=? AND args_hash=? AND status='pending'"
            " ORDER BY created_at LIMIT 1",
            (thread_id, checkpoint_ns, hash_key),
        ).fetchone()  # 命中
    inserted = row["request_id"] if row else rid  # 实际 id
    insert_audit(inserted, "created", requester_id)  # 审计（即使幂等也记录？幂等时也可记，保持可观测）
    return inserted  # 返回


def decide(  # 决策（乐观并发）
    request_id: str,  # 请求
    action: str,  # approved / rejected / expired
    decided_by: str,  # 决策人
    note: str = "",  # 备注
    edited_args: dict | None = None,  # 编辑参数
) -> None:  # 结束
    """只更新 pending → 目标状态；不是 pending 则 Conflict（乐观并发）。"""
    with transaction() as conn:  # 单事务
        cur = conn.execute(  # 条件更新
            "UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, decision_note=?, edited_args=? "
            "WHERE request_id=? AND status='pending'",
            (action, decided_by, _stamp(), note, json.dumps(edited_args or {}) if edited_args is not None else None, request_id),
        )  # 更新
        if cur.rowcount == 0:  # 非 pending（已处理/超时）
            raise ApprovalConflict("该请求已被处理或已超时")  # 抛
    insert_audit(request_id, action, decided_by, {"note": note})  # 审计


def insert_audit(  # 追加审计
    request_id: str,  # 请求
    event: str,  # 事件
    actor_id: str,  # 操作者
    detail: dict | None = None,  # 明细
) -> None:  # 结束
    with transaction() as conn:  # 单事务
        conn.execute(  # 追加（只 INSERT）
            "INSERT INTO approval_audit(request_id, event, actor_id, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, event, actor_id, json.dumps(detail or {}, ensure_ascii=False), _stamp()),
        )  # 插入


def list_pending(tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:  # 待审列表
    """按租户取 pending，新的在前。"""
    rows = get_connection().execute(  # 查询
        "SELECT request_id, tool_name, resource_summary, risk, expires_at, created_at"
        " FROM approval_requests WHERE tenant_id=? AND status='pending' ORDER BY created_at DESC LIMIT ?",
        (tenant_id, int(limit)),
    ).fetchall()  # 全部
    return [dict(row) for row in rows]  # 列表


def list_requests(*, limit: int = 100) -> list[dict[str, Any]]:  # 台账（只读）
    """按时间倒序列出审批请求（含状态/决策人），供后台只读台账。"""
    rows = get_connection().execute(  # 查询
        "SELECT request_id, tool_name, resource_summary, risk, status, created_at, decided_by, decided_at"
        " FROM approval_requests ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()  # 全部
    return [dict(row) for row in rows]  # 列表


def get_request(request_id: str) -> dict[str, Any] | None:  # 取单条请求
    """按 id 取审批请求（含 args/status）。"""
    row = get_connection().execute(  # 查询
        "SELECT request_id, thread_id, tenant_id, requester_id, tool_name, args_redacted, edited_args, status"
        " FROM approval_requests WHERE request_id=?",
        (request_id,),
    ).fetchone()  # 一行
    if row is None:  # 无
        return None  # 空
    return dict(row)  # 返回


def has_executed(request_id: str) -> bool:  # 是否已执行过（幂等）
    """审计里是否已有 executed 事件。"""
    row = get_connection().execute(  # 查询
        "SELECT 1 FROM approval_audit WHERE request_id=? AND event='executed' LIMIT 1",
        (request_id,),
    ).fetchone()  # 命中
    return row is not None  # 判


def expire_pending(now: float | None = None) -> list[str]:  # 超时扫描
    """把过期的 pending 标 expired 并审计；返回 request_id 供唤醒图。"""
    stamp = _stamp() if now is None else float(now)  # 当前
    expired: list[str] = []  # 结果
    with transaction() as conn:  # 单事务
        rows = conn.execute(  # 标记过期
            "UPDATE approval_requests SET status='expired', decided_at=?, updated_at=? "
            "WHERE status='pending' AND expires_at < ? RETURNING request_id",
            (stamp, stamp, stamp),
        ).fetchall()  # 过期行
        expired = [row["request_id"] for row in rows]  # id 列表
    for rid in expired:  # 逐条审计
        insert_audit(rid, "expired", "system")  # 记
    return expired  # 返回