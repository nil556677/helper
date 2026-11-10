"""T0.3 工作记忆写侧接线：run_react/dispatch/finalize 让白板真正被记步/收尾。"""

from __future__ import annotations  # 延迟值注解

from app.agent.fallback.agent import _anchor_facts  # 锚点抽取
from app.agent.memory import blank_working  # 空白板


def test_anchor_facts_from_content_dict() -> None:  # 从 content dict 挑锚点
    from langchain_core.messages import ToolMessage  # 消息类型

    msg = ToolMessage(content={"city": "成都", "date": "2025-01-01", "extra": 1}, tool_call_id="c", name="query_weather")  # 带白名单+多余字段
    anchors = _anchor_facts("query_weather", msg)  # 抽取
    assert anchors == {"city": "成都", "date": "2025-01-01"}  # 只挑白名单两字段
    assert "extra" not in anchors  # 多余字段不进


def test_anchor_facts_from_content_json() -> None:  # 从 content JSON 字符串挑锚点
    import json  # 序列化

    from langchain_core.messages import ToolMessage  # 消息类型

    msg = ToolMessage(content=json.dumps({"order_id": "37979", "amount": 99.5}), tool_call_id="c", name="query_order")  # JSON 字符串
    anchors = _anchor_facts("query_order", msg)  # 抽取
    assert anchors["order_id"] == "37979"  # 单号进锚点


def test_anchor_facts_unknown_tool_empty() -> None:  # 未知工具不挑锚点
    from langchain_core.messages import ToolMessage  # 消息类型

    msg = ToolMessage(content={"x": 1}, tool_call_id="c", name="unknown_tool")  # 不在白名单
    assert _anchor_facts("unknown_tool", msg) == {}  # 无锚点


def test_anchor_facts_missing_field() -> None:  # 白名单字段缺失则不记
    from langchain_core.messages import ToolMessage  # 消息类型

    msg = ToolMessage(content={"date": "2025-01-01"}, tool_call_id="c", name="query_weather")  # 缺 city
    anchors = _anchor_facts("query_weather", msg)  # 抽取
    assert "city" not in anchors  # 缺的字段不记
    assert anchors.get("date") == "2025-01-01"  # 有的字段记


def test_promote_clears_board_in_finalize() -> None:  # promote 取结论且空白板
    # 验证 working.py 的 promote + finalize 语义：结论可写、白板可清。
    from app.agent.memory import promote_working, touch_working, update_working  # 白板助手

    white = touch_working(update_working(blank_working(), current_step="x"), {"order_id": "A1"})  # 有事实+步
    white = update_working(white, current_step="")  # 完成当前步
    _steps, facts = promote_working(white)  # 收尾取结论
    assert facts == {"order_id": "A1"}  # 结论候选
    fresh = blank_working()  # finalize 清板后
    assert fresh["key_facts"] == {}  # 关键事实已清
    assert fresh["past_steps"] == []  # 步骤已清