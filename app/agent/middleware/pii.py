"""脱敏工具：出站到模型/日志前掩码；本地库保留原文。

只保留**纯函数 :func:`redact` 与规则表**，供显式调用点复用（如 `app.agent.memory.manager`）。

**为什么不再有"自动包裹"的 PIIMiddleware**：那套自研 `wrap_model_call` 从没被接线——
模型/工具调用一律走框架的 AgentMiddleware，框架自带的 PII（邮箱/卡号）已在
`app.agent.agent_builder.default_middleware` 启用。若把这里的手机号规则也自动套到模型
出入站，会把"按手机号查订单"所需的手机号一并洗掉、直接打断工具链路，故不自动套用；
需要更宽的脱敏面时，由调用点显式调 :func:`redact`。
"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 正则

__all__ = ["redact"]  # 导出

#: 默认规则；顺序有意义（身份证/银行卡在前，避免被手机号规则切碎）。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (  # 规则表
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),  # 身份证
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),  # 银行卡
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),  # 手机号
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),  # 邮箱
)  # 规则表结束


def redact(text: str) -> tuple[str, dict[str, int]]:  # 脱敏
    """返回（脱敏文本，类型计数）；不保留原值。"""
    counts: dict[str, int] = {}  # 计数
    result = str(text or "")  # 复制
    for name, pattern in _RULES:  # 逐条规则
        result, hits = pattern.subn(f"[REDACTED:{name}]", result)  # 替换
        if hits:  # 有命中
            counts[name] = counts.get(name, 0) + hits  # 累计
    return result, counts  # 返回
