"""订单域服务（MySQL 后台配置）。"""

from __future__ import annotations  # 延迟值注解

from app.services.order.repo import (  # 仓储
    Order,  # 订单
    OrderRepo,  # 接口
    get_order_repo,  # 取
    set_order_repo,  # 注入
)  # 导入结束

__all__ = ["Order", "OrderRepo", "get_order_repo", "set_order_repo"]  # 导出