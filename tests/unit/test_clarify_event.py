"""CLARIFY 事件：判别联合可序列化/回读（前端契约）。"""

from __future__ import annotations  # 延迟求值注解

from app.agent import events  # 事件构造
from app.agent.contracts import EventEnvelope, EventType  # 契约


def test_clarify_event_roundtrip() -> None:  # 构造 + 回读
    env = events.clarify(  # 造事件
        "thread-1",  # 线程
        title="请选择",  # 标题
        description="你想查什么？",  # 说明
        options=[{"value": "time", "label": "查时间"}, {"value": "weather", "label": "查天气"}],  # 选项
    )  # 结束
    dumped = env.model_dump()  # 序列化
    assert dumped["type"] == EventType.CLARIFY  # 类型
    back = EventEnvelope.model_validate(dumped)  # 回读
    assert back.payload.thread_id == "thread-1"  # 线程
    assert back.payload.options[0]["value"] == "time"  # 选项
    assert EventType.CLARIFY not in {EventType.DONE, EventType.ERROR, EventType.CANCELLED}  # 非终态