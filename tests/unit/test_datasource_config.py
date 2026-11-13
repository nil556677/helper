"""真实数据源：未配置时优雅报错（不真连）。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑异步

import pytest  # 断言异常

from app.agent.tools.tooling import ToolError  # 业务错
from app.services.logistics import client as logistics_client  # 物流
from app.services.order import repo as order_repo  # 订单


def test_order_repo_unconfigured(fake_root) -> None:  # 未配置 MySQL → ToolError
    del fake_root  # 只需隔离库
    order_repo.set_order_repo(None)  # 用默认实现
    with pytest.raises(ToolError):  # 期望报错
        asyncio.run(order_repo.get_order_repo().get_order_detail("12345"))  # 查


def test_logistics_unconfigured(fake_root) -> None:  # 未配置百度快递 → ToolError
    del fake_root  # 只需隔离库
    logistics_client.set_provider(None)  # 用默认实现
    with pytest.raises(ToolError):  # 期望报错
        asyncio.run(logistics_client.get_provider().trace("SF123"))  # 查


def test_env_fallback_config(fake_root, monkeypatch) -> None:  # 后台配置缺时走环境变量
    """测环境兜底需要一块"没配 mysql host"的隔离库——直接读真实 DB 会被实配的 host 干扰。"""
    from app.core.db import schema_v1  # 建表
    from app.core.db.connection import connect  # 连接

    conn = connect()  # 连接
    try:  # 建空 settings 表（隔离库，不读真实 DB）
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关
    monkeypatch.delenv("AGENT_MYSQL_HOST", raising=False)  # 清
    monkeypatch.setenv("AGENT_MYSQL_HOST", "db.local")  # 设
    monkeypatch.setenv("AGENT_LOGISTICS_ENDPOINT", "https://baidu.example")  # 设
    from app.services.logistics.client import _logistics_config  # 配置
    from app.services.order.repo import _mysql_config  # 配置

    m = _mysql_config()  # mysql
    assert m["host"] == "db.local"  # 环境兜底生效
    lc = _logistics_config()  # 物流
    assert lc["endpoint"] == "https://baidu.example"  # 环境兜底生效
