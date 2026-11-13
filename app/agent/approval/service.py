"""审批服务：列表/决策/超时扫描/批准后执行（薄封装仓储，供 API/UI/定时任务调用）。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.approval import repository  # 仓储

__all__ = ["catchup_approved", "decide", "execute_approved", "expire_all", "ledger", "pending"]  # 导出


def pending(tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:  # 待审列表
    """按租户取 pending（新的在前）。"""
    return repository.list_pending(tenant_id, limit=limit)  # 委托


def ledger(*, limit: int = 100) -> list[dict[str, Any]]:  # 只读台账
    """按时间倒序列出审批记录（含状态/决策人）。"""
    return repository.list_requests(limit=limit)  # 委托


def decide(request_id: str, action: str, *, decided_by: str, note: str = "") -> None:  # 决策
    """批准/拒绝（乐观并发；已被处理会抛 ApprovalConflict）。"""
    if action not in ("approved", "rejected"):  # 只允许两种
        raise ValueError("action 只能是 approved 或 rejected")  # 拒绝非法
    repository.decide(request_id, action, decided_by, note)  # 委托


def expire_all() -> list[str]:  # 超时扫描
    """把过期 pending 标 expired，返回需唤醒的 request_id。"""
    return repository.expire_pending()  # 委托


async def catchup_approved(*, limit: int = 50) -> list[str]:  # 恢复补跑（崩溃残留）
    """把"已确认但没执行完"的记录补齐执行（幂等，`execute_approved` 不重复跑）。

    场景：用户点了确认、但进程在执行查询前退出 → 记录停在 approved、无 executed。
    启动恢复时把这个补掉，避免这类查询永远卡在 approved。返回实际补跑的 request_id。
    """
    done: list[str] = []  # 实际补跑的 id
    for row in repository.list_approved_unexecuted(limit=limit):  # 逐条残留
        rid = str(row.get("request_id") or "")  # id
        if not rid:  # 无 id
            continue  # 跳过
        try:  # 补执行（幂等）
            out = await execute_approved(rid)  # 执行
            if out.get("executed"):  # 真正跑了
                done.append(rid)  # 记
        except Exception:  # 单条失败不影响其它（best-effort）
            continue  # 下一个
    return done  # 返回


async def execute_approved(request_id: str, *, registry: dict[str, Any] | None = None) -> dict[str, Any]:  # 批准后执行
    """仅 approved 且未执行过才执行工具（幂等）；拒绝/超时/已执行 → 不执行。

    执行参数优先取决策时人工编辑过的 `edited_args`（人类审批可能已校正参数），否则用
    `args_redacted`（创建时快照，含执行所需真实参数）。执行前对能拿到 args_schema 的
    工具做一次 schema 复核（best-effort：拿不到 schema 就跳过），避免用过期/非法参数执行。
    """
    import json as _json  # 解析参数 JSON

    from app.agent.gateway.redact import redact_args  # 审计脱敏视图（读侧不打到执行参数）
    from app.agent.tools.catalog import tool_catalog  # 工具目录
    from app.agent.tools.wiring import unwrap_tool_output  # 还原包络

    row = repository.get_request(request_id)  # 取请求（含 edited_args）
    if row is None:  # 不存在
        return {"executed": False, "reason": "not_found"}  # 返回
    if str(row.get("status") or "") != "approved":  # 非批准
        return {"executed": False, "reason": str(row.get("status") or "")}  # 不执行
    if repository.has_executed(request_id):  # 已执行过（幂等）
        return {"executed": False, "reason": "already"}  # 不重跑
    tools = registry if registry is not None else tool_catalog()  # 工具表
    tool = tools.get(str(row.get("tool_name") or ""))  # 取工具
    if tool is None:  # 工具不存在
        return {"executed": False, "reason": "tool_missing"}  # 返回
    try:  # 解析并复核参数
        edited = row.get("edited_args")  # 人工编辑（JSON 文本或 None）
        base = edited if edited not in (None, "") else (row.get("args_redacted") or "{}")  # 优先人工编辑，否则创建快照
        args = _json.loads(base) if isinstance(base, str) else dict(base or {})  # 解析成 dict
        args = dict(args or {})  # 归一为 dict
        _precheck_args(tool, args, request_id)  # 执行前 schema 复核（最佳努力）
    except Exception as exc:  # 复核失败（含参数非法）
        repository.insert_audit(request_id, "execute_blocked", "system", {"error": str(exc)})  # 审计：被拒
        return {"executed": False, "reason": str(exc)}  # 不执行（返回原因）
    try:  # 执行
        envelope = await tool.ainvoke(args)  # 调工具（真实参数）
        out = unwrap_tool_output(envelope)  # 还原
        # 审计明细只留脱敏参数视图（真实参数不落明细，降低 PII 扩散）。
        repository.insert_audit(  # 记录一次执行
            request_id, "executed", "system",  # 事件/操作者
            {"ok": bool(getattr(out, "ok", False)), "redacted_args": redact_args(args)},  # 状态 + 脱敏视图
        )  # 结束
        return {"executed": True, "ok": bool(getattr(out, "ok", False)), "data": getattr(out, "data", None)}  # 结果
    except Exception as exc:  # 失败
        repository.insert_audit(request_id, "execute_failed", "system", {"error": str(exc)})  # 审计
        return {"executed": False, "reason": str(exc)}  # 返回


def _precheck_args(tool: Any, args: dict[str, Any], request_id: str) -> None:  # noqa: ARG001 — 执行前参数复核
    """用工具描述符的 args_schema 校验参数（best-effort）。

    拿不到 schema（如测试的假工具/未声明契约）就直接放行，不阻断执行；拿到就严格校验，
    参数非法抛异常，由 execute_approved 捕获并按"执行被拒"处理。附带契约 version 只在
    有 version 字段时参考（当前只校验参数形状）。
    """
    try:  # 取描述符（可能无 metadata/schema）
        from app.agent.tools.wiring import describe  # 描述符
        desc = describe(tool)  # 读契约
        schema = desc.args_schema  # 参数契约
    except Exception:  # 描述符取不到
        return  # 跳过复核（放行）
    if schema is None:  # 未声明参数契约
        return  # 跳过
    if isinstance(schema, type) and hasattr(schema, "model_validate"):  # pydantic 模型
        schema.model_validate(args)  # 参数非法会抛（由调用方捕获）
