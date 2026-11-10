"""默认大模型参数修复测试：模型修复成功 / 失败 / 无模型明确失败。"""

from __future__ import annotations  # 延迟值注解

import json  # 构造模型返回
from types import SimpleNamespace  # 假模型/响应

import pytest  # 断言异常
from pydantic import BaseModel, Field  # 参数契约

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.fixup import fix_tool_args_with_model, model_repair  # 被测
from app.agent.tools.tooling import ToolError  # 业务错


class _Req(BaseModel):  # 参数契约
    order_id: str = Field(min_length=5)  # 最短 5


class _FakeModel:  # 假模型（返回固定文本）
    def __init__(self, text: str) -> None:  # 构造
        self.text = text  # 返回文本

    async def ainvoke(self, _messages) -> SimpleNamespace:  # 调用
        return SimpleNamespace(content=self.text)  # 响应


def test_model_repair_parses_dict() -> None:  # 模型给出合法 JSON → 解析为 dict
    model = _FakeModel(json.dumps({"order_id": "12345"}))  # 合法修复
    out = __import__("asyncio").run(model_repair(model, {"order_id": "x"}, _Req, [{ "loc": ["order_id"], "msg": "短", "type": "x" }]))  # 调用
    assert out == {"order_id": "12345"}  # 正确解析


def test_model_repair_bad_json_returns_none() -> None:  # 模型输出非 JSON → None
    model = _FakeModel("not json")  # 坏输出
    out = __import__("asyncio").run(model_repair(model, {"order_id": "x"}, _Req, []))  # 调用
    assert out is None  # 未修好


def test_fix_with_model_converges() -> None:  # 有模型：修复收敛
    model = _FakeModel(json.dumps({"order_id": "12345"}))  # 一版即合法
    out = __import__("asyncio").run(fix_tool_args_with_model({"order_id": "x"}, _Req, model=model))  # 修复
    assert out == {"order_id": "12345"}  # 收敛


def test_fix_with_model_exhausted_throws() -> None:  # 有模型但修不好 → 抛协议错
    model = _FakeModel(json.dumps({"order_id": "x"}))  # 恒非法
    with pytest.raises(ToolError) as exc:  # 应抛
        __import__("asyncio").run(fix_tool_args_with_model({"order_id": "x"}, _Req, model=model, limit=2))  # 两次
    assert exc.value.code == str(ErrorCode.PROTOCOL_ARG_INVALID)  # 参数错


def test_fix_no_model_raises_clearly(monkeypatch) -> None:  # 无可用模型：明确失败
    import app.agent.tools.fixup as fixup  # 模块
    monkeypatch.setattr(fixup, "get_chat_model", lambda: None)  # 默认模型不可用
    with pytest.raises(ToolError) as exc:  # 应抛
        __import__("asyncio").run(fix_tool_args_with_model({"order_id": "x"}, _Req))  # 不传 model
    assert "无可用模型" in exc.value.message  # 说明无模型


def test_fix_valid_first_try() -> None:  # 首轮合法：不调模型直接返回
    model = _FakeModel("should not be used")  # 不应被调用
    out = __import__("asyncio").run(fix_tool_args_with_model({"order_id": "abcde"}, _Req, model=model))  # 合法
    assert out == {"order_id": "abcde"}  # 原样返回