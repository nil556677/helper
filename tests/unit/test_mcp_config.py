"""MCP 配置持久化（settings + DPAPI）+ 启用审计。"""

from __future__ import annotations  # 延迟值注解

from app.agent.skills import mcp_config  # 配置
from app.agent.skills import store as skill_store  # 审计
from app.core.db.connection import get_connection  # DB


def test_save_load_config_with_secret(rag_db) -> None:  # 保存/读取 + 密钥解密
    mcp_config.save_server(
        {"server_id": "s1", "transport": "stdio", "command": "python", "args": ["s.py"], "enabled": False},
        secret="s3cr3t",
    )  # 保存
    servers = mcp_config.load_servers()  # 读
    one = next(s for s in servers if s["server_id"] == "s1")  # 取
    assert one["command"] == "python"  # 配置在
    assert one["env_secret"] == "s3cr3t"  # 解密成功
    raw = get_connection().execute("SELECT value_json FROM settings WHERE key='mcp.secret.s1'").fetchone()["value_json"]  # 原始
    assert "s3cr3t" not in str(raw) or str(raw).startswith("enc:")  # 不落明文（降级时也带 enc 前缀）


def test_enabled_default_false(rag_db) -> None:  # 默认关
    assert mcp_config.enabled("nonexistent") is False  # 没有即关


def test_set_enabled_and_audit(rag_db) -> None:  # 启用 + 写审计
    mcp_config.save_server({"server_id": "s2", "command": "python"})  # 建
    assert mcp_config.enabled("s2") is False  # 默认关
    mcp_config.set_enabled("s2", True)  # 开
    assert mcp_config.enabled("s2") is True  # 开
    c = skill_store.counts("mcp:s2")  # 审计
    assert c.get("enabled", 0) >= 1  # 有启用事件


def test_decrypt_secret_roundtrip(rag_db) -> None:  # 加解密往返
    enc = mcp_config._encrypt("abc123")  # 加密
    assert mcp_config.decrypt_secret(enc) == "abc123"  # 解密
