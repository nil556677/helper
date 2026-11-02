"""worker 进程内部的图执行后端（唯一的图执行实现）。"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import AsyncIterator, Callable  # 集合抽象
from typing import Any  # 任意值

from app.agent import stream as agent_stream  # 底层流
from app.agent.run.types import RunInput  # 入参


class LocalBackend:  # worker 内部后端
    """包装 ``start_stream``，在 **worker 进程**里跑图；由 run_server 暴露成 HTTP+SSE。

    不是 UI 的可选模式：UI 只能经 :class:`app.agent.run.http_client.HttpClient` 访问它。
    """

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
            run_id=run_input.run_id,  # 复用路由生成的 run_id（与发送即落库一致）
        )  # 返回句柄
        self._handles[handle.run_id] = handle  # 记录
        return handle.run_id  # run id

    async def events(  # 订阅事件
        self, run_id: str, *, last_event_id: str = ""  # 参数
    ) -> AsyncIterator[Any]:  # 事件迭代器
        """从该 run 的队列取事件；sentinel 视为结束。last_event_id 进程内忽略。"""
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
