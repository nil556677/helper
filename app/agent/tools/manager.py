"""工具层：`@tool` 收集、全局查重、描述符与管理器（handler 由 `@tool` 自带）。"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 名称校验
from collections.abc import Iterable  # 集合输入
from types import ModuleType  # 模块类型

from langchain_core.tools import BaseTool  # 框架工具基类

from app.agent.contracts import Mutation, ToolDescriptor  # 契约

__all__ = [  # 对外导出
    "DuplicateToolError",  # 重复工具
    "InvalidToolNameError",  # 非法名称
    "ToolManager",  # 管理器
    "collect_tools",  # 收集器
]  # 导出结束

_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")  # 只允许小写蛇形、≤30


class DuplicateToolError(ValueError):  # 重名
    """同名工具重复注册（构图期失败，不留到运行期）。"""


class InvalidToolNameError(ValueError):  # 非法名
    """工具名不合规（必须 `[a-z0-9_]`，无点号，≤30）。"""


def collect_tools(module: ModuleType) -> list[BaseTool]:  # 从模块收集
    """收集模块内由 `@tool` 定义的 `BaseTool` 实例（按定义顺序）。"""
    found: list[BaseTool] = []  # 结果
    for value in vars(module).values():  # 遍历模块属性
        if isinstance(value, BaseTool):  # 是工具
            found.append(value)  # 收下
    return found  # 返回


class ToolManager:  # 工具注册表
    """唯一名称来源：注册期校验名称并生成描述符。"""

    def __init__(self) -> None:  # 构造
        self._tools: dict[str, BaseTool] = {}  # 名称 -> 工具
        self._descriptors: dict[str, ToolDescriptor] = {}  # 名称 -> 描述符

    def register(  # 注册一个
        self,
        tool: BaseTool,  # 框架工具
        *,
        mutation: Mutation = Mutation.READ_ONLY,  # 变更画像
        permissions: tuple[str, ...] = (),  # 权限
        version: int = 1,  # 版本
    ) -> None:  # 无返回
        """校验名称唯一且合规，然后登记描述符。"""
        name = str(tool.name or "")  # 名字即 id
        if not _NAME_RE.match(name):  # 不合规
            raise InvalidToolNameError(f"工具名不合法：{name!r}")  # 拒绝
        if name in self._tools:  # 已存在
            raise DuplicateToolError(f"工具重复：{name}")  # 拒绝
        self._tools[name] = tool  # 登记
        self._descriptors[name] = ToolDescriptor(  # 描述符
            name=name,  # 名称
            version=int(version),  # 版本
            description=str(tool.description or ""),  # 描述
            args_schema=tool.args_schema,  # 参数契约
            mutation=mutation,  # 变更画像
            permissions=tuple(permissions),  # 权限
        )  # 描述符结束

    def register_all(  # 批量注册
        self,
        tools: Iterable[BaseTool],  # 工具集合
        *,
        mutation: Mutation = Mutation.READ_ONLY,  # 默认画像
    ) -> None:  # 无返回
        """逐个注册（任一失败即整体抛出，构图期定位）。"""
        for tool in tools:  # 逐个
            self.register(tool, mutation=mutation)  # 注册

    def get(self, name: str) -> BaseTool:  # 取工具
        """不存在直接 KeyError（不返回 None，避免调用方静默）。"""
        return self._tools[name]  # 取

    def names(self) -> list[str]:  # 全部名称
        """按注册顺序返回名称。"""
        return list(self._tools)  # 列表

    def descriptor(self, name: str) -> ToolDescriptor:  # 取描述符
        """不存在直接 KeyError。"""
        return self._descriptors[name]  # 取

    def descriptors(self) -> list[ToolDescriptor]:  # 全部描述符
        """按注册顺序返回。"""
        return list(self._descriptors.values())  # 列表
