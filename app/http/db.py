"""（兼容再导出）异步 SQLite 已下沉到 :mod:`app.core.db.aio`。

HTTP 层继续以 `from app.http.db import AsyncDB` 引用，避免改一大批 import。
"""
from __future__ import annotations  # 类型注解延迟求值

from app.core.db.aio import AsyncDB  # 规范实现

__all__ = ["AsyncDB"]  # 对外导出
