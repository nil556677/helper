"""工具注册表（**封闭集合**）：只能执行这里的工具。

``enabled`` 以配置 ``tools.<name>.enabled`` 为准（读不到时退回声明默认值）；
``search_kb`` 是 RAG 默认/兜底工具，无配置项、恒启用。
"""

from __future__ import annotations

from app.core.config import store as params_store
from app.tools import clock, express, order, weather
from app.tools.base import ToolSpec

TOOLS: dict[str, ToolSpec] = {
    "search_kb": ToolSpec(
        name="search_kb",
        description="查数据：搜索本地资料库并给出引用（RAG）",
        param_schema={"query": {"required": True}},
        enabled=True,
        handler=None,  # 新图由 kb 节点直接走 HybridRetriever
    ),
    "query_order": ToolSpec(
        name="query_order",
        description="查询订单信息",
        param_schema={"order_id": {"required": True, "pattern": r"\d{6,}"}},
        enabled=True,
        handler=order.handler,
    ),
    "query_express": ToolSpec(
        name="query_express",
        description="查询快递物流",
        param_schema={
            "tracking_no": {"required": True, "pattern": r"[A-Za-z0-9]{8,32}"},
            "carrier": {"required": False},
        },
        enabled=True,
        handler=express.handler,
    ),
    "weather": ToolSpec(
        name="weather",
        description="查询天气",
        param_schema={"city": {"required": True}},
        enabled=True,
        handler=weather.handler,
    ),
    "time": ToolSpec(
        name="time",
        description="查询当前时间",
        param_schema={},
        enabled=True,
        handler=clock.handler,
    ),
}


def get(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def enabled(name: str) -> bool:
    """工具是否启用：配置优先，读不到用声明默认值。"""
    spec = TOOLS.get(name)
    if spec is None:
        return False
    try:
        return bool(params_store.load()[f"tools.{name}.enabled"])
    except Exception:
        return spec.enabled
