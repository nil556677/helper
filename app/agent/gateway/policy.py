"""权限与租户判定：工具是否允许被当前调用方使用（纯函数，可单测）。

规则（面向 5.3.4 最小权限）：
- 工具 `permissions` 为空 → 允许（声明者即有权，无需额外 ACL）；
- 工具 `permissions` 非空 → 要求调用方权限集包含全部标签，缺任一 → 拒绝（`protocol.permission`）；
- 租户匹配：调用方未绑定租户时放行；绑定了就要与实际归属一致，否则拒绝。
当前工具 permissions 均空，故默认全放行；将来给某个工具加权限标签即自动生效。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.contracts import ErrorCode  # 错误码单源

from app.agent.gateway.principals import PolicyContext  # 调用方上下文

__all__ = ["check", "enforce_tenant"]  # 导出


def _permissions_of(descriptor: Any) -> frozenset[str]:  # 取工具的权限标签
    """描述符（ToolDescriptor）或带 permissions 属性的对象都兼容。"""
    raw = getattr(descriptor, "permissions", None)  # 取
    if raw is None:  # 没有
        return frozenset()  # 空
    if hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes)):  # 可迭代非字符串
        return frozenset(str(item) for item in raw if item is not None)  # 转集合
    return frozenset({str(raw)}) if raw else frozenset()  # 单个或空


def check(descriptor: Any, ctx: PolicyContext, *, args: dict[str, Any] | None = None) -> tuple[bool, str]:  # 权限判定
    """允许返回 (True, "")；拒绝返回 (False, ErrorCode)。`args` 预留，供未来按参数放权。"""
    del args  # 预留参数级放权（当前不用）
    required = _permissions_of(descriptor)  # 工具声明的权限
    if not required:  # 工具无权限要求
        return True, ""  # 放行
    missing = required - set(ctx.permissions)  # 缺哪些
    if missing:  # 有缺
        return False, str(ErrorCode.PROTOCOL_PERMISSION)  # 无权限
    return True, ""  # 全部满足


def enforce_tenant(owner_tenant: str, ctx: PolicyContext) -> tuple[bool, str]:  # 租户匹配
    """数据归属租户 vs 调用方租户；调用方未绑定租户（单租户桌面）时放行。"""
    caller = str(ctx.tenant_id or "")  # 调用方租户
    if not caller:  # 未绑定租户
        return True, ""  # 放行（单租户桌面）
    if owner_tenant and owner_tenant != caller:  # 归属不一致
        return False, str(ErrorCode.PROTOCOL_PERMISSION)  # 越权
    return True, ""  # 一致或数据无归属