"""聊天页的发送与流式消费（从 composer 拆出）。

只关心三件事：**把消息发出去**、**消费事件流**、**把这一轮落回会话**。
界面元素与回调由 :class:`StreamRefs` **显式传入**——不允许用模块级全局（多窗口会互相覆盖）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.agent.events import error as event_error
from app.agent.run_client import RunInput
from app.agent.run_session import RunSession, get_run_client
from app.ui.components.dialog import notify
from app.ui.screens.chat.events import _apply_live_event, _turn_from_live
from app.ui.screens.chat.live import _build_live, _refresh_header
from app.ui.screens.chat.scroll import _schedule_scroll, _set_follow
from app.ui.screens.chat.state import _FIRST_EVENT_S, _RUN_CAP_S, _ActiveStream, _LiveState
from app.ui.screens.chat.widgets import _build_pending
from app.ui.state import conversations
from app.ui.state.chat_model import Entry
from app.ui.state.chat_queue import MessageQueue, QueuedMessage


@dataclass
class StreamRefs:
    """发送/消费要用到的界面引用（由 composer 建好一次性传入）。"""

    live_state: _LiveState  # 本页实时视图集合
    stream: Any  # 对话流容器（用户气泡画在里面）
    scroll: Any  # 滚动区
    box: Any  # 输入框（读值/清空/聚焦）
    queue: MessageQueue  # 待发送队列
    busy: dict[str, bool]  # 是否已有 worker 在跑
    active: _ActiveStream  # 当前流式句柄
    pending_files: list[dict[str, Any]]  # 待发送附件
    sync_button: Callable[[], None]  # 同步发送/停止按钮
    render_queue: Callable[[], None]  # 重画排队区
    render_pending_files: Callable[[], None]  # 重画附件 chip
    refresh_stats: Callable[[], None]  # 重算底栏统计
    send_button: Callable[[], Any]  # 取发送按钮元素（可能还没建）
    on_refresh: Any = None  # 收成历史轮的回调（可选）


async def execute(refs: StreamRefs, item: QueuedMessage) -> None:  # 执行一条消息
    """执行一条消息：流式事件驱动时间线与答案。"""
    started = time.perf_counter()  # 计时：从开始执行到界面出东西
    text = item.text  # 用户输入
    conversation_id = item.conversation_id  # 会话
    live_state = refs.live_state  # 简写
    live_state.active[conversation_id] = {"question": text}  # 标记这个会话有任务
    try:  # 外层保证标记一定清掉
        conversation = conversations.current()  # 当前会话
        on_current = conversation.id == conversation_id and not refs.box.is_deleted  # 是否在当前页
        if on_current and not conversation.entries:  # 第一条消息
            refs.stream.clear()  # 去掉空态
        pending = _build_pending(refs.stream, question=text, attachments=item.attachments) if on_current else None  # 用户气泡
        if pending is not None:  # 画了等待行
            refs.scroll.scroll_to(percent=1.0)  # 滚到底
        live = _build_live(live_state, pending, refs.scroll)  # 先建实时视图，启动失败也能显示错误
        _schedule_scroll(refs.scroll)  # 兜底：JS 没装上时也滚一次
        _set_follow(True)  # 提交输入 → 恢复贴底跟随（DSH 的做法）
        try:  # 启动失败不要留下空等待
            handle = await RunSession.create(  # 经 RunClient 启动（worker/本进程）
                get_run_client(),  # 进程级客户端
                RunInput(  # 入参
                    question=text,  # 问题
                    conversation_id=conversation_id,  # 会话
                    attachments=list(item.attachments),  # 附件
                ),  # 入参结束
            )  # 会话返回
        except Exception as exc:  # 启动异常
            logger.opt(exception=exc).error("启动流失败：{}", text)  # 日志
            _apply_live_event(live, event_error("ui", str(exc)))  # 错误
            if live in live_state.views:  # 注销
                live_state.views.remove(live)  # 移除
            conversations.append_to(  # 落一条失败轮
                conversation_id,  # 会话
                Entry(question=text, turn=_turn_from_live(live), attachments=item.attachments),  # 失败内容
            )  # 落库结束
            return  # 结束
        refs.active.start(handle, conversation_id)  # 记录当前流与所属会话
        live["handle"] = handle  # 审批回调用
        refs.sync_button()  # 只有当前会话才显示停止
        logger.debug(  # 诊断"点了很久才动"：这一行能看出卡在界面还是卡在检索
            "发送耗时：建视图+启动 {} ms · {}", int((time.perf_counter() - started) * 1000), text[:20]
        )  # 日志结束
        first_event = True  # 是否还没收到第一个事件

        async def _consume() -> None:  # 事件消费（_drain 靠它推进，不能让它永久挂起）
            nonlocal first_event  # 改外层标记
            while True:  # 直到 sentinel
                if first_event:  # 第一个事件应该很快就来（引擎起步就发 ack）
                    try:  # 首事件单独计时：慢了只告警，不能当成整轮超时
                        event = await asyncio.wait_for(handle.events.get(), timeout=_FIRST_EVENT_S)  # 首事件超时
                    except TimeoutError:  # 首事件慢（离线/冷启动/worker 卡顿）
                        logger.warning(  # 说清楚是哪一类超时（以前这里被外层当成 600 秒误报）
                            "首事件 {} 秒仍未到，继续等待（总上限 {} 秒）· {}",
                            _FIRST_EVENT_S,
                            _RUN_CAP_S,
                            text[:20],
                        )  # 日志结束
                        first_event = False  # 只告警一次
                        event = await handle.events.get()  # 继续等（总时长由外层 _RUN_CAP_S 兜）
                    else:  # 首事件按时到
                        first_event = False  # 标记
                        logger.debug(  # 首事件延迟（含排队与建图）
                            "首事件 {} ms · {}", int((time.perf_counter() - started) * 1000), text[:20]
                        )  # 日志结束
                else:  # 后续事件
                    event = await handle.events.get()  # 取事件
                if event is RunSession.SENTINEL:  # 结束
                    return  # 退出
                _apply_live_event(live, event)  # 更新 UI

        try:  # 消费（总时长封顶，防引擎卡死让 busy 永久占用、队列卡掉）
            await asyncio.wait_for(_consume(), timeout=_RUN_CAP_S)  # 封顶等待
        except TimeoutError:  # 引擎迟迟不结束
            logger.warning("本轮超过 {} 秒未结束，已强制中断（防队列卡死）", _RUN_CAP_S)  # 日志
            _apply_live_event(live, event_error("ui", "本轮超时，已强制中断"))  # 错误
        except Exception as exc:  # UI 消费失败
            logger.opt(exception=exc).error("流式事件消费失败：{}", text)  # 日志
            _apply_live_event(live, event_error("ui", str(exc)))  # 错误
        finally:  # 无论成功失败都恢复按钮
            refs.active.clear()  # 句柄与会话一起清
            refs.sync_button()  # 恢复发送
        turn = _turn_from_live(live)  # 还原 Turn
        if live in live_state.views:  # 注销实时视图
            live_state.views.remove(live)  # 移除
        conversations.append_to(conversation_id, Entry(question=text, turn=turn, attachments=item.attachments))  # 落会话
        live_state.active.pop(conversation_id, None)  # 先清活动标记，避免重画时还显示“正在处理”
        still_current = conversations.current().id == conversation_id and not refs.box.is_deleted  # 还在原会话
        if still_current:  # 页面还在
            if refs.on_refresh is not None:  # 有重画回调
                refs.on_refresh()  # 收成历史轮（同时更新轮次导航）
            else:  # 没有回调
                refs.refresh_stats()  # 至少刷统计
            refs.box.run_method('focus')  # 聚焦输入
            refs.scroll.scroll_to(percent=1.0)  # 滚到底
    finally:  # 清掉活动标记
        live_state.active.pop(conversation_id, None)  # 无论成功失败都移除


async def drain(refs: StreamRefs) -> None:  # 串行消费队列
    """当前一条执行完，自动开始下一条。"""
    # 路径 1：已有 worker 在跑 → 直接返回（新消息留在 queue 里，由 worker 接上）。
    if refs.busy['value']:
        return
    refs.busy['value'] = True
    try:
        # 路径 2：循环取队首执行；出队即“开始执行”，之后不可编辑/删除。
        while refs.queue:
            item = refs.queue.pop()
            if item is None:  # 防御：并发下队列刚好被清空
                break
            refs.render_queue()
            try:
                await execute(refs, item)
            except Exception:
                # 路径 2a：单条失败不拖垮整个队列（记日志，继续下一条）。
                logger.exception('排队消息执行失败')
    finally:
        # 路径 3：队列清空/异常退出都要复位，避免下次发送永远排队。
        refs.busy['value'] = False


async def send(refs: StreamRefs) -> None:  # 发送
    """发送：输入永远可用；正在检索就进队列，空闲就立即开始消费。"""
    text = (refs.box.value or '').strip()
    # 路径 1：没有文字也没有附件 → 忽略。
    if not text and not refs.pending_files:
        return
    # 路径 2：入队时统一校验上限（条数/总字符）；被拒只提示，**不清空输入**。
    reason = refs.queue.push(text, conversations.current().id, attachments=list(refs.pending_files))
    if reason is not None:
        notify(reason, kind='warning')
        return
    refs.box.value = ''
    refs.pending_files.clear()  # 附件已交给队列
    refs.render_pending_files()  # 清空 chip
    # 路径 3：空闲 → 立刻消费（不显示排队条）；忙碌 → 显示排队条等执行。
    if refs.busy['value']:
        refs.render_queue()
        return
    await drain(refs)


async def on_send_click(refs: StreamRefs) -> None:  # 按钮统一入口
    """有当前会话的流就停止，否则发送。"""
    if refs.active.belongs_to(conversations.current().id):  # 本会话在跑
        current_id = conversations.current().id  # 当前会话
        handle = refs.active.handle  # 当前流
        handle.cancel()  # 请求停止（任务级取消）
        refs.queue.clear(current_id)  # 连本会话的待发送一起清掉
        refs.render_queue()  # 重画队列
        for view in refs.live_state.views:  # 找到这一轮的实时视图
            if view.get('handle') is handle:  # 同一个流
                view['run_state'] = 'cancelling'  # 立即反馈“正在停止”
                _refresh_header(view)  # 组头改文案
                break  # 找到就停
        button = refs.send_button()  # 按钮
        if button is not None and not button.is_deleted:  # 有按钮
            button.set_enabled(False)  # 防连点
            button.props('loading')  # 转圈
            button.update()  # 刷新
        return  # 不再发送
    await send(refs)  # 空闲/别的会话在跑 → 发送当前会话的消息
