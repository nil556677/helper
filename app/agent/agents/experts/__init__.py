"""专家自动发现：扫描本包 .py 模块 → 校验 DESCRIPTOR + build → 注册 EXPERTS。

约定（每个专家模块必须提供）：
- `DESCRIPTOR: AgentDescriptor`
- `build(model, tools) -> 编译后的 agent 图`
新增专家 = 丢一个文件进本目录，不改注册表、不改图。
"""

from __future__ import annotations  # 延迟求值注解

import importlib  # 动态导入
import pkgutil  # 包内模块扫描
from typing import Any, Callable  # 类型

from app.agent.contracts.routing import AGENT_KINDS, AgentDescriptor  # 契约（单一来源）

#: 真实专家种类（`unsupported` 是出口标记，不是专家）。
EXPERT_KINDS: tuple[str, ...] = tuple(k for k in AGENT_KINDS if k != "unsupported")  # 专家种类


def _validate(descriptor: AgentDescriptor, where: str) -> None:  # 校验单个描述符
    """名字必须属于 EXPERT_KINDS；副作用合法；工具数不超上限。"""
    if descriptor.name not in EXPERT_KINDS:  # 未声明的专家
        raise ValueError(f"{where}: 专家名 {descriptor.name!r} 不在 EXPERT_KINDS={EXPERT_KINDS} 中")
    if descriptor.side_effect not in ("none", "reversible", "irreversible"):  # 非法副作用
        raise ValueError(f"{where}: side_effect={descriptor.side_effect!r} 非法")
    if len(descriptor.tools) > descriptor.max_tools:  # 工具超上限
        raise ValueError(f"{where}: 工具 {len(descriptor.tools)} 个，超过上限 {descriptor.max_tools}")


def discover_experts() -> dict[str, tuple[AgentDescriptor, Callable[..., Any]]]:  # 扫描注册
    """扫描本包模块；缺 DESCRIPTOR/build、重名、非法 → 启动期报错（fail-fast）。"""
    experts: dict[str, tuple[AgentDescriptor, Callable[..., Any]]] = {}  # 注册表
    for info in pkgutil.iter_modules(__path__):  # 本包内模块
        if info.name.startswith("_") or info.name == "base":  # 跳过私有/基座
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")  # 导入
        descriptor = getattr(module, "DESCRIPTOR", None)  # 描述符
        build = getattr(module, "build", None)  # 构建函数
        if not isinstance(descriptor, AgentDescriptor):  # 缺描述符
            raise ValueError(f"{info.name}: 缺少 DESCRIPTOR: AgentDescriptor")
        if not callable(build):  # 缺 build
            raise ValueError(f"{info.name}: 缺少可调用的 build(model, tools)")
        _validate(descriptor, info.name)  # 校验
        if descriptor.name in experts:  # 重名
            raise ValueError(f"专家重名: {descriptor.name}")
        experts[descriptor.name] = (descriptor, build)  # 注册
    return experts  # 返回


def assert_experts_complete(experts: dict[str, Any], *, strict: bool = True) -> None:  # 完整性与单源校验
    """发现的专家集合必须与 EXPERT_KINDS 完全一致（缺/多都报错）。"""
    found = set(experts)  # 已发现
    expected = set(EXPERT_KINDS)  # 应发现
    if not strict:  # 放宽（开发期）
        missing = expected - found  # 缺失
        if missing:
            raise ValueError(f"缺少专家实现: {sorted(missing)}")
        return
    if found != expected:  # 必须完全一致
        raise ValueError(f"专家集合不一致: 缺 {sorted(expected - found)}，多 {sorted(found - expected)}")


EXPERTS = discover_experts()  # 启动期注册（当前为空，随模块加入自动增长）

__all__ = ["EXPERT_KINDS", "EXPERTS", "assert_experts_complete", "discover_experts"]  # 导出