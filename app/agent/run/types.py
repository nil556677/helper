"""运行 DTO 与结束标记（客户端与后端共用）。"""

from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意值

#: 结束标记；与 ``app.agent.stream.SENTINEL`` 是同一个对象（消费端统一比较）。
SENTINEL = object()  # 结束


@dataclass(frozen=True)
class RunInput:  # 一次运行的入参
    """发给 worker 的最小入参（附件 E0.4 收敛为 id）。"""

    question: str  # 问题
    conversation_id: str  # 会话 id
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 附件元数据（含 path）
    run_id: str = ""  # 运行 id（路由先生成，agent 复用；空则由 start_stream 生成）
