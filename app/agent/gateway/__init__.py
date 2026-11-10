"""工具网关：调用方权限上下文 / 权限判定 / 敏感字段脱敏（横切，与具体工具解耦）。

面向需求 5.3.4/5.4：把「谁能用这个工具、参数里的敏感值、落库前的脱敏」收敛到一处，
供审批门、审批执行、路由、审计统一调用。当前单用户桌面产品：无 ACL 上下文时默认放行
（机制就位，多租户/角色化时由调用方注入 `PolicyContext` 即生效）。
"""

from __future__ import annotations  # 延迟求值注解

from app.agent.gateway.policy import check, enforce_tenant  # 权限判定
from app.agent.gateway.principals import PolicyContext  # 调用方上下文
from app.agent.gateway.redact import SENSITIVE_NAMES, redact_args  # 脱敏

__all__ = [  # 对外导出
    "PolicyContext",  # 权限上下文
    "SENSITIVE_NAMES",  # 敏感字段名集合
    "check",  # 权限判定
    "enforce_tenant",  # 租户匹配
    "redact_args",  # 参数脱敏
]  # 导出结束