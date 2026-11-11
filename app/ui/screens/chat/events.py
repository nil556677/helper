"""聊天页的事件消费（把流式事件应用到实时视图上）。"""

from __future__ import annotations

import asyncio  # 打字效果的推进任务
import time
from typing import Any

from app.agent.contracts import (  # 契约层直连（消除对旧门面的协议耦合）
    RETRY_KIND_LABELS,  # 重试类别文案
    EventEnvelope,  # 事件信封
    EventType,  # 事件类型
)
from app.ui.screens.chat.citations import _render_citations
from app.ui.screens.chat.live import (
    _clear_clarify,
    _refresh_header,
    _render_approval,
    _render_basis,
    _render_clarify,
    _render_steps,
    _set_details,
    _upsert_step,
)
from app.ui.screens.chat.state import _RENDER_INTERVAL_S, _alive
from app.ui.state.chat_model import Fragment, Process, Step, Turn

#: 打字游标。
_CURSOR = " ▍"  # 出字中的光标

#: 打字速度（每帧推进的字符数）。
_REVEAL_PER_STEP = 6  # 每步 6 字符

#: 打字步进间隔（秒）。
_REVEAL_STEP_S = 0.04  # 40ms 一步 → 最快 ~150 字/秒

#: 工具类别 → 人话标题（扩展点：新增工具类别加一行即可）。
_TOOL_TITLES: dict[str, str] = {
    "read": "读取文件",  # 读
    "write": "写入文件",  # 写
    "edit": "编辑文件",  # 改
    "bash": "运行命令",  # 命令
    "search": "查找资料",  # 检索
    "weather": "查询天气",  # 天气
    "time": "查询时间",  # 时间
}


def _fragment_list(items: list[dict[str, Any]]) -> list[Fragment]:  # 事件转 Fragment
    """过滤成 UI 契约字段。"""
    result: list[Fragment] = []  # 结果
    for item in items:  # 逐条
        result.append(  # 追加
            Fragment(  # 片段
                index=int(item.get("index") or 0),  # 序号
                source_name=str(item.get("source_name") or "资料"),  # 来源
                origin=str(item.get("origin") or "资料"),  # 出处
                score=float(item.get("score") or 0.0),  # 分数
                text=str(item.get("text") or ""),  # 正文
                snippet=str(item.get("snippet") or ""),  # 命中段
                score_kind=str(item.get("score_kind") or "rrf"),  # 分数语义
                doc_id=int(item.get("doc_id") or 0),  # 所属文档（查看整篇用）
            )  # Fragment 结束
        )  # 追加结束
    return result  # 返回


def _render_answer(view: dict[str, Any], *, force: bool = False) -> None:  # 画答案
    """合并渲染：最多 20 次/秒；终态用 force 保证最后一帧不丢。

    ``answer_text`` 始终保留**完整**答案（数据与渲染解耦）；显示用 ``answer_shown`` 游标
    逐字揭示（打字效果），未启用揭示（REPLACE/历史）时游标默认取全长。
    """
    if not view.get("answer_dirty"):  # 没有新内容
        return  # 跳过
    if view.get("answer_md") is None or not _alive(view):  # 没有可画的 DOM
        view["answer_dirty"] = False  # 清标记（数据仍在 answer_text 里）
        return  # 跳过
    now = time.perf_counter()  # 当前时刻
    if not force and now - view.get("answer_at", 0.0) < _RENDER_INTERVAL_S:  # 还没到下一帧
        return  # 先攒着
    full = str(view.get("answer_text") or "")  # 完整答案
    shown = view.get("answer_shown")  # 已揭示游标
    if shown is None:  # 未启用揭示（历史/REPLACE）
        shown = len(full)  # 全显
    text = full[: int(shown)]  # 只显示前缀
    view["answer_md"].content = text + (  # 前缀 + 出字光标
        _CURSOR if view.get("streaming") and text else ""  # 出字中才带光标
    )  # 内容结束
    view["answer_md"].update()  # 下发 DOM
    view["answer_at"] = now  # 记时刻
    view["answer_dirty"] = False  # 清标记


