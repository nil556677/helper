"""工具层增强：异步超时强扣 / render 体积上限 / describe 透传 error_schema+examples。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 异步执行
import json  # 解析 render

from pydantic import BaseModel  # 契约

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.tooling import tool_spec  # 范式装饰器
from app.agent.tools.wiring import RENDER_MAX_CHARS, describe, render, unwrap_tool_output  # 接线/渲染
from app.core.error import ok as make_ok  # 成功包络


class _P(BaseModel):  # 参数契约
    q: str = ""  # 查询


class _Err(BaseModel):  # 错误契约示例
    code: str = ""  # 错误码
    retryable: bool = False  # 可重试


def test_async_timeout_returns_execution_timeout() -> None:  # 异步工具超时强扣
    @tool_spec(description="超时工具", args_schema=_P, timeout_ms=200)  # 200ms 门槛
    async def _slow(q: str = ""):  # 慢函数
        await asyncio.sleep(0.3)  # 超过门槛
        return {"q": q}  # （不会到这）

    env = unwrap_tool_output(asyncio.run(_slow.ainvoke({"q": "x"})))  # 跑
    assert env.ok is False  # 失败
    assert env.code == str(ErrorCode.EXECUTION_TIMEOUT)  # 超时码
    assert env.retryable is True  # 可重试


def test_render_clips_overlong() -> None:  # render 超长截断
    text = render(make_ok({"pad": "y" * 50000}))  # 50k 字符 data
    assert "…" in text and "截断" in text  # 有截断标记
    assert len(text) < 50000  # 已截断
    assert len(text) <= RENDER_MAX_CHARS + 32  # 只多提示尾部


def test_render_keeps_short() -> None:  # 短数据不受影响
    text = render(make_ok({"a": 1}))  # 小 data
    assert json.loads(text) == {"a": 1}  # 完整保留


def test_describe_passes_error_schema_and_examples() -> None:  # describe 透传 error_schema/examples
    @tool_spec(description="契约工具", args_schema=_P, error_schema=_Err, examples=("a", "b"))  # 声明
    def _enc(q: str = ""):  # 纯函数
        return {"q": q}  # data

    desc = describe(_enc)  # 描述符
    assert desc.error_schema is _Err  # 错误契约
    assert desc.examples == ("a", "b")  # 示例