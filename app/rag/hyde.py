"""HyDE：先让 LLM 生成"假设的理想答案"，再用它的向量去检索（弥合问题-文档语义鸿沟）。

默认关闭（`RAG_ENABLE_HYDE=1` 开启）。假设文本**绝不外露**给用户，只用于取向量。
任何失败都返回空串，调用方退回普通检索。
"""

from __future__ import annotations  # 延迟值注解

import os  # 环境开关

from app.agent.llm import DEFAULT_PARAMS  # 默认参数
from app.agent.llm.gateway import ModelGateway  # 统一网关
from app.agent.llm.types import ModelRequest  # 请求
from app.core.retry import RetryPolicy  # 不重试

__all__ = ["enabled", "hypothetical"]  # 导出

#: 单次超时（秒）。
TIMEOUT_S = 2.0  # 超时

#: 提示：要求只写"可能出现在资料里的一段话"，不解释、不声明。
_SYSTEM = (  # 系统提示
    "根据用户问题，写一段最可能出现在本地资料里的、能直接回答该问题的陈述段落。"
    "只写这一段，不要解释、不要声明、不要编号；不超过 120 字。"
)  # 结束

from app.rag._emit import forward as _forward_emit  # 事件转发（有 run 才发）

_GATEWAY = ModelGateway(slot="llm", policy=RetryPolicy(), emit=_forward_emit)  # 网关（瞬时错误可重试 + 事件）

_PARAMS = DEFAULT_PARAMS.with_overrides(  # 参数
    temperature=0.0,  # 稳
    max_tokens=160,  # 小
    timeout_s=TIMEOUT_S,  # 超时
    max_retries=0,  # 无重试
)  # 结束


def enabled() -> bool:  # 是否启用 HyDE
    """环境变量开关，默认关闭。"""
    return str(os.environ.get("RAG_ENABLE_HYDE") or "").strip() in ("1", "true", "yes")  # 开关


async def hypothetical(question: str) -> str:  # 生成假设答案
    """有模型才生成；失败返回空串。"""
    text = str(question or "").strip()  # 问题
    if not text:  # 空
        return ""  # 空
    try:  # 调网关
        result = await _GATEWAY.generate(  # 生成
            ModelRequest(  # 请求
                model="llm",  # 槽位
                messages=[{"role": "user", "content": text}],  # 输入当数据
                system=_SYSTEM,  # 系统提示
            ),  # 结束
            _PARAMS,  # 参数
        )  # 结束
        return str(result.text or "").strip()  # 文本
    except Exception:  # 任何失败
        return ""  # 回退空