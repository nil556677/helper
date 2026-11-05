"""事件序列化与序列校验：跨 UI/SSE/trace 边界的唯一出入口。"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 默认时间戳
import uuid  # 生成事件 id
from typing import Any  # 任意类型

from app.agent.contracts.events import PAYLOAD_BY_TYPE, EventEnvelope  # 信封与载荷映射
from app.agent.contracts.kinds import TERMINAL_TYPES, EventType  # 终态集合与类型


def make(  # 便捷构造
    event_type: EventType,  # 事件类型
    payload: Any = None,  # 载荷（dict / 模型 / None）
    *,
    run_id: str = "",  # 归属 run
    event_id: str | None = None,  # 事件 id
    timestamp: float | None = None,  # 时间戳
    **fields: Any,  # 直接传载荷字段
) -> EventEnvelope:  # 构造结果
    """构造并校验一个事件。

    ``payload`` 与 ``fields`` 只能二选一：混用会让后者被静默丢弃，故直接拒绝。
    """
    model = PAYLOAD_BY_TYPE[event_type]  # 该类型的载荷模型
    if payload is not None and fields:  # 两种来源同时出现
        raise ValueError("payload 与 fields 不能同时传：要么传完整载荷，要么用字段构造")  # 拒绝混用
    if payload is None:  # 用关键字字段造
        payload = model(**fields)  # 构造载荷
    elif isinstance(payload, dict):  # 传了字典
        payload = model.model_validate(payload)  # 先强校验
    elif not isinstance(payload, model):  # 传了别的东西（字符串等）
        raise TypeError(f"{event_type.value} 的 payload 必须是 {model.__name__} 或 dict")  # 拒绝
    return EventEnvelope(  # 组装信封
        event_id=event_id or uuid.uuid4().hex,  # 事件 id
        run_id=run_id,  # run id
        timestamp=time.time() if timestamp is None else timestamp,  # 时间戳
        type=event_type,  # 类型
        payload=payload,  # 载荷
    )  # 信封结束


def to_dict(event: EventEnvelope) -> dict[str, Any]:  # 事件转 JSON 友好字典
    """枚举转字符串，可直接进 SSE。"""
    return event.model_dump(mode="json")  # 序列化


def from_dict(data: dict[str, Any]) -> EventEnvelope:  # 字典转事件
    """校验 type 与 payload 必须匹配。"""
    return EventEnvelope.model_validate(data)  # 校验并构造


def validate_sequence(  # 校验一个 run 的事件序列
    events: list[EventEnvelope],  # 事件序列
    *,  # 以下仅关键字
    allow_empty: bool = False,  # 是否允许空序列（默认拒绝：空 run 通常是上游 bug）
) -> None:  # 无返回
    """终态唯一且必须是最后一帧；默认拒绝空序列。

    流式/构造期需要逐步校验时显式传 ``allow_empty=True``；API 边界应保持默认严格。
    """
    if not events:  # 空序列
        if allow_empty:  # 调用方明确允许
            return  # 放行
        raise ValueError("事件序列不能为空：一个 run 至少应有一帧")  # 拒绝
    terminals = [event for event in events if event.type in TERMINAL_TYPES]  # 终态事件
    if len(terminals) > 1:  # 超过一个终态
        raise ValueError(f"一个 run 只允许一个终态事件，收到 {len(terminals)} 个")  # 报错
    if terminals and events[-1].type not in TERMINAL_TYPES:  # 终态不在最后
        raise ValueError("终态事件必须是最后一帧")  # 报错
