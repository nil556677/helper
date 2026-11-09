"""物流查询客户端：第三方免费 provider，可插拔；测试注入 mock。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.tools.tooling import ToolError  # 业务错
from app.agent.contracts import ErrorCode  # 错误码

__all__ = ["LogisticsProvider", "get_provider", "set_provider"]  # 导出


class LogisticsProvider:  # 第三方 provider 接口
    """真实实现默认走百度快递（免费额度）；未配 key 抛 ToolError，限流/失败降级。"""

    async def trace(self, tracking_no: str) -> dict[str, Any]:  # 查轨迹
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "物流查询未配置（请在后台填写第三方接口/免费 key）")  # 未配置


_provider: LogisticsProvider | None = None  # 注入位


def set_provider(provider: LogisticsProvider | None) -> None:  # 注入 provider
    """测试注入 mock；生产置百度默认 provider。"""
    global _provider  # 改模块级
    _provider = provider  # 存


def get_provider() -> LogisticsProvider:  # 取 provider
    global _provider  # 改模块级
    if _provider is None:  # 未注入
        _provider = LogisticsProvider()  # 默认（未配置时抛）
    return _provider  # 返回