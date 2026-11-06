"""统一模型提供者：装配层注入 gateway 产出的 chat_model，节点按需取用（单一入口）。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Callable  # 类型

__all__ = ["get_chat_model", "set_chat_model_provider"]  # 导出

_PROVIDER: Callable[[], Any] | None = None  # 提供者


def set_chat_model_provider(provider: Callable[[], Any] | None) -> None:  # 注入
    global _PROVIDER  # 改模块级
    _PROVIDER = provider  # 存


def get_chat_model() -> Any | None:  # 取（无则 None，节点据此降级）
    return _PROVIDER() if _PROVIDER is not None else None  # 返回