def _render_frame(view: dict[str, Any]) -> None:  # 打字效果逐帧渲染（绕过节流，保证流畅）
    """揭示游标前进时直接更新 DOM（不走 _render_answer 的 20fps 节流）。"""
    if view.get("answer_md") is None or not _alive(view):  # DOM 没了
        return  # 停
    full = str(view.get("answer_text") or "")  # 完整答案
    shown = int(view.get("answer_shown") or 0)  # 游标
    text = full[:shown]  # 前缀
    view["answer_md"].content = text + (  # 前缀 + 光标
        _CURSOR if view.get("streaming") and text else ""  # 出字中
    )  # 内容结束
    view["answer_md"].update()  # 下发


async def _typewriter_loop(view: dict[str, Any]) -> None:  # 打字效果后台任务
    """逐字推进 ``answer_shown`` 直到追平 ``answer_text`` 或出字结束。"""
    try:  # 任何异常都不能拖垮事件流
        while _alive(view):  # 视图还活着
            full = str(view.get("answer_text") or "")  # 目标
            shown = int(view.get("answer_shown") or 0)  # 当前
            if shown < len(full):  # 还没追平
                view["answer_shown"] = min(shown + _REVEAL_PER_STEP, len(full))  # 前进
                _render_frame(view)  # 画一帧
                await asyncio.sleep(_REVEAL_STEP_S)  # 等一拍
            elif view.get("streaming"):  # 追平但还在出字
                await asyncio.sleep(_REVEAL_STEP_S)  # 等更多 delta
            else:  # 追平且已出完
                break  # 收工
    except Exception:  # 被取消/异常
        pass  # 静默


def _start_typewriter(view: dict[str, Any]) -> None:  # 确保打字效果在跑
    """"``answer_shown`` 默认 0（从头揭示）；没在跑就起一个后台任务。"""
    if view.get("answer_shown") is None:  # 新一轮首次 delta
        view["answer_shown"] = 0  # 从头揭示
    if view.get("_typewriter_task") is None or view["_typewriter_task"].done():  # 没有任务
        try:  # 起任务（跑在 nicegui 事件循环）
            view["_typewriter_task"] = asyncio.get_running_loop().create_task(_typewriter_loop(view))  # 起
        except RuntimeError:  # 没有 loop（异常路径）
            view["answer_shown"] = None  # 回退全显
            view["answer_dirty"] = True  # 标脏
            _render_answer(view, force=True)  # 立即全显


def _finish_typewriter(view: dict[str, Any]) -> None:  # 打字收尾：取消任务 + 立即全显
    """"终态/停写：取消后台循环，游标拉到全文，一次渲染兜底。"""
    task = view.get("_typewriter_task")  # 任务
    if task is not None and not task.done():  # 还在跑
        task.cancel()  # 取消
    view["_typewriter_task"] = None  # 清引用
    view["answer_shown"] = len(str(view.get("answer_text") or ""))  # 全显
    view["answer_dirty"] = True  # 标脏
    _render_answer(view, force=True)  # 兜底渲染


def _render_live_reasoning(view: dict[str, Any]) -> None:  # 画思考（实时）
    """同样合并渲染：一个思考 chunk 会触发三次更新，不合并更贵。"""
    if not view.get("reasoning_dirty"):  # 没有新内容
        return  # 跳过
    if view.get("reasoning_row") is None or not _alive(view):  # 没有可画的 DOM
        view["reasoning_dirty"] = False  # 清标记
        return  # 跳过
    now = time.perf_counter()  # 当前时刻
    if now - view["reasoning_at"] < _RENDER_INTERVAL_S:  # 还没到下一帧
        return  # 先攒着
    view["reasoning_row"].visible = True  # 有内容才显示
    view["reasoning_md"].content = view["reasoning_text"]  # 用累积全文重画
    lines = [line.strip() for line in view["reasoning_text"].splitlines() if line.strip()]  # 末行
    view["reasoning_preview"].text = lines[-1][:48] if lines else ""  # 预览
    view["reasoning_md"].update()  # 刷新正文
    view["reasoning_preview"].update()  # 刷新预览
    view["reasoning_row"].update()  # 刷新外壳
    view["reasoning_at"] = now  # 记时刻
    view["reasoning_dirty"] = False  # 清标记


