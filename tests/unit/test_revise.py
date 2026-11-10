"""工具参数「调用前修复」助手测试：有限次收敛 / 首轮合法 / 修复用尽终止。"""

from __future__ import annotations  # 延迟值注解

import pytest  # 断言异常
from pydantic import BaseModel, Field  # 参数契约

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.revise import collect_issues, fix_tool_args  # 被测
from app.agent.tools.tooling import ToolError  # 业务错


class _Req(BaseModel):  # 参数契约（带约束）
    """order_id 至少 5 位。"""

    order_id: str = Field(min_length=5)  # 最短 5


def test_first_valid_returns_without_repair() -> None:  # 首轮合法直接返回
    called = []  # 记录修复是否被调
    out = fix_tool_args({"order_id": "abcde"}, _Req, repair=lambda a, i: called.append(1) or a)  # 合法
    assert out == {"order_id": "abcde"}  # 原样
    assert called == []  # 未触发修复


def test_repair_converges() -> None:  # 修复收敛
    def repair(args, _issues):  # 恒给出合法值
        return {"order_id": "12345"}  # 合法

    out = fix_tool_args({"order_id": "x"}, _Req, repair=repair)  # 首轮非法
    assert out == {"order_id": "12345"}  # 收敛到合法


def test_repair_exhausted_raises_protocol() -> None:  # 修复用尽终止
    def repair(args, _issues):  # 修不好
        return {"order_id": "x"}  # 仍非法

    with pytest.raises(ToolError) as exc:  # 应抛业务错
        fix_tool_args({"order_id": "x"}, _Req, repair=repair, limit=2)  # 两次仍失败
    assert exc.value.code == str(ErrorCode.PROTOCOL_ARG_INVALID)  # 参数错


def test_collect_issues_reports_field() -> None:  # 结构性报告含字段位置
    issues = collect_issues(_Req, {"order_id": "x"})  # 非法值
    assert issues  # 非空
    assert any("order_id" in issue.get("loc", []) for issue in issues)  # 指向该字段
    assert collect_issues(_Req, {"order_id": "abcde"}) == []  # 合法空
    assert collect_issues(None, {"order_id": "x"}) == []  # 无契约视为合法