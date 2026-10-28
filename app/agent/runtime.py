"""会话级运行入口：run_turn —— 批处理复用与 GUI 相同的父图（RAG-only）。

与 `stream.py` 共用装配器（`assembly.build`）与驱动器（`driver.drive`），
只差事件出口：流式把事件塞队列给 UI，批处理把事件丢弃、把图状态还原成 `Turn` 返回。
无路由/补参/工具能力（方案 A：统一到生产那条 RAG 异步管线）。
"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 一次性跑 async 图
import uuid  # run id
from typing import Any  # 任意值

from langchain_core.messages import HumanMessage  # 初始用户消息

from app.agent import assembly  # 装配器（建图）
from app.agent.context import RunContext  # 运行上下文
from app.agent.driver import drive  # 驱动器（图驱动 + 终态收敛）
from app.agent.turn import turn_from_state  # 图状态转 Turn
from app.ui.state.chat_model import Turn  # UI 答复形状


def _drop(_event: Any) -> None:  # 批处理事件出口
    """批处理不要事件流，只返回最终 Turn。"""
    return None  # 丢弃


def _graph() -> Any:  # 取（必要时建）编译图
    """委托装配器（进程内复用）；保留名字，测试可接桩。"""
    return assembly.build()  # 返回


def get_graph() -> Any:  # 取编译图（保留名字，测试可接桩）
    return _graph()  # 委托


def failure_turn(question: str, error: BaseException) -> Turn:  # 构造失败的一轮
    """一轮失败答复（只暴露类型名，不把内部细节抛给用户）。"""
    del question  # 问题不参与展示
    return Turn(note=f"本地检索出错了，稍后再试。（{type(error).__name__}）", note_kind="error")  # 失败提示


async def _run(text: str, conversation_id: str) -> Turn:  # 异步跑一轮
    """建 RunContext，委托驱动器跑父图，还原成 Turn。"""
    ctx = RunContext(  # 运行上下文
        run_id=uuid.uuid4().hex,  # 临时 id
        question=text,  # 问题
        conversation_id=conversation_id or "default",  # 会话
        emit=_drop,  # 丢弃事件
    )  # 上下文结束
    initial = {  # 初始状态
        "question": text,  # 问题
        "conversation_id": ctx.conversation_id,  # 会话
        "history": [],  # 由 load_context 填
        "attachments": [],  # 批处理不带附件
        "messages": [HumanMessage(content=text)],  # 图需要的消息
    }  # 状态结束
    result = await drive(get_graph(), ctx, initial_state=initial, emit=_drop)  # 驱动
    if result.state:  # 有最终状态
        return turn_from_state(result.state)  # 还原成 Turn
    if result.error is not None:  # 失败
        return failure_turn(text, result.error)  # 失败轮
    return failure_turn(text, RuntimeError("empty"))  # 兜底（如启动前取消）


def run_turn(  # 跑一轮问答
    text: str,  # 用户输入
    *,  # 仅关键字参数
    session: str = "",  # 会话 id
) -> Turn:  # 跑一轮问答
    """批处理入口：同步壳包异步；任何失败都返回失败轮。"""
    try:  # 图/执行失败也要能返回
        return asyncio.run(_run(text, session))  # 跑
    except Exception as exc:  # 任何异常
        return failure_turn(text, exc)  # 失败轮