def _apply_live_event(view: dict[str, Any], event: EventEnvelope) -> None:  # 处理事件
    """按事件类型更新视图；载荷在 event.payload（类型化模型）。"""
    payload = event.payload  # 类型化载荷
    if event.type is EventType.ACK:  # 已收到
        view["run_state"] = "running"  # 状态
        _refresh_header(view)  # 组头
        return  # 结束
    if event.type is EventType.STATE:  # 状态迁移
        view["run_state"] = payload.after  # 记录
        _refresh_header(view)  # 组头
        return  # 结束
    if event.type is EventType.STAGE:  # 步骤：加进时间线（按发生顺序，不再按固定分组归并）
        _upsert_step(  # 更新/追加
            view,  # 视图
            payload.stage_id,  # 步骤 id
            group=payload.group,  # 分组（仅选图标，所有步骤都展示）
            title=payload.title or payload.stage_id,  # 人话标题
            status=payload.status,  # 状态
            detail=payload.detail,  # 明细（一行摘要）
            body=payload.body,  # 展开正文（资料/判断/校验结论）
            ms=payload.duration_ms,  # 耗时
        )  # 更新结束
        return  # 结束
    if event.type is EventType.RETRY:  # 重试
        kind = RETRY_KIND_LABELS.get(payload.error_kind, payload.error_kind or "错误")  # 类别说明
        wait = f"，{payload.delay_ms / 1000:.1f}s 后继续" if payload.delay_ms else ""  # 等待
        view["retry_note"] = f"第 {payload.attempt}/{payload.max_attempts} 次重试：{kind}{wait}"  # 一句话
        _render_steps(view)  # 时间线带上提示
        return  # 结束
    if event.type is EventType.CONTEXT:  # 上下文
        view["context"] = payload.model_dump()  # 记录
        return  # 结束
    if event.type is EventType.PII:  # 脱敏
        view["redactions"] = payload.model_dump()  # 记录
        return  # 结束
    if event.type is EventType.TOOL:  # 工具：作为时间线的一步（子项带参数/结果）
        call_id = str(payload.call_id or payload.tool_name)  # id
        kind_label = _TOOL_TITLES.get(payload.variant, str(payload.tool_name or "工具"))  # 人话标题
        children: list[dict] = []  # 子项
        if payload.diff:  # 有改动
            children.append({"id": f"{call_id}.diff", "group": "tool", "title": "查看改动", "status": "done", "detail": payload.diff, "ms": 0, "children": []})  # 改动子项
        _upsert_step(  # 更新/追加工具步骤
            view,  # 视图
            call_id,  # 工具 id
            group="tool",  # 工具分组（图标））
            title=f"{kind_label} · {payload.tool_name}" if payload.variant in _TOOL_TITLES else str(payload.tool_name or kind_label),  # 标题
            status=payload.status,  # 状态
            detail=payload.detail,  # 明细
            body=payload.diff or "",  # 展开正文（改动/输出）
            children=children,  # 子项
        )  # 更新结束
        return  # 结束
    if event.type is EventType.APPROVAL:  # 审批
        _render_approval(view, payload.model_dump())  # 画审批卡
        return  # 结束
    if event.type is EventType.CLARIFY:  # 澄清待办（HITL 暂停）
        view["run_state"] = "waiting"  # 等待用户
        _refresh_header(view)  # 组头
        _render_clarify(view, payload.model_dump())  # 画澄清卡
        return  # 结束
    if event.type is EventType.BASIS:  # 检索依据
        view["basis"] = payload.model_dump()  # 记录
        _render_basis(view)  # 渲染
        return  # 结束
    if event.type is EventType.SETTINGS:  # 本轮设置
        view["settings"] = {**payload.model_dump(), **dict(payload.extra or {})}  # 合并额外参数
        return  # 结束
    if event.type is EventType.REASONING:  # 思考流
        text = payload.text  # 文本
        if text:  # 非空
            view["reasoning_text"] = view.get("reasoning_text", "") + text  # 累积
            view["reasoning_dirty"] = True  # 标脏
            _render_live_reasoning(view)  # 合并刷新
        return  # 结束
    if event.type is EventType.USAGE:  # 用量
        view["usage_data"] = {**payload.model_dump(), **dict(payload.extra or {})}  # 合并缓存明细
        _render_usage(view)  # 底部
        return  # 结束
    if event.type is EventType.DELTA:  # 增量
        text = payload.text  # 文本
        if not text:  # 空帧
            return  # 跳过
        view["answer_text"] += text  # 累积（数据必须完整，与渲染解耦）
        view["streaming"] = True  # 还在出字（末尾显示光标）
        _start_typewriter(view)  # 起/续打字效果（后台逐字揭示）
        return  # 结束
    if event.type is EventType.CITATION:  # 引用
        view["citations"] = list(payload.items)  # 记录
        if view["citations_box"] is not None and _alive(view):  # 有引用区
            view["citations_box"].clear()  # 清空
            with view["citations_box"]:  # 重建
                _render_citations(_fragment_list(view["citations"]))  # 渲染
        _refresh_header(view)  # 更新引用数
        return  # 结束
    if event.type is EventType.REPLACE:  # 整段替换
        view["answer_text"] = payload.text  # 换掉已经流出的正文
        view["streaming"] = False  # 替换视为整段（不再打字）
        _finish_typewriter(view)  # 取消打字 + 全显
        _render_answer(view, force=True)  # 立即重画
        return  # 结束
    if event.type is EventType.VERIFY:  # 引用校验结论
        view["verify"] = payload.model_dump()  # 记录（后面对角标标灰/标失效）
        return  # 结束
    if event.type is EventType.CONFLICT:  # 资料冲突
        text = payload.message  # 文本
        view["conflict"] = text  # 记录
        if view["conflict_label"] is not None and _alive(view) and text:  # 有 UI
            view["conflict_label"].text = f"⚠️ 资料与通用知识可能冲突：{text}"  # 提示
            view["conflict_label"].visible = True  # 显示
            view["conflict_label"].update()  # 刷新
        return  # 结束
    if event.type is EventType.NOTE:  # 说明
        view["note"] = payload.text  # 文本
        view["note_kind"] = payload.note_kind  # 样式
        _show_note(view)  # 渲染
        return  # 结束
    if event.type is EventType.DONE:  # 完成
        view["streaming"] = False  # 不再出字（去掉末尾光标）
        _finish_typewriter(view)  # 收尾：取消打字 + 全显
        view["answer_dirty"] = True  # 强制重画一次，光标才会消失
        _render_answer(view, force=True)  # 最后一帧必须落地
        view["total_ms"] = payload.total_ms  # 耗时
        view["ttft_ms"] = payload.ttft_ms  # 首字
        view["usage_data"] = dict(payload.usage)  # 用量
        view["run_state"] = "done"  # 状态
        _clear_clarify(view)  # 终态清卡（去掉澄清残留）
        _refresh_header(view)  # 组头
        _render_usage(view)  # 底部用量
        return  # 结束
    if event.type is EventType.CANCELLED:  # 停止
        view["streaming"] = False  # 不再出字
        _finish_typewriter(view)  # 收尾：取消打字 + 全显已生成部分
        view["answer_dirty"] = True  # 强制重画（去光标）
        _render_answer(view, force=True)  # 已生成的部分要先画出来
        view["run_state"] = "cancelled"  # 状态
        _clear_clarify(view)  # 终态清卡
        view["note"] = "已停止生成。"  # 说明
        view["note_kind"] = "plain"  # 普通
        _show_note(view)  # 渲染
        _refresh_header(view)  # 组头
        _set_details(view, True)  # 自动展开
        return  # 结束
    if event.type is EventType.ERROR:  # 失败
        view["streaming"] = False  # 不再出字
        _finish_typewriter(view)  # 收尾：取消打字 + 全显已生成部分
        view["answer_dirty"] = True  # 强制重画（去光标）
        _render_answer(view, force=True)  # 错误前已生成的部分不能丢
        view["run_state"] = "error"  # 状态
        _clear_clarify(view)  # 终态清卡
        view["note"] = payload.message  # 说明
        view["note_kind"] = "error"  # 错误色
        _show_note(view)  # 渲染
        _refresh_header(view)  # 组头
        _set_details(view, True)  # 自动展开
        return  # 结束


def _render_usage(view: dict[str, Any]) -> None:  # 底部用量
    """引用数 + 字数 + 耗时，用户能看懂。"""
    if view["usage"] is None or not _alive(view):  # 没 UI
        return  # 跳过
    parts: list[str] = []  # 片段
    if view["citations"]:  # 有引用
        parts.append(f"{len(view['citations'])} 段引用")  # 引用
    if view["answer_text"]:  # 有答案
        parts.append(f"{len(view['answer_text'])} 字")  # 字数
    if view["total_ms"]:  # 有耗时
        parts.append(f"{view['total_ms'] / 1000:.1f}s")  # 秒
    usage = view.get("usage_data") or {}  # 用量明细
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):  # token
        parts.append(f"输入 {int(usage.get('prompt_tokens') or 0)} / 输出 {int(usage.get('completion_tokens') or 0)}")  # token
    if usage.get("cache_read_tokens"):  # 缓存命中
        parts.append(f"缓存命中 {int(usage['cache_read_tokens'])}")  # 缓存
    view["usage"].text = " · ".join(parts)  # 文案
    view["usage"].update()  # 刷新


def _show_note(view: dict[str, Any]) -> None:  # 显示说明
    """没有答案/出错时显示一句。"""
    label = view["note_label"]  # 标签
    if label is None or not view["note"] or not _alive(view):  # 没有 UI 或空说明
        return  # 不画
    label.text = view["note"]  # 文本
    label.classes(replace="hp-note-error" if view["note_kind"] == "error" else "hp-hint")  # 样式
    label.update()  # 刷新


