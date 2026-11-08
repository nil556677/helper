"""参数完整性：对照工具 args_schema 必填字段。"""

from __future__ import annotations  # 延迟值注解

from app.agent.routing.args import missing_required, required_fields  # 被测


def test_required_fields_from_schema() -> None:  # 从 args_schema 取必填
    fields = required_fields(["query_weather"])  # 天气工具
    assert "city" in fields  # 城市必填


def test_missing_required() -> None:  # 实体缺失则计入缺参
    assert "city" in missing_required(["query_weather"], {})  # 没城市 → 缺
    assert missing_required(["query_weather"], {"city": "成都"}) == []  # 有城市 → 不缺


def test_unknown_tool_ignored() -> None:  # 未注册工具忽略
    assert required_fields(["no_such_tool"]) == []  # 空