"""MCP Consumer(P1)：把外部 MCP server 的工具导入 `skill_registry.EXTRA_TOOLS`，供 skill 引用。

框架现成：`langchain_mcp_adapters` 的 `MultiServerMCPClient` / `get_tools()`，不自己写协议。
本模块只做四件事：可用性探测(缺依赖降级) / 工具元数据翻译(补 `dsh`，走既有审批·脱敏·审计) /
注册进 `EXTRA_TOOLS` / 门控(默认关)。远端结果一律当"不可信数据"处理（出口过 `gateway.redact`）。
"""

from __future__ import annotations  # 延迟值注解

import os  # 门控
from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 校验

__all__ = ["McpServerConfig", "McpUnavailable", "adopt", "available", "gated", "load_tools", "register"]  # 导出


class McpServerConfig(BaseModel):  # 一个 MCP server 的连接配置
    server_id: str = Field(default="mcp", description="server id（标识）")  # id
    transport: str = Field(default="stdio", description="stdio 或 streamable_http")  # 传输
    command: str = Field(default="", description="stdio：可执行命令")  # 命令
    args: list[str] = Field(default_factory=list, description="stdio：命令参数")  # 参数
    url: str = Field(default="", description="streamable_http：端点")  # 地址
    mutation: str = Field(default="read_only", description="远端工具默认变更画像（写工具需白名单显式标写）")  # 变更
    approval: str = Field(default="none", description="默认审批策略（读不审、写要审）")  # 审批
    risk: str = Field(default="low", description="默认风险")  # 风险
    timeout_ms: int = Field(default=5000, description="默认工具超时")  # 超时


class McpUnavailable(RuntimeError):  # 依赖缺失
    """未安装 mcp / langchain_mcp_adapters 时抛；调用方应禁用该 server 并提示，不崩。"""


def available() -> bool:  # 依赖是否可导入（含真正的客户端模块，避免"顶层能导、用时才炸"）
    try:  # 探测
        import mcp  # noqa: F401  仅探测
        from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: F401  实际使用点
        return True  # 可用
    except Exception:  # 缺失/版本不兼容
        return False  # 不可用


def _make_client(spec: dict[str, Any]) -> Any:  # 构造适配器客户端（可被测试打桩）
    """正确子模块导入（`langchain_mcp_adapters.client`）；不在此模块顶层 import，避免缺依赖就崩。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient  # 适配器

    return MultiServerMCPClient(spec)  # 客户端


def gated(server_id: str) -> bool:  # 门控（环境白名单 或 持久化启用位；默认关）
    raw = str(os.environ.get("HELPER_MCP_ENABLED", "")).strip()  # 环境白名单
    if raw and server_id in {x.strip() for x in raw.split(",") if x.strip()}:  # 环境命中
        return True  # 开
    try:  # 持久化启用位（settings）
        from app.agent.skills import mcp_config  # 配置

        return mcp_config.enabled(server_id)  # 持久化启用
    except Exception:  # DB 未就绪等
        return False  # 关


def register(tools: list[Any]) -> dict[str, Any]:  # 注册进 EXTRA_TOOLS（供 skill 引用）
    from app.agent.skills.registry import EXTRA_TOOLS  # 注入缝

    for t in tools:  # 逐个
        name = getattr(t, "name", None)  # 工具名
        if name:  # 有名
            EXTRA_TOOLS[name] = t  # 注册
    return EXTRA_TOOLS  # 返回


def adopt(remote: Any, *, config: McpServerConfig | None = None) -> Any:  # 包装 + 元数据翻译(补 dsh)
    """把远端工具包成带 `dsh` 元数据的 StructuredTool，让审批/脱敏/审计对它生效。"""
    cfg = config or McpServerConfig()  # 默认
    name = str(getattr(remote, "name", None) or "mcp_tool")  # 名
    desc = str(getattr(remote, "description", None) or name)  # 描述
    schema = getattr(remote, "args_schema", None)  # 参数契约（适配器会带）
    from langchain_core.tools import StructuredTool  # 工具

    async def _run(**kwargs: Any) -> Any:  # 转发给远端
        coro = remote.ainvoke(kwargs)  # 远端调用（可 await）
        return await coro  # 取结果

    tool_kwargs: dict[str, Any] = {  # 构造参数
        "name": name,  # 名
        "description": desc,  # 描述
        "coroutine": _run,  # 异步入口
        "metadata": {  # 项目元数据（供 describe/审批/审计）
            "dsh": {  # 元数据
                "mutation": str(cfg.mutation),  # 变更
                "approval": str(cfg.approval),  # 审批
                "risk": str(cfg.risk),  # 风险
                "version": 1,  # 版本
                "timeout_ms": int(cfg.timeout_ms),  # 超时
                "result_schema": None,  # 远端不保证形状，不强校验
            },  # 结束
        },  # 结束
    }  # 结束
    if isinstance(schema, type) and issubclass(schema, BaseModel):  # 有合法契约
        tool_kwargs["args_schema"] = schema  # 带契约
    return StructuredTool(**tool_kwargs)  # 返回包装


async def load_tools(config: McpServerConfig) -> list[Any]:  # 从 MCP server 装载工具
    """打开适配器客户端(get_tools) → adopt 翻译 + 注册；缺依赖抛 McpUnavailable。"""
    if not available():  # 缺依赖
        raise McpUnavailable("未安装 langchain_mcp_adapters / mcp（或版本不兼容）")  # 提示
    spec: dict[str, Any] = {}  # 连接描述
    if config.transport == "stdio":  # 本地子进程
        spec = {"command": config.command, "args": list(config.args), "transport": "stdio"}  # 子进程
    else:  # HTTP
        spec = {"url": str(config.url), "transport": "streamable_http"}  # HTTP
    remote_tools: list[Any] = []  # 远端
    async with _make_client({config.server_id: spec}) as client:  # 连接（客户端工厂可被打桩）
        remote_tools = await client.get_tools()  # 取工具
    return [adopt(t, config=config) for t in remote_tools]  # 翻译（注册由调用方决定）
