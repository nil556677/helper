"""订单工具：**先用固定样例数据**（后续从 MySQL 查，接口形状保持不变）。

只回答"订单本身"的信息（状态/金额/商品/下单时间/收货地址）；
物流轨迹是独立工具 `query_express`，不要混在一起。
"""

from __future__ import annotations

from app.core.intent import RouteDecision
from app.ui.state.chat_model import Turn

#: 固定样例订单（order_id → 订单数据）。
_ORDERS: dict[str, dict[str, str]] = {
    "123456": {
        "status": "已支付",
        "amount": "299.00",
        "items": "无线鼠标 ×1",
        "created": "2026-10-21 10:12",
        "address": "北京市朝阳区望京街道 1 号",
    },
    "889900": {
        "status": "待付款",
        "amount": "1299.00",
        "items": "机械键盘 ×1",
        "created": "2026-10-22 09:05",
        "address": "上海市浦东新区张江路 88 号",
    },
    "202610": {
        "status": "已完成",
        "amount": "89.00",
        "items": "USB-C 线 ×2",
        "created": "2026-10-18 16:40",
        "address": "广州市天河区体育西路 3 号",
    },
}


def handler(text: str = "", decision: RouteDecision | None = None) -> Turn:
    del text
    order_id = str((decision.slots if decision else {}).get("order_id") or "").strip()
    data = _ORDERS.get(order_id)
    if data is None:
        return Turn(note=f"没有查到订单号 {order_id}。")
    return Turn(
        answer=(
            f"订单 {order_id}：{data['status']}，金额 ¥{data['amount']}，"
            f"商品 {data['items']}，下单时间 {data['created']}，"
            f"收货地址 {data['address']}。"
        )
    )
