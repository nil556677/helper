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
        asyncio.run(order_repo.get_order_repo().get_order("12345"))  # 查


def test_logistics_unconfigured(fake_root) -> None:  # 未配置百度快递 → ToolError
    del fake_root  # 只需隔离库
    logistics_client.set_provider(None)  # 用默认实现
    with pytest.raises(ToolError):  # 期望报错
        asyncio.run(logistics_client.get_provider().trace("SF123"))  # 查