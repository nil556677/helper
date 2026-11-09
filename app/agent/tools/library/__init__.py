"""内置工具库：各域工具在这里汇总，`collect` 只从这里收。

新增工具 = 在对应域目录加一个文件，并在本文件补一行 re-export。
"""

from __future__ import annotations  # 延迟求值注解

from app.agent.tools.library.rag.search_knowledge_base import (  # 检索（mode 区分强度）
    search_knowledge_base,  # 知识库检索
)  # 导入结束
from app.agent.tools.library.time.current_time import current_time  # 时间
from app.agent.tools.library.weather.query_weather import query_weather  # 天气

__all__ = ["search_knowledge_base", "current_time", "query_weather"]  # 导出