"""天气域工具汇总。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.tools.library.weather.query_weather import query_weather  # 天气工具

__all__ = ["query_weather"]  # 导出