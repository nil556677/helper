"""物流域服务（第三方免费 API，可插拔）。"""

from __future__ import annotations  # 延迟值注解

from app.services.logistics.client import (  # 客户端
    LogisticsProvider,  # 接口
    get_provider,  # 取
    set_provider,  # 注入
)  # 导入结束

__all__ = ["LogisticsProvider", "get_provider", "set_provider"]  # 导出