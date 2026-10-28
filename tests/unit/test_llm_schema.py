"""结构化输出模型基类：description/默认值强制 + **错误全量收集**（不强制转换）。

规则来源（2026-10-27 用户决定）：
- 字段说明只写 `Field(description=...)`（行尾注释模型看不到）；
- 每个字段都必须有默认值；
- 值错了**不猜、不强制转换**：一次校验收全部错误，整包回传给模型重新生成。
"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 造 JSON 解析错误

import pytest  # 测试框架
from pydantic import Field  # 字段

from app.agent.llm import (  # 被测对象
    StructuredModel,  # 基类
    issues_from,  # 异常 → 全部错误
    issues_text,  # 错误 → 人话
    parse_into,  # 整份校验
    schema_preview,  # schema 预览
)


class _Good(StructuredModel):  # 合规样例
    """示例：路由决策。"""

    intent: str = Field(default="kb", description="意图，取值 kb / tool / chitchat")  # 意图
    need_tool: bool = Field(default=False, description="是否需要调用工具；true / false")  # 布尔


def test_schema_has_descriptions_and_defaults() -> None:  # schema 自检
    """发给模型的 schema：每个字段都有 description，且没有 required。"""
    schema = schema_preview(_Good)  # 生成
    assert schema.get("required", []) == []  # 全有默认值 → 没有必填
    assert schema["additionalProperties"] is False  # 不允许额外字段
    assert schema["properties"]["intent"]["description"].startswith("意图")  # 说明进 schema
    assert schema["properties"]["need_tool"]["type"] == "boolean"  # 布尔类型明确
    assert schema["properties"]["need_tool"]["default"] is False  # 默认值可见


def test_missing_description_is_rejected_at_class_creation() -> None:  # 缺说明
    """只有行尾注释、没有 Field(description) → 定义类时就报错。"""
    with pytest.raises(TypeError) as error:  # 期望异常
        class _Bad(StructuredModel):  # 不合规
            value: int = 0  # 行尾注释不算说明

    assert "description" in str(error.value)  # 提示要写 description


def test_missing_default_is_rejected_at_class_creation() -> None:  # 缺默认值
    """没有默认值 → 定义类时就报错。"""
    with pytest.raises(TypeError) as error:  # 期望异常
        class _Bad(StructuredModel):  # 不合规
            value: int = Field(description="说明有，但没默认值")  # 缺默认

    assert "默认值" in str(error.value)  # 提示要补默认值


def test_parse_into_success_uses_defaults_for_missing_fields() -> None:  # 缺省不是错
    """模型省略字段不算错误：用默认值补齐。"""
    data, issues = parse_into(_Good, {"intent": "tool"})  # 只给一个字段
    assert issues == []  # 没有错误
    assert data is not None  # 有对象
    assert data.need_tool is False  # 默认值


def test_parse_into_collects_all_field_errors() -> None:  # 错误全收
    """两个字段同时错 → 一次返回两条，不是只报第一条。"""
    data, issues = parse_into(_Good, {"intent": 5, "need_tool": "是"})  # 两个都错
    assert data is None  # 没通过
    assert len(issues) == 2  # 两条
    assert {issue.path for issue in issues} == {"intent", "need_tool"}  # 路径都在
    text = issues_text(issues)  # 人话
    assert "1. intent" in text  # 编号 1
    assert "2. need_tool" in text  # 编号 2


def test_bool_wrong_value_is_not_coerced() -> None:  # 不猜
    """`"是"` 不换算成 True：直接算错，交给模型重新生成。"""
    data, issues = parse_into(_Good, {"intent": "kb", "need_tool": "是"})  # 中文布尔
    assert data is None  # 不通过
    assert [issue.path for issue in issues] == ["need_tool"]  # 报在布尔字段上


def test_issues_from_json_error() -> None:  # 整体不是 JSON
    """输出不是 JSON 时给一条「整体」错误。"""
    try:  # 故意解析失败
        json.loads("这不是 JSON")  # 抛错
    except json.JSONDecodeError as error:  # 捕获
        issues = issues_from(error)  # 转换
    assert len(issues) == 1  # 一条
    assert issues[0].path == "<整体>"  # 整体
    assert "JSON" in issues[0].message  # 说清原因


def test_issues_from_generic_error() -> None:  # 其他异常
    """别的异常也折成一条，不丢信息。"""
    issues = issues_from(RuntimeError("网关炸了"))  # 转换
    assert issues[0].message == "网关炸了"  # 原文
