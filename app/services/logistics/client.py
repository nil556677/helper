"""物流查询客户端：第三方免费 provider，可插拔；测试注入 mock。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.tools.tooling import ToolError  # 业务错
from app.agent.contracts import ErrorCode  # 错误码

__all__ = ["BaiduProvider", "LogisticsProvider", "get_provider", "set_provider"]  # 导出


class LogisticsProvider:  # 第三方 provider 接口
    """真实实现默认走百度快递（免费额度）；未配 key 抛 ToolError，限流/失败降级。"""

    async def trace(self, tracking_no: str) -> dict[str, Any]:  # 查轨迹
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "物流查询未配置（请在后台填写第三方接口/免费 key）")  # 未配置


def _logistics_config() -> dict[str, Any]:  # 读后台配置
    """从 settings 读物流接口（agent.logistics.*）；取不到返回空。"""
    try:  # 配置可能未就绪
        from app.core.config.store import load  # 配置

        values = load().values  # 全部
    except Exception:  # 读失败
        return {}  # 空
    return {  # 映射
        "endpoint": str(values.get("agent.logistics.endpoint") or ""),  # 接口地址
        "key": str(values.get("agent.logistics.key") or ""),  # 免费 key
    }  # 结束


class BaiduProvider(LogisticsProvider):  # 百度快递（免费额度）实现
    """GET endpoint?no=运单号&key=... 解析 JSON；无 key/无端点 → ToolError。"""

    async def trace(self, tracking_no: str) -> dict[str, Any]:  # 查轨迹
        cfg = _logistics_config()  # 配置
        if not cfg.get("endpoint"):  # 未配置端点
            raise ToolError(str(ErrorCode.EXECUTION_FAILED), "物流查询未配置（请在后台填写百度快递接口地址/key）")  # 报错
        params = {"no": str(tracking_no), "key": cfg.get("key") or ""}  # 参数
        try:  # 走统一 HTTP 客户端
            from app.core.http_client import aget_json  # HTTP

            data = await aget_json(cfg["endpoint"], params=params)  # 请求
        except Exception as exc:  # 网络/限流
            raise ToolError(str(ErrorCode.EXECUTION_FAILED), f"物流查询失败：{exc}")  # 报错
        if not isinstance(data, dict):  # 非常规
            raise ToolError(str(ErrorCode.EXECUTION_FAILED), "物流查询返回格式异常")  # 报错
        return {  # 归一化
            "carrier": str(data.get("carrier") or data.get("company") or ""),  # 快递公司
            "status": str(data.get("status") or data.get("state") or ""),  # 状态
            "detail": str(data.get("detail") or data.get("message") or ""),  # 说明
            "events": list(data.get("events") or []),  # 事件
        }  # 结束


_provider: LogisticsProvider | None = None  # 注入位


def set_provider(provider: LogisticsProvider | None) -> None:  # 注入 provider
    """测试注入 mock；生产置百度默认 provider。"""
    global _provider  # 改模块级
    _provider = provider  # 存


def get_provider() -> LogisticsProvider:  # 取 provider
    global _provider  # 改模块级
    if _provider is None:  # 未注入
        _provider = BaiduProvider()  # 百度默认（未配置时抛）
    return _provider  # 返回