"""压缩摘要用到的模型生成器：走 B 的网关（非流式、低温）。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Awaitable, Callable, Sequence  # 类型
from typing import Any  # 任意值

from app.agent.contexts import blocks  # 标签块
from app.agent.contexts.prompts import SUMMARY_SYSTEM_UID, get  # 模板
from app.agent.llm import DEFAULT_PARAMS, ModelGateway  # 网关与默认参数
from app.agent.llm.types import ModelRequest  # 请求契约

__all__ = ["build_summarizer"]  # 对外导出

#: 摘要参数：低温、非流式（一次给全）。
_SUMMARY_PARAMS = DEFAULT_PARAMS.with_overrides(temperature=0.0, streaming=False)  # 摘要参数


def _slot() -> str:  # 压缩槽位
    """读取配置 `agent.compaction.slot`；取不到用默认 ``llm-compact``。"""
    try:  # 配置可能还没好，取不到不炸
        from app.core.config.store import load as load_config  # 延迟导入

        return str(load_config().values.get("agent.compaction.slot") or "llm-compact")  # 读配置
    except Exception:  # 读失败
        return "llm-compact"  # 回退专用槽位


def _history_texts(oldest: Sequence[dict[str, Any]]) -> str:  # 旧消息 → 文本
    """按顺序拼成供压缩的一段对话。"""
    parts = [f"{item.get('role', 'user')}：{item.get('content') or ''}" for item in oldest]  # 逐条
    return "\n".join(parts)  # 合并


def build_summarizer(  # 造压缩器
    emit: Callable[[Any], None],  # 事件出口（网关重试直达前端）
) -> Callable[[Sequence[dict[str, Any]], str], Awaitable[str]] | None:  # 压缩器
    """配置的槽位不可用时回退主 ``llm``；都不可用返回 None（调用方退回 trim）。"""
    slot = _slot()  # 配置的压缩槽位
    gateway = ModelGateway(slot=slot, emit=emit)  # 网关
    if not gateway.available(_SUMMARY_PARAMS) and slot != "llm":  # 专用槽位没配
        slot = "llm"  # 回退主模型
        gateway = ModelGateway(slot=slot, emit=emit)  # 重建网关
    if not gateway.available(_SUMMARY_PARAMS):  # 仍不可用
        return None  # 跳过压缩

    async def compactor(oldest: Sequence[dict[str, Any]], old_summary: str) -> str:  # 压缩器
        """把旧消息并入旧摘要，返回新摘要（可能被引擎拒绝）。"""
        body = blocks.join_blocks(  # 标签化输入
            blocks.container("existing_summary", old_summary),  # 旧摘要
            blocks.container("history", _history_texts(oldest)),  # 旧消息
        )  # 输入结束
        request = ModelRequest(  # 请求
            model=slot,  # 实际使用的槽位
            messages=[{"role": "user", "content": body}],  # 单条
            system=get(SUMMARY_SYSTEM_UID).render(),  # 压缩提示
        )  # 请求结束
        result = await gateway.generate(request, _SUMMARY_PARAMS)  # 调网关
        return str(result.text or "")  # 取文本

    return compactor  # 返回
