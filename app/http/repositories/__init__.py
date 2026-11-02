"""异步仓储包：HTTP 层的数据访问（aiosqlite，只读为主）。

任务系统/导入仍用同步连接；本包只服务 HTTP 路由。
"""
from __future__ import annotations  # 类型注解延迟求值
