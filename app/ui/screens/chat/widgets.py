"""聊天页共用小件（用户气泡、附件、答案正文、依据正文）。"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from nicegui import ui

from app.services import chat_stats
from app.ui.screens.chat.state import _HISTORY_PREVIEW_CHARS


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


def _render_attachments(items: list[dict]) -> None:  # 气泡下附件
    """文件名 + 大小；图片给缩略图。"""
    if not items:
        return
    with ui.row().classes("w-full justify-end gap-2 flex-wrap pt-1"):
        for item in items:
            name = str(item.get("name") or "附件")
            size = int(item.get("size") or 0)
            with ui.row().classes("items-center gap-1 hp-attachment-chip"):
                if item.get("kind") == "image" and Path(str(item.get("path") or "")).is_file():  # 图片
                    ui.image(str(item["path"])).classes("hp-attach-thumb")  # 缩略图
                else:  # 文件
                    ui.icon("attach_file").classes("text-sm")  # 图标
                ui.label(name).classes("hp-field-help")
                if size:
                    ui.label(f"{size / 1024:.0f}KB").classes("hp-field-help")


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
