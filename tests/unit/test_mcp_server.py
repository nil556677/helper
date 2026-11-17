"""MCP Provider P2：导出白名单 / 服务端确认 / 脱敏 / 审计。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步
from types import SimpleNamespace  # 假工具

from app.agent.skills import mcp_server  # 被测
from app.core.error import ok  # 成功包络


def _fake_tools() -> dict[str, object]:  # 假目录
    async def _read(*a, **k):  # 只读工具
        del a, k  # 不用
        return ok({"phone": "15196630910", "status": "已发货"})  # 含 PII

    async def _manual(*a, **k):  # 需确认工具
        del a, k  # 不用
        return ok({"done": True})  # 结果

    read = SimpleNamespace(  # 只读工具
        name="fake_read",  # 名
        description="读工具",  # 描述
        args_schema=None,  # 无契约
        metadata={"dsh": {"approval": "none", "mutation": "read_only", "risk": "low"}},  # 元数据
        ainvoke=_read,  # 调用
    )  # 结束
    manual = SimpleNamespace(  # 需确认工具
        name="fake_manual",  # 名
        description="写工具",  # 描述
        args_schema=None,  # 无契约
        metadata={"dsh": {"approval": "manual", "mutation": "read_only", "risk": "medium"}},  # 元数据
        ainvoke=_manual,  # 调用
    )  # 结束
    return {"fake_read": read, "fake_manual": manual}  # 返回


def _patch(monkeypatch, tools, enabled: str, allow_mutation: bool = False) -> None:  # 打桩公共
    monkeypatch.setattr(mcp_server, "_catalog", lambda: tools)  # 假目录
    monkeypatch.setenv("HELPER_MCP_SERVER_ENABLED", enabled)  # 白名单
    if allow_mutation:  # 放行写
        monkeypatch.setenv("HELPER_MCP_ALLOW_MUTATION", "1")  # 开
    else:  # 默认不放行
        monkeypatch.delenv("HELPER_MCP_ALLOW_MUTATION", raising=False)  # 清


def test_exported_tools_only_allowlisted(monkeypatch) -> None:  # 只导出白名单内且存在的
    _patch(monkeypatch, _fake_tools(), "fake_read,no_such")  # 白名单
    items = mcp_server.exported_tools()  # 导出
    assert [i["name"] for i in items] == ["fake_read"]  # 只有一个


def test_handle_call_not_allowed(monkeypatch, rag_db) -> None:  # 不在白名单拒绝
    _patch(monkeypatch, _fake_tools(), "fake_read")  # 只开 fake_read
    out = asyncio.run(mcp_server.handle_call("fake_manual", {}))  # 调未白名单
    assert out["ok"] is False and out["error"] == "not_allowed"  # 拒


def test_handle_call_needs_confirmation_for_manual(monkeypatch, rag_db) -> None:  # 写工具默认需确认拒
    _patch(monkeypatch, _fake_tools(), "fake_manual")  # 白名单含写工具
    out = asyncio.run(mcp_server.handle_call("fake_manual", {}))  # 调
    assert out["ok"] is False and out["error"] == "needs_confirmation"  # 拒并提示


def test_handle_call_read_executes_and_redacts(monkeypatch, rag_db) -> None:  # 只读执行 + PII 脱敏 + 审计
    _patch(monkeypatch, _fake_tools(), "fake_read")  # 只读
    out = asyncio.run(mcp_server.handle_call("fake_read", {"order_id": "123"}))  # 调
    assert out["ok"] is True  # 成功
    assert "已发货" in out["content"]  # 有内容
    assert "15196630910" not in out["content"]  # 手机号被脱敏
    from app.core.db.connection import get_connection  # DB

    rows = get_connection().execute("SELECT tool_name, ok FROM mcp_calls ORDER BY id DESC LIMIT 1").fetchall()  # 审计
    assert rows and rows[0]["tool_name"] == "fake_read"  # 有审计行


def test_build_fastmcp_tolerant(monkeypatch) -> None:  # fastmcp 缺失返回 None；存在返回对象
    _patch(monkeypatch, _fake_tools(), "fake_read")  # 白名单
    srv = mcp_server.build_fastmcp()  # 建
    assert srv is None or hasattr(srv, "tool")  # 缺失 None / 存在对象均可


def test_counts_metrics(monkeypatch, rag_db) -> None:  # 调用指标聚合
    _patch(monkeypatch, _fake_tools(), "fake_read,fake_manual")  # 白名单
    asyncio.run(mcp_server.handle_call("fake_read", {}))  # 成功一次
    asyncio.run(mcp_server.handle_call("fake_manual", {}))  # 需确认（记失败）
    c = mcp_server.counts()  # 指标
    assert c["total"] >= 2  # 调用记录
    assert c["ok"] >= 1 and c["failed"] >= 1  # 成败都有
    assert "fake_read" in c["by_tool"]  # 分工具
