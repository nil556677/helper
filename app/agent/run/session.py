"""RunSession：UI 对着运行会话的句柄（组合 RunGateway）。

把 ``client.events`` 订阅进一个 asyncio.Queue，并提供 cancel/approve/run_id；
断线按 ``Last-Event-ID`` 续传，任何路径都保证放 SENTINEL。
"""
from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 队列/任务
from typing import Any  # 任意值

import httpx  # 连接错误判定
from loguru import logger  # 排障日志

from app.agent.run.gateway import RunGateway  # 网关协议
from app.agent.run.types import SENTINEL, RunInput  # 入参与结束标记

#: 断线重连上限（次）；超过就结束本轮，避免 UI 永久挂着等一个不存在的 worker。
_MAX_RETRIES = 5

#: 正在跑的控制请求任务（cancel/approve）：**保活**，见 :meth:`RunSession._spawn`。
_PENDING_TASKS: set[asyncio.Task] = set()


def _split_event(item: Any) -> tuple[int | None, Any]:  # 归一化事件项
    """把两种事件形状统一成 ``(seq, event)``。

    - ``HttpClient.events`` 产出 ``(seq, event)``：seq 供 ``Last-Event-ID`` 续传；
    - 假客户端/本地产出裸 ``Event``：无 seq。

    裸事件返回 ``(None, event)``，调用方用 ``seq is None`` 判断是否更新续传点。
    """
    if isinstance(item, tuple) and len(item) == 2:  # (seq, event) 元组
        return item  # 原样返回
    return None, item  # 裸事件：无 seq


class RunSession:  # UI 句柄适配器
    """模拟 ``StreamHandle``：events(asyncio.Queue) + cancel() + approve() + run_id。"""

    SENTINEL = SENTINEL  # 结束标记（供 UI 比较）

    def __init__(self, client: RunGateway, run_id: str) -> None:  # 构造
        self.client = client  # 网关
        self.run_id = run_id  # run id
        self.events: asyncio.Queue = asyncio.Queue()  # 事件队列（UI 从这里 get）
        self._task: asyncio.Task | None = None  # 后台消费任务

    @classmethod
    async def create(cls, client: RunGateway, run_input: RunInput) -> RunSession:  # 启动
        """启动一轮并返回可消费的会话。"""
        run_id = await client.start(run_input)  # 启动
        session = cls(client, run_id)  # 建
        loop = asyncio.get_running_loop()  # 事件循环
        session._task = loop.create_task(session._feed())  # 起后台消费
        return session  # 返回

    async def _feed(self) -> None:  # 后台消费（断线续传）
        """消费 ``client.events`` 填进队列；**任何路径**都保证放 sentinel。"""
        last = "0"  # 续传点
        retries = 0  # 重连计数
        # 必须 try/finally 收尾：任何异常都不能让 UI 等不到结束标记。
        try:
            while True:  # 重连循环
                try:  # 本次流
                    async for item in self.client.events(self.run_id, last_event_id=last):  # 订阅
                        seq, event = _split_event(item)  # 归一化
                        if seq is not None:  # 有 seq
                            last = str(seq)  # 记录续传点
                        self.events.put_nowait(event)  # 入队
                    break  # 正常结束
                except (httpx.HTTPError, OSError) as exc:  # 网络抖动
                    retries += 1  # 递增
                    if retries > _MAX_RETRIES:  # 超上限
                        logger.warning("事件流重连 {} 次仍失败，本轮结束：{}", _MAX_RETRIES, exc)  # 留痕
                        break  # 放弃
                    await asyncio.sleep(0.5 * retries)  # 退避
                except Exception as exc:  # 非网络错误
                    logger.opt(exception=exc).error("订阅事件流失败，本轮结束")  # 留痕
                    break  # 结束
        finally:  # 无论怎样都放结束标记
            self.events.put_nowait(SENTINEL)  # 收尾

    def cancel(self) -> None:  # 取消（同步可调）
        """POST 取消；worker 会回 cancelled 终态从而自然结束 feed。"""
        self._spawn(self.client.cancel(self.run_id))  # 异步发

    def approve(self, request_id: str, allow: bool) -> None:  # 审批（同步可调）
        self._spawn(self.client.approve(self.run_id, request_id, allow))  # 异步发

    @staticmethod
    def _spawn(coro: Any) -> None:  # 起任务
        """有 loop 就起；没有（极少）就忽略。"""
        try:  # 取当前循环
            loop = asyncio.get_running_loop()  # 运行中
        except RuntimeError:  # 没有循环
            coro.close()  # 关，避免 "coroutine was never awaited"
            return  # 结束
        # 必须持有引用，否则任务可能被 GC 掉，cancel/approve 静默丢失。
        task = loop.create_task(coro)  # 起
        _PENDING_TASKS.add(task)  # 保活
        task.add_done_callback(_PENDING_TASKS.discard)  # 完成即移除
