"""MCP Provider(P2)：把内置 `tool_catalog` 工具导出成 MCP server（默认只读白名单）。

服务端四道闸：① allowlist（`HELPER_MCP_SERVER_ENABLED`）② 写/需确认工具默认拒绝
（`HELPER_MCP_ALLOW_MUTATION` 显式开；无 UI 不静默执行）③ 输出 PII 脱敏 ④ 调用审计（mcp_calls 只追加）。
`build_fastmcp()` 用框架 `fastmcp` 薄接线；本模块核心逻辑（exported_tools/handle_call）可单测。
"""

from __future__ import annotations  # 延迟值注解

import json  # 序列化
import os  # 门控
import time  # 时间戳
from typing import Any  # 任意值

from app.core.db.connection import transaction  # DB

__all__ = ["build_fastmcp", "counts", "exported_tools", "handle_call"]  # 导出

#: 调用审计表（只追加）。
_DDL = (  # DDL
    "CREATE TABLE IF NOT EXISTS mcp_calls ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"  # id
    "  tool_name TEXT NOT NULL,"  # 工具
    "  ok INTEGER NOT NULL,"  # 是否成功
    "  error TEXT DEFAULT '',"  # 错误
    "  args_redacted TEXT DEFAULT '{}',"  # 脱敏参数
    "  created_at REAL NOT NULL"  # 秒
    ")",
    "CREATE INDEX IF NOT EXISTS idx_mcp_calls_tool ON mcp_calls(tool_name, created_at)",  # 按工具审计
)  # 结束


def allowed_names() -> list[str]:  # 导出白名单（默认空）
    raw = str(os.environ.get("HELPER_MCP_SERVER_ENABLED", "")).strip()  # 环境
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else []  # 列表


def _mutation_allowed() -> bool:  # 是否放行写/需确认工具（默认否）
    return str(os.environ.get("HELPER_MCP_ALLOW_MUTATION", "")).strip().lower() in ("1", "true", "on", "yes")  # 判断


def _catalog() -> dict[str, Any]:  # 本地工具目录（可打桩）
    from app.agent.tools.catalog import tool_catalog  # 目录

    return tool_catalog()  # 返回


def exported_tools() -> list[dict[str, Any]]:  # 导出给 MCP 的工具清单
    """只列 allowlist 内、且确实存在的工具；带 description + args JSON schema。"""
    cat = _catalog()  # 目录
    out: list[dict[str, Any]] = []  # 结果
    for name in allowed_names():  # 白名单
        tool = cat.get(name)  # 取工具
        if tool is None:  # 不存在
            continue  # 跳过
        out.append({  # 导出项
            "name": name,  # 名
            "description": str(getattr(tool, "description", "") or name),  # 描述
            "args_schema": _schema_of(tool),  # 参数 schema
        })  # 结束
    return out  # 返回


async def handle_call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:  # 处理一次 MCP 调用
    """allowlist → 需确认(写)默认拒 → 执行 → 脱敏 → 审计。返回 {ok, content|error}。"""
    args = dict(args or {})  # 参数
    if name not in allowed_names():  # 不在白名单
        return {"ok": False, "error": "not_allowed"}  # 拒
    tool = _catalog().get(name)  # 工具
    if tool is None:  # 不存在
        return {"ok": False, "error": "not_found"}  # 拒
    from app.agent.gateway.redact import redact_args  # 脱敏
    from app.agent.react.policies.approval import should_require_approval  # 确认判定
    from app.agent.tools.tooling import unwrap_tool_output  # 还原包络
    from app.agent.tools.wiring import describe  # 描述符

    _ensure_table()  # 建审计表
    need, reason = should_require_approval(describe(tool), args=args)  # 是否需确认
    if need and not _mutation_allowed():  # 需确认且未放行
        _record_call(name, False, "needs_confirmation", args)  # 审计
        return {"ok": False, "error": "needs_confirmation", "reason": reason}  # 不静默执行
    try:  # 执行
        env = unwrap_tool_output(await tool.ainvoke(args))  # 执行
        ok = bool(getattr(env, "ok", False))  # 是否成功
        data = getattr(env, "data", None)  # 数据
        safe = redact_args(data) if isinstance(data, dict) else data  # 先按字段脱敏（PII）
        text = _to_text(safe) if ok else ""  # 文本（已脱敏）
        _record_call(name, ok, "", args)  # 审计
        return {"ok": ok, "content": text}  # 返回
    except Exception as exc:  # 失败
        _record_call(name, False, str(exc)[:200], args)  # 审计
        return {"ok": False, "error": str(exc)[:200]}  # 返回


