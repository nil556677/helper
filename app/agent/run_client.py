"""RunClient 接口与实现（E0）：UI 的唯一运行入口。

目标：UI 只依赖 ``RunClient``，不直接 import ``app.agent.*`` 重依赖。

- ``RunClient``：协议（start / events / cancel / approve / RunInput）。
- ``InProcessClient``：包装现有 ``start_stream``，仍在本进程跑，行为与旧 UI 一致
  （E0.1 过渡 + 测试用，不上网）。
- ``HttpClient``（E0.2 实现）：POST 命令 + SSE 事件流打 worker。

E0.1 边界：只抽接口 + InProcess 实现；UI 仍走老路径，现有测试行为不变。
附件：暂随 ``RunInput.attachments`` 传整份（含 text）；E0.4 改为只传 id、由 worker 读盘。
"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import AsyncIterator, Callable  # 集合抽象
from dataclasses import dataclass, field  # 数据类
from typing import Any, Protocol  # 协议

from app.agent import stream as agent_stream  # 底层流

#: 结束标记（与 stream 的 sentinel 是同一个对象）。
SENTINEL = object()  # 复用


@dataclass(frozen=True)
class RunInput:  # 一次运行的入参
    """发给 worker 的最小入参（附件 E0.4 收敛为 id）。"""

    question: str  # 问题
    conversation_id: str  # 会话 id
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 附件（暂为公开元数据，含 text）


class RunClient(Protocol):  # 运行入口协议
    """start/events/cancel/approve；UI 只依赖它。"""

    async def start(self, run_input: RunInput) -> str:  # 启动返回 run_id
        """启动一轮，返回 run_id。"""
        ...  # 协议说明

    async def events(  # 订阅事件流
        self, run_id: str, *, last_event_id: str = ""  # run_id / 断线续传点
    ) -> AsyncIterator[Any]:  # 事件流（不含 sentinel）
        """迭代产生该 run 的事件；结束后自然停止。"""
        ...  # 协议说明

    async def cancel(self, run_id: str) -> None:  # 取消
        """请求取消该 run（幂等）。"""
        ...  # 协议说明

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        """对补参/审批请求给结论。"""
        ...  # 协议说明


class InProcessClient:  # 进程内实现（过渡/测试）
    """包装 ``start_stream``，仍在本进程跑，行为与旧 UI 完全一致。"""

    def __init__(  # 构造
        self,  # 实例
        factory: Callable[..., Any] | None = None,  # 流工厂（测试注入用）
    ) -> None:  # 无返回
        self._factory = factory or agent_stream.start_stream  # 默认
        self._handles: dict[str, Any] = {}  # run_id → handle

    async def start(self, run_input: RunInput) -> str:  # 启动
        handle = self._factory(  # 开流
            run_input.question,  # 问题
            session=run_input.conversation_id,  # 会话
            attachments=list(run_input.attachments),  # 附件
        )  # 返回句柄
        self._handles[handle.run_id] = handle  # 记录
        return handle.run_id  # run id

    async def events(  # 订阅事件
        self, run_id: str, *, last_event_id: str = ""  # 参数
    ) -> AsyncIterator[Any]:  # 事件迭代器
        """从该 run 的队列取事件；sentinel 视为结束。last_event_id 进程内忽略（实时消费即可）。"""
        del last_event_id  # 进程内无重放
        handle = self._handles.get(run_id)  # 找
        if handle is None:  # 未知
            return  # 结束
        while True:  # 直到结束
            event = await handle.events.get()  # 取一条（阻塞）
            if event is agent_stream.SENTINEL:  # 结束标记
                return  # 停
            yield event  # 放行

    async def cancel(self, run_id: str) -> None:  # 取消
        handle = self._handles.get(run_id)  # 找
        if handle is not None:  # 有
            handle.cancel()  # 转发

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        handle = self._handles.get(run_id)  # 找
        if handle is not None:  # 有
            handle.approve(request_id, allow)  # 转发
