"""物流轨迹查询工具（第三方免费 API，只读、不审批）。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 参数/结果契约

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.tooling import ToolError, tool_spec  # 统一范式
from app.services.logistics.client import get_provider  # 物流 provider
from app.services.order.repo import get_order_repo  # 订单仓储（查运单号）

__all__ = ["query_logistics"]  # 导出


class LogisticsQuery(BaseModel):  # 参数契约
    order_id: str = Field(default="", description="订单号；填了则先据此查运单号再查第三方，二选一即可")  # 单号
    tracking_no: str = Field(default="", description="运单号，直接查第三方物流轨迹；与 order_id 二选一")  # 运单号


class LogisticsResult(BaseModel):  # 结果契约
    """物流轨迹固定形状。"""

    order_id: str = ""  # 订单号（若有）
    tracking_no: str = ""  # 运单号
    carrier: str = ""  # 快递公司
    status: str = ""  # 当前状态
    detail: str = ""  # 一句话轨迹
    events: list[dict[str, Any]] = Field(default_factory=list)  # 最近事件列表


@tool_spec(  # 统一范式：只读，不需审批
    description=(  # 能力说明
        "按订单号或运单号查询物流轨迹、快递公司、当前配送状态。"
        "不返回订单金额/支付方式（用 query_order），不返回物流政策（用 search_knowledge_base）。"
        "只读、低风险，不需审批。"
    ),  # 说明结束
    args_schema=LogisticsQuery,  # 参数契约
    result_schema=LogisticsResult,  # 结果契约
    approval="none",  # 不审批
    risk="low",  # 低风险
    timeout_ms=8000,  # 第三方给足
)  # 装饰结束
async def query_logistics(order_id: str = "", tracking_no: str = ""):  # 异步纯函数
    """order_id 或 tracking_no 二选一；order_id 先查运单号再查第三方。"""
    target = str(tracking_no or "").strip()  # 运单号
    resolved_order_id = str(order_id or "")  # 订单号
    if not target:  # 没直接给运单号
        if not resolved_order_id:  # 单号也没有
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), "需提供订单号或运单号")  # 缺参
        repo = get_order_repo()  # 订单仓储
        order, _items = await repo.get_order_detail(resolved_order_id)  # 查单（同步异步由仓储封）
        if order is None:  # 订单不存在
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"订单 {resolved_order_id} 不存在")  # 业务错
        target = str(getattr(order, "logistics_no", "") or "").strip()  # 运单号
        if not target:  # 订单无运单号
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"订单 {resolved_order_id} 还没有物流单号")  # 无轨迹
    provider = get_provider()  # 第三方 provider
    trace = await provider.trace(target)  # 查轨迹
    return {  # 固定形状
        "order_id": resolved_order_id,  # 订单号
        "tracking_no": target,  # 运单号
        "carrier": str(trace.get("carrier") or ""),  # 快递公司
        "status": str(trace.get("status") or ""),  # 状态
        "detail": str(trace.get("detail") or str(trace.get("status") or "")),  # 一句话
        "events": list(trace.get("events") or [])[-5:],  # 最近事件
    }  # 结束