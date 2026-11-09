"""订单仓储：后台配置 MySQL 连接；测试可注入假仓储（不真连库）。"""

from __future__ import annotations  # 延迟值注解

from dataclasses import dataclass  # 订单对象
from typing import Any  # 任意值

from app.agent.tools.tooling import ToolError  # 业务错
from app.agent.contracts import ErrorCode  # 错误码

__all__ = ["Order", "OrderRepo", "get_order_repo", "set_order_repo"]  # 导出


@dataclass
class Order:  # 订单
    """订单主数据（客户后台 MySQL 的一行）。"""

    order_id: str  # 订单号
    status: str  # 状态
    amount: str  # 金额（文本，避免精度）
    pay_method: str  # 支付方式
    created_at: str  # 下单时间
    logistics_no: str = ""  # 运单号（物流工具画像）
    tenant_id: str = ""  # 租户（权限用）

    def to_data(self) -> dict[str, Any]:  # 出参
        return {  # 固定形状
            "order_id": self.order_id,  # 订单
            "status": self.status,  # 状态
            "amount": self.amount,  # 金额
            "pay_method": self.pay_method,  # 支付方式
            "created_at": self.created_at,  # 下单时间
            "logistics_no": self.logistics_no,  # 运单号
            "text": f"订单 {self.order_id} 状态 {self.status}，金额 {self.amount}，{self.pay_method}，{self.created_at} 下单",  # 一句话
        }  # 结束


class OrderRepo:  # 订单仓储接口
    """真实实现读后台配置的 MySQL；未配置/连不上抛 ToolError。"""

    async def get_order(self, order_id: str) -> Order | None:  # 按单号
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在后台填写 MySQL 地址/账号/密码）")  # 未配置

    async def get_orders_by_phone(self, phone: str) -> list[Order]:  # 按手机号
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在后台填写 MySQL 地址/账号/密码）")  # 未配置


_repo: OrderRepo | None = None  # 注入位（测试/装配覆盖）


def set_order_repo(repo: OrderRepo | None) -> None:  # 注入仓储
    """测试注入假仓储；生产置 None 用后台配置的真实实现。"""
    global _repo  # 改模块级
    _repo = repo  # 存


def get_order_repo() -> OrderRepo:  # 取仓储
    """优先注入；否则后台配置的实现（本版先给骨架，落 MySQL 到接入装配时）。"""
    global _repo  # 改模块级
    if _repo is None:  # 未注入
        _repo = OrderRepo()  # 默认（未配置时抛）
    return _repo  # 返回