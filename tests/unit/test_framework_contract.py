"""框架版本护栏：锁定版本下我们依赖的关键能力/字段，升级框架时先在这里红。

锁定集（`requirements.txt`）：langchain 1.4.7 / langgraph 1.2.16 / langchain-openai 1.7.2。

为什么需要这一层：这些是**隐式契约**——字段名、事件键、API 位置。框架升级时改掉一处，
业务代码不会报错，只会悄悄变成「永远 0」或「走进废弃分支」。把它们钉在测试里，
升级的人会先看到红，而不是上线后才发现。
"""

from __future__ import annotations  # 类型注解延迟求值


def test_chatopenai_accepts_the_params_we_pass() -> None:  # 采样参数字段
    """我们显式传的每个名字都必须是 ChatOpenAI 的字段；top_k 例外（走 extra_body）。"""
    from langchain_openai import ChatOpenAI  # 锁定版本的模型类

    fields = set(ChatOpenAI.model_fields)  # 声明的字段
    for name in (  # 逐个核对
        "temperature",  # 温度
        "top_p",  # 核采样
        "frequency_penalty",  # 频率惩罚
        "presence_penalty",  # 存在惩罚
        "max_tokens",  # 输出上限
        "stop",  # 停止词
        "seed",  # 可复现采样
        "extra_body",  # 额外体
        "stream_usage",  # 流式用量
        "max_retries",  # SDK 重试
        "request_timeout",  # 超时（又叫 timeout 别名）
    ):  # 核对结束
        assert name in fields, f"ChatOpenAI 不再有 {name!r} 字段，升级后要改 params.to_chat_kwargs"  # 断言
    assert "top_k" not in fields, "top_k 突然成了字段：要么改走一等字段，要么确认 extra_body 仍正确"  # top_k 仍走额外体


def test_usage_metadata_uses_cache_creation_not_cache_write() -> None:  # 用量键名
    """LangChain 1.6 的输入明细键是 cache_creation；读 cache_write 会永远是 0。"""
    from langchain_core.messages.ai import InputTokenDetails  # 输入明细形状

    keys = set(InputTokenDetails.__annotations__)  # 声明的键
    assert "cache_creation" in keys  # 缓存写（正确键）
    assert "cache_read" in keys  # 缓存读
    assert "cache_write" not in keys, "框架改回 cache_write 了：同步改 gateway._usage_parts"  # 旧键不该出现


def test_agent_creation_should_use_langchain_create_agent() -> None:  # 创建智能体
    """锁定版本里 create_react_agent 已废弃，新代码用 langchain.agents.create_agent。"""
    from langchain.agents import create_agent  # 推荐入口
    from langgraph.prebuilt import create_react_agent  # 旧入口（仍可用但会告警）

    assert callable(create_agent)  # 新入口存在
    assert hasattr(create_react_agent, "__deprecated__"), "create_react_agent 的废弃标记没了：请复核 E 类方案"  # 旧入口已标记


def test_graph_primitives_and_checkpointers_available() -> None:  # 图与检查点
    """类 C/D/E 要用的原语必须都在锁定版本里。"""
    from langgraph.checkpoint.memory import InMemorySaver  # 内存检查点
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # 异步 SQLite 检查点
    from langgraph.graph import END, START, StateGraph  # 图原语
    from langgraph.graph.message import add_messages  # 消息 reducer
    from langgraph.types import Command, interrupt  # 动态边与中断

    for item in (InMemorySaver, AsyncSqliteSaver, StateGraph, START, END, add_messages, Command, interrupt):  # 逐个
        assert item is not None  # 都在


def test_trim_messages_and_callbacks_available() -> None:  # 上下文与追踪原语
    """类 C 的裁剪、类 D 的回调基类都来自 langchain-core，必须可用。"""
    from langchain_core.callbacks import BaseCallbackHandler  # 回调基类
    from langchain_core.messages import trim_messages  # 消息裁剪

    assert callable(trim_messages)  # 可用
    assert BaseCallbackHandler is not None  # 可用
