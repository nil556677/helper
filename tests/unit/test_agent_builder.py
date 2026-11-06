"""官方 agent 构造与取消中间件：栈顺序、可构造、取消能抛。"""

from __future__ import annotations  # 延迟求值注解

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # 假模型
from langchain_core.messages import AIMessage  # 消息

from app.agent.agent_builder import build_react_agent, default_middleware  # 构造器
from app.agent.context import RunContext, run_context  # 上下文
from app.agent.middleware.agent_mw import CancelAgentMiddleware  # 取消中间件
from app.agent.tools.library import current_time  # 一个真实工具


def test_default_middleware_order_and_types() -> None:  # 栈顺序/类型
    stack = default_middleware()  # 默认栈
    assert stack[0].name == "cancel"  # 取消最外
    assert any(type(item).__name__ == "ModelCallLimitMiddleware" for item in stack)  # 限额在
    assert sum(1 for item in stack if type(item).__name__ == "PIIMiddleware") == 2  # 两类 PII


def test_build_react_agent_accepts_tools_and_middleware() -> None:  # 可构造
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))  # 假模型
    agent = build_react_agent(model, [current_time], "系统提示")  # 构造
    assert hasattr(agent, "ainvoke")  # 是编译图


def test_cancel_middleware_raises_when_cancelled() -> None:  # 取消能抛
    import asyncio  # 钩子已改 async

    ctx = RunContext(run_id="r", question="q")  # 上下文
    ctx.cancel_event.set()  # 已取消
    with run_context(ctx):  # 绑定
        middleware = CancelAgentMiddleware()  # 中间件
        try:  # 期望抛
            asyncio.run(middleware.abefore_model({}, None))  # 模型前检查（await 钩子）
        except Exception as exc:  # 捕获
            assert type(exc).__name__ == "RunCancelled"  # 正确异常
        else:  # 没抛
            raise AssertionError("取消后应抛 RunCancelled")  # 失败