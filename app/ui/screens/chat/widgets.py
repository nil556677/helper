"""聊天页共用小件（用户气泡、附件、答案正文、依据正文）。"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from nicegui import ui

from app.services import chat_stats
from app.ui.screens.chat.state import _HISTORY_PREVIEW_CHARS, _STEP_ICONS


def _render_attachments(items: list[dict]) -> None:  # 气泡下附件
    """文件名 + 大小；图片给缩略图（点开新窗口看原图），文件缺失回落成图标。"""
    if not items:  # 没有附件
        return  # 跳过
    with ui.column().classes("w-full justify-end gap-1 pt-1 items-end"):  # 右侧排附件
        for item in items:  # 逐个
            name = str(item.get("name") or "附件")  # 名
            size = int(item.get("size") or 0)  # 大小
            path = Path(str(item.get("path") or ""))  # 路径
            meta = []  # 元信息
            if size:  # 有大小
                meta.append(f"{size / 1024:.0f}KB")  # KB
            if item.get("kind") == "image" and path.is_absolute() and path.is_file():  # 图片存在
                with ui.row().classes("items-center gap-2 hp-attachment-chip"):  # 芯片
                    with ui.image(str(path)).classes("hp-attach-thumb").props("cursor-pointer"):  # 缩略图
                        ui.link("", str(path), new_tab=True).classes("absolute inset-0")  # 点开大图
                    ui.label(name)  # 文件名
                    if meta:  # 有元信息
                        ui.label(" · ".join(meta)).classes("hp-field-help")  # 大小
                continue  # 下一个
            with ui.row().classes("items-center gap-1 hp-attachment-chip"):  # 文件/缺失
                ui.icon("attach_file").classes("text-sm")  # 图标
                ui.label(name).classes("hp-field-help")  # 名
                if meta:  # 有元信息
                    ui.label(" · ".join(meta)).classes("hp-field-help")  # 大小


def _seg_width(tokens: int) -> str:
    """一段占参考窗口的百分比宽度（超出窗口就满条，交给 bar 的 overflow 剪掉）。"""
    # 路径：min(..., 1.0) 把超过 100% 的段夹到 100%，其余按真实比例。
    share = min(tokens / chat_stats.CONTEXT_LIMIT, 1.0)
    return f'{share * 100:.2f}%'


def _build_pending(stream: Any, *, question: str, attachments: list[dict] | None = None) -> Any:
    """先画用户气泡 + 附件，返回这一轮的容器；实时内容由 _build_live 填。

    不再建秒表/等待行：状态与步骤由事件流即时渲染，避免秒表更新已删除元素。
    """
    # 路径：先画用户气泡（立刻可见），再建这一轮的容器（后续只替换容器里的内容）。
    with stream:
        ui.label(question).classes('hp-bubble-user')
        _render_attachments(attachments or [])
        box = ui.column().classes('w-full hp-turn')
    return box


def _step_dict(step: Any) -> dict[str, Any]:  # Step(dataclass) 或 dict → 统一 dict
    """历史给 Step 对象、实时给 dict；这里归一，渲染器只认 dict（单一事实）。"""
    if isinstance(step, dict):  # 已是 dict
        return dict(step)  # 原样
    return {  # dataclass Step
        "group": str(getattr(step, "group", "")),  # 分组
        "title": str(getattr(step, "title", "")),  # 标题
        "detail": str(getattr(step, "detail", "")),  # 明细
        "status": str(getattr(step, "status", "done")),  # 状态
        "ms": int(getattr(step, "ms", 0) or 0),  # 耗时
        "children": [_step_dict(c) for c in getattr(step, "children", [])],  # 子项递归
    }  # 返回


def _render_steps_flow(steps: list[Any], *, live: bool = False) -> None:  # 时间线渲染器（实时/历史共用）
    """一条垂直时间线：圆点 + 连线 + 每步一行，子步骤缩进。**单一事实源**。

    在当前 NiceGUI 上下文里直接画；live 仅作语义位（历史不滚）。
    """
    entries = [_step_dict(s) for s in steps]  # 归一
    if not entries:  # 没有步骤
        return  # 跳过
    for idx, step in enumerate(entries):  # 逐步骤（保持发生顺序）
        group = str(step.get("group") or "")  # 分组
        title = str(step.get("title") or "")  # 标题
        detail = str(step.get("detail") or "")  # 明细
        status = str(step.get("status") or "done")  # 状态
        ms = int(step.get("ms") or 0)  # 耗时
        icon = _STEP_ICONS.get(group, "chevron_right")  # 图标
        marks = {"running": "…", "done": "✓", "failed": "✗", "skipped": "·"}  # 状态符号
        with ui.row().classes("w-full items-stretch gap-2 no-wrap hp-timeline-row"):  # 时间线行
            with ui.column().classes("hp-timeline-rail").style("width:20px;"):  # 圆点+连线轨
                with contextlib.suppress(Exception):  # 首行无上连线
                    if idx > 0:  # 非首行
                        ui.element("div").classes("hp-timeline-connector").style("flex:1;")  # 上连线
                ui.label(marks.get(status, "·")).classes("hp-timeline-dot").style(  # 状态点
                    "color:%s;" % ("var(--hp-accent)" if status == "running" else ("var(--hp-danger)" if status == "failed" else "var(--hp-label-tertiary)"))
                )  # 点结束
                with contextlib.suppress(Exception):  # 末行无下连线
                    if idx < len(entries) - 1:  # 非末行
                        ui.element("div").classes("hp-timeline-connector").style("flex:1;")  # 下连线
            with ui.column().classes("w-full gap-0 pt-1"):  # 该步内容
                with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 该步头
                    ui.icon(icon).classes("text-sm hp-step-icon")  # 图标
                    ui.label(title).classes("hp-step-title")  # 标题
                    if detail:  # 有明细
                        ui.label(detail).classes("hp-field-help").style("overflow:hidden;text-overflow:ellipsis")  # 一行
                    children = step.get("children") or []  # 子项
                    if children:  # 有子项
                        ui.label(f"{len(children)} 项").classes("hp-hint")  # 数量
                    ui.space()  # 撑开
                    if step.get("status") == "failed":  # 失败
                        ui.icon("error").classes("text-sm hp-step-error")  # 错误图标
                    if ms:  # 有耗时
                        ui.label(f"{ms}ms").classes("hp-field-help")  # 耗时
                if children:  # 子步骤缩排
                    with ui.column().classes("w-full gap-0 pl-6"):  # 缩进区
                        _render_steps_flow(children, live=False)  # 递归子项（不再滚）


def _preview_text(text: str, limit: int = 400) -> str:  # 长答案的预览
    """截到最近的段落/句子边界，别把表格或代码块切一半。"""
    if len(text) <= limit:  # 本来就不长
        return text  # 原样
    cut = text[:limit]  # 先粗切
    for sep in ("\n\n", "\n", "。", "；"):  # 找最近的边界
        position = cut.rfind(sep)  # 从后往前找
        if position > limit * 0.5:  # 边界不能太靠前（否则预览太短）
            return cut[: position + len(sep)].rstrip() + "\n\n…"  # 齐边界截断
    return cut.rstrip() + "…"  # 找不到边界就硬截


def _render_answer_text(text: str, *, history: bool = False) -> None:  # 画答案正文
    """实时轮直接渲染全文；历史长答案先给预览，点"展开全文"才原地渲染完整 markdown。

    为什么只对**历史**折叠：实时轮在流式增量，折叠会挡视线。
    历史长答案占大头（切会话要重建 20 轮的 DOM/解析），折叠后省一个量级。
    """
    body = str(text or "")  # 答案全文
    if not history or len(body) <= _HISTORY_PREVIEW_CHARS:  # 实时/不长的回答
        ui.markdown(body).classes("hp-markdown w-full")  # 直接渲染全文
        return  # 结束
    holder = ui.column().classes("w-full gap-1")  # 折叠容器
    with holder:  # 先画预览
        ui.markdown(_preview_text(body)).classes("hp-markdown w-full")  # 预览

    def _expand() -> None:  # 展开全文
        holder.clear()  # 清掉预览
        with holder:  # 原地重画
            ui.markdown(body).classes("hp-markdown w-full")  # 完整 markdown
        with contextlib.suppress(Exception):  # 按钮已经用完了
            button.delete()  # 删掉按钮

    button = ui.button(f"展开全文（{len(body)} 字）", on_click=_expand).props(  # 展开按钮
        "flat dense no-caps"
    ).classes("hp-field-help")  # 弱化样式


def _render_basis_content(basis: dict[str, Any]) -> None:  # 依据正文
    """关键词/计数/来源，历史与实时共用。"""
    counts = basis.get("hit_counts") or {}  # 计数
    keywords = " / ".join(basis.get("keywords") or []) or "（未提取到关键词）"  # 关键词
    ui.label(f"关键词：{keywords}").classes("hp-field-help")  # 关键词
    ui.label(  # 计数
        f"找到 {counts.get('semantic', 0)} 段 + {counts.get('keyword', 0)} 段"
        f" → 合并 {counts.get('merged', 0)} 段 → 精选 {counts.get('selected', 0)} 段"
    ).classes("hp-field-help")  # 计数
    for item in basis.get("sources") or []:  # 来源
        snippet = str(item.get("snippet") or "").strip().replace("\n", " ")[:80]  # 摘要
        ui.label(f"· {item.get('source_name', '资料')} · {item.get('origin', '')}".rstrip()).classes(
            "hp-field-help"
        )  # 行
        if snippet:  # 有摘要
            ui.label(f"  {snippet}").classes("hp-field-help")  # 摘要
