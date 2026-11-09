"""内置工具库：各域工具在这里汇总，`collect` 只从这里收。

新增工具 = 在对应域目录加一个文件，并在本文件补一行 re-export。
"""

from __future__ import annotations  # 延迟值注解

from app.agent.tools.library.rag.search_knowledge_base import (  # 检索（mode 区分强度）
    search_knowledge_base,  # 知识库检索
)  # 导入结束
from app.agent.tools.library.time.current_time import current_time  # 时间
from app.agent.tools.library.weather.query_weather import query_weather  # 天气
from app.agent.tools.library.order.query_order import query_order  # 订单查询（需审批）
from app.agent.tools.library.logistics.query_logistics import query_logistics  # 物流查询（第三方）

__all__ = ["search_knowledge_base", "current_time", "query_weather", "query_order", "query_logistics"]  # 导出