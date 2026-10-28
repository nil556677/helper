"""工具层：`@tool` 收集、名称校验、全局查重、描述符。"""

from __future__ import annotations  # 类型注解延迟求值

import sys  # 取当前模块
from types import ModuleType, SimpleNamespace  # 模块/假对象

import pytest  # 测试框架
from langchain_core.tools import BaseTool, tool  # 框架工具

from app.agent.contracts import Mutation  # 变更画像
from app.agent.tools import (  # 被测
    DuplicateToolError,  # 重名
    InvalidToolNameError,  # 非法名
    ToolManager,  # 管理器
    collect_tools,  # 收集器
)


@tool
def search_kb(query: str) -> str:  # 检索工具
    """检索本地知识库。"""
    return f"hit:{query}"  # 假结果


@tool
def read_file(path: str) -> str:  # 读文件工具
    """读取用户指定文件。"""
    return path  # 假结果


def _module() -> ModuleType:  # 当前模块
    """收集器只扫本模块。"""
    return sys.modules[__name__]  # 取模块


def test_collect_tools_finds_decorated_only() -> None:  # 收集
    """只收集 `@tool` 实例。"""
    tools = collect_tools(_module())  # 收集
    names = [t.name for t in tools]  # 名称
    assert "search_kb" in names  # 有检索
    assert "read_file" in names  # 有读文件
    assert all(isinstance(t, BaseTool) for t in tools)  # 都是框架工具


def test_register_builds_descriptor() -> None:  # 描述符
    """注册后能从描述符读到名称/描述/参数契约。"""
    manager = ToolManager()  # 管理器
    manager.register(search_kb, mutation=Mutation.READ_ONLY)  # 注册
    assert manager.names() == ["search_kb"]  # 名称
    descriptor = manager.descriptor("search_kb")  # 描述符
    assert descriptor.name == "search_kb"  # 名称一致
    assert descriptor.description  # 有描述
    assert descriptor.args_schema is not None  # 有参数契约


def test_duplicate_tool_rejected() -> None:  # 查重
    """同名工具重复注册即失败。"""
    manager = ToolManager()  # 管理器
    manager.register(search_kb)  # 第一次
    with pytest.raises(DuplicateToolError):  # 期望失败
        manager.register(search_kb)  # 第二次


def test_invalid_name_rejected() -> None:  # 名称合规
    """含点号/大写/超长都拒绝。"""
    manager = ToolManager()  # 管理器
    bad = SimpleNamespace(name="Bad-Name", description=" x ", args_schema=None)  # 非法名
    with pytest.raises(InvalidToolNameError):  # 期望失败
        manager.register(bad)  # type: ignore[arg-type]


def test_get_missing_raises() -> None:  # 不存在
    """取不到直接 KeyError，不静默返回 None。"""
    manager = ToolManager()  # 管理器
    with pytest.raises(KeyError):  # 期望失败
        manager.get("nope")  # 不存在


def test_register_all() -> None:  # 批量
    """批量注册保持顺序。"""
    manager = ToolManager()  # 管理器
    manager.register_all([search_kb, read_file])  # 批量
    assert manager.names() == ["search_kb", "read_file"]  # 顺序
