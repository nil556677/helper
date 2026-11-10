"""调用方权限上下文：工具调用时谁在调用、他有哪些角色/权限/租户。

单用户桌面产品：`AccessContext`（app/agent/contracts/triage.py）里 permissions/roles 目前为空，
所以 `from_state` 能读到就吃，读不到就返回空上下文。工具 `permissions` 为空时判"放行"，
机制先就位；将来注入真实 ACL 即可生效。
"""

from __future__ import annotations  # 延迟值注解

from dataclasses import dataclass, field  # 不可变上下文
from typing import Any, Iterable  # 任意值 / 可迭代

__all__ = ["PolicyContext"]  # 导出


def _strs(value: Any) -> frozenset[str]:  # 归一为字符串集合
    """把 list/tuple/set/frozenset 的元素转成字符串集合；非可迭代返回空集。"""
    if not isinstance(value, (list, tuple, set, frozenset)):  # 非集合
        return frozenset()  # 空集
    return frozenset(str(item) for item in value if item is not None)  # 去 None 转串


@dataclass(frozen=True)  # 不可变，避免流经时被改
class PolicyContext:  # 调用方上下文
    """工具调用方的身份与权限快照。"""

    user_id: str = ""  # 用户 id（缺省空）
    tenant_id: str = ""  # 租户（缺省空=未绑定租户）
    roles: frozenset[str] = field(default_factory=frozenset)  # 角色集合
    permissions: frozenset[str] = field(default_factory=frozenset)  # 权限标签集合

    @classmethod  # 工厂
    def from_state(cls, state: dict[str, Any] | None) -> "PolicyContext":  # 从图状态解析
        """尽量从 state 里提取上下文；读不到返回空（默认放行语义）。

        优先读 `state["access"]`（AccessContext），其次 `state["triage"]`，最后顶层散列的
        `tenant_id` 等字段。任何缺失都不报错，返回空上下文。
        """
        state = dict(state or {})  # 归一
        access = state.get("access")  # 完整上下文（可能为 dict）
        triage = state.get("triage")  # 决策（可能带角色/权限）
        srcs: list[dict[str, Any]] = []  # 由高到低的来源候选
        if isinstance(access, dict):  # 优先完整上下文
            srcs.append(access)  # 加入
        if isinstance(triage, dict):  # 决策兜底
            srcs.append(triage)  # 加入
        srcs.append(state)  # 顶层兜底

        def _pick(key: str) -> Any:  # 第一个非空来源里的值
            for src in srcs:  # 按优先级
                value = src.get(key)  # 取
                if value not in (None, ""):  # 非空
                    return value  # 返回
            return ""  # 兜底

        return cls(  # 组装
            user_id=str(_pick("user_id") or _pick("requester_id") or ""),  # 用户
            tenant_id=str(_pick("tenant_id") or ""),  # 租户
            roles=_strs(_pick("roles")),  # 角色
            permissions=_strs(_pick("permissions")),  # 权限
        )  # 返回