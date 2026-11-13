"""订单查询工具（MySQL 后台配置）；读敏感单：需用户确认 + 留痕。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 参数/结果契约（args_schema 发模型）

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.tooling import ToolError, tool_spec  # 统一范式
from app.services.order.repo import get_order_repo  # 订单仓储

__all__ = ["query_order"]  # 导出


class OrderQuery(BaseModel):  # 参数契约
    order_id: str = Field(default="", description="订单号，纯数字，如 12345")  # 单号
    phone: str = Field(default="", description="下单手机号；订单号未知时用手机号查")  # 手机号


class OrderResult(BaseModel):  # 结果契约
    """订单主数据 + 商品明细固定形状。"""

    order_id: str = ""  # 订单号
    status: str = ""  # 状态
    amount: str = ""  # 金额
    pay_method: str = ""  # 支付方式
    created_at: str = ""  # 下单时间
    logistics_no: str = ""  # 运单号
    items: list[dict[str, Any]] = Field(default_factory=list)  # 商品明细
    text: str = ""  # 一句话


@tool_spec(  # 统一范式：读敏感单，需用户确认后查询
    description=(  # 能力说明
        "按订单号或手机号查询订单状态、金额、支付方式、下单时间。"
        "不返回物流轨迹（用 query_logistics），不返回政策条款（用 search_knowledge_base）。"
        "查询前会先请您确认（告知将查询哪些信息），确认后执行并留痕。"
    ),  # 说明结束
    args_schema=OrderQuery,  # 参数契约
    result_schema=OrderResult,  # 结果契约
    approval="manual",  # 必经用户确认
    risk="medium",  # 中风险
    timeout_ms=8000,  # 库查询给足
)  # 装饰结束
async def query_order(order_id: str = "", phone: str = ""):  # 异步纯函数
    """按单号/手机号查订单；缺参/不存在/多单 → ToolError。"""
    if not order_id and not phone:  # 两者都缺
        raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), "需提供订单号或手机号")  # 缺参
    repo = get_order_repo()  # 仓储（后台配置/注入）
    items: list[Any] = []  # 商品
    if order_id:  # 按单号
        order, items = await repo.get_order_detail(order_id)  # 订单+商品
        if order is None:  # 不存在
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"订单 {order_id} 不存在")  # 业务错
    else:  # 按手机号
        orders = await repo.get_orders_by_phone(phone)  # 查
        if len(orders) > 1:  # 多单
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), "该手机号关联多个订单，请提供订单号")  # 需单号
        if not orders:  # 无单
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"手机号 {phone} 下没有订单")  # 无
        order = orders[0]  # 取单
        order, items = await repo.get_order_detail(order.order_id)  # 补商品
    return order.to_data(items)  # 出参（订单+商品）