def _turn_from_live(view: dict[str, Any]) -> Turn:  # 实时视图转 Turn
    """有序步骤 + 答案 + 引用 + 检索依据，供历史重画（与展开的时间线**同一份 steps**）。"""
    steps: list[Step] = []  # 步骤
    for item in view["steps"]:  # 逐条（按发生顺序））
        steps.append(  # 追加
            Step(  # 步骤
                group=str(item.get("group") or ""),  # 分组
                title=str(item.get("title") or ""),  # 标题
                detail=str(item.get("detail") or ""),  # 明细
                body=str(item.get("body") or ""),  # 展开正文
                status=str(item.get("status") or "done"),  # 状态
                ms=int(item.get("ms") or 0),  # 耗时
                children=[  # 子项（递归映射））
                    Step(  # 子步骤
                        group=str(c.get("group") or ""),  # 分组
                        title=str(c.get("title") or ""),  # 标题
                        detail=str(c.get("detail") or ""),  # 明细
                        status=str(c.get("status") or "done"),  # 状态
                        ms=int(c.get("ms") or 0),  # 耗时
                    )
                    for c in (item.get("children") or []) if isinstance(c, dict)  # 逐个
                ],
            )  # 步骤结束
        )  # 追加结束
    process = Process(  # 过程组
        running="",  # 已完成
        done=str(view["headline"].text if view["headline"] is not None else "完成"),  # 标题
        steps=steps,  # 步骤（有序时间线）
        reasoning=view["reasoning_text"],  # 思考
    ) if steps or view["reasoning_text"] else None  # 有内容才建
    usage = {  # 本轮用量
        "total_ms": view["total_ms"],  # 耗时
        "ttft_ms": view["ttft_ms"],  # 首字
        "answer_chars": len(view["answer_text"]),  # 字数
        "citations": len(view["citations"]),  # 引用
    }  # 用量结束
    return Turn(  # 返回
        process=process,  # 过程
        answer=view["answer_text"],  # 答案
        fragments=_fragment_list(view["citations"]),  # 引用
        note=view["note"],  # 说明
        note_kind=view["note_kind"],  # 样式
        conflict=view["conflict"],  # 冲突提示
        basis=dict(view["basis"]),  # 检索依据
        settings=dict(view["settings"]),  # 设置
        usage=usage,  # 用量
        context_usage=dict(view.get("context") or {}),  # 上下文占用（分块）
    )  # 结束