def build_fastmcp() -> Any | None:  # 用 fastmcp 薄接线（缺依赖返回 None）
    """把 exported_tools 注册到 FastMCP；每个工具转发到 handle_call。"""
    try:  # 探测依赖
        from fastmcp import FastMCP  # 框架
    except Exception:  # 缺依赖
        return None  # 降级
    mcp = FastMCP("helper")  # 服务器
    for item in exported_tools():  # 逐工具
        name = item["name"]  # 名

        async def _call(_name: str = name, **kwargs: Any) -> Any:  # 转发
            return await handle_call(_name, kwargs)  # 调核心

        mcp.tool(name=name, description=item["description"])(_call)  # 注册
    return mcp  # 返回


def counts() -> dict[str, Any]:  # 调用指标（按工具/成功失败聚合）
    """从 mcp_calls 汇总：{total, ok, failed, by_tool:{tool:{ok,failed}}}。"""
    _ensure_table()  # 建表
    from app.core.db.connection import get_connection  # DB

    rows = get_connection().execute(  # 聚合
        "SELECT tool_name, ok, COUNT(*) AS c FROM mcp_calls GROUP BY tool_name, ok"
    ).fetchall()  # 行
    by_tool: dict[str, dict[str, int]] = {}  # 分工具
    total = ok = failed = 0  # 总计
    for r in rows:  # 逐行
        name = str(r["tool_name"])  # 工具
        cnt = int(r["c"])  # 次数
        total += cnt  # 总
        bucket = by_tool.setdefault(name, {"ok": 0, "failed": 0})  # 桶
        if int(r["ok"]):  # 成功
            ok += cnt  # 总成功
            bucket["ok"] += cnt  # 工具成功
        else:  # 失败
            failed += cnt  # 总失败
            bucket["failed"] += cnt  # 工具失败
    return {"total": total, "ok": ok, "failed": failed, "by_tool": by_tool}  # 返回


def _schema_of(tool: Any) -> dict[str, Any]:  # 参数 JSON schema
    schema = getattr(tool, "args_schema", None)  # 契约
    if isinstance(schema, type) and hasattr(schema, "model_json_schema"):  # pydantic
        try:  # 生成
            return schema.model_json_schema()  # schema
        except Exception:  # 失败
            return {}  # 空
    return {}  # 无


def _to_text(data: Any) -> str:  # data → 文本
    if isinstance(data, str):  # 字符串
        return data  # 原样
    if hasattr(data, "model_dump_json"):  # pydantic
        return str(data.model_dump_json())  # 序列化
    try:  # JSON
        return json.dumps(data, ensure_ascii=False, default=str)  # 序列化
    except Exception:  # 失败
        return str(data)  # 兜底


def _ensure_table() -> None:  # 幂等建审计表
    with transaction() as conn:  # 事务
        for stmt in _DDL:  # 逐条
            conn.execute(stmt)  # 执行


def _record_call(tool_name: str, ok: bool, error: str, args: dict[str, Any]) -> None:  # 记调用审计
    from app.agent.gateway.redact import redact_args  # 脱敏

    try:  # 审计失败不阻断
        with transaction() as conn:  # 事务
            conn.execute(  # 插入
                "INSERT INTO mcp_calls(tool_name, ok, error, args_redacted, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(tool_name), 1 if ok else 0, str(error),  # 基础
                 json.dumps(redact_args(args), ensure_ascii=False), time.time()),  # 参数
            )  # 插入
    except Exception:  # 忽略
        return  # 忽略
