"""知识库的共用小件：卡片头、统计格、状态徽标、解析摘要、迷你行、右栏预览。

**为什么单独一层**：三个视图都要用这几样东西，放在任一视图里其它两个就得互相 import；
放在这里，视图之间**零依赖**（只依赖服务层与这里）。
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.services import knowledge as knowledge_service
from app.ui.components.card import card_head  # noqa: F401  # 供本包其它模块用统一的卡片头
from app.ui.components.card import stat as stat  # 统一数字格
from app.ui.components.format import human_time

#: 状态 → 界面文案（四个状态 + 退场版本）
_STATUS_LABELS = {
    "pending": "待处理",
    "indexing": "处理中",
    "ready": "就绪",
    "failed": "失败",
    "superseded": "已退场",
}

#: 片段类型 → 界面文案（详情页的分块明细用）
KIND_LABELS = {
    "text": "正文",
    "table": "表格",
    "code": "代码",
    "heading": "标题",
    "image": "图片",
    "byline": "作者/单位",
}

#: 精确字段类型 → 界面文案（详情页的精确字段卡用）
CODE_LABELS = {"clause": "条款号", "number": "编号", "date": "日期", "money": "金额"}


def status_label(status: str) -> str:
    """状态的中文名（未知状态原样返回，别吞掉信息）。"""
    return _STATUS_LABELS.get(status, status)


def status_badge(status: str, *, done: int = -1, total: int = -1) -> str:
    """状态徽标文案：处理中带进度（``处理中 3/12``）。"""
    if status == "indexing":
        if total >= 0 and done != total:
            return f"处理中 {done}/{total}"
        return "处理中"
    return status_label(status)


#: 状态 → Quasar 语义色（chip 的 color）
_STATUS_TONE = {
    "ready": "positive",
    "indexing": "warning",
    "pending": "grey-6",
    "failed": "negative",
    "superseded": "grey-5",
}


def status_chip(status: str, *, done: int = -1, total: int = -1) -> ui.chip:
    """状态胶囊（Quasar chip，outline 风格）：比裸文字更一眼分清。"""
    text = status_badge(status, done=done, total=total)
    return ui.chip(text, color=_STATUS_TONE.get(status, "grey-6")).props("dense outline").classes(
        "hp-status-chip"
    )


def parse_summary(row: object) -> str:
    """解析摘要：``22 页 · 20 页有文本 · 2 页待 OCR · 8,412 字``。

    "有文本的页"由 ``pages - len(ocr_pages)`` 算出（**不单独存**，少一个会漂移的列）；
    没有页信息的格式（txt/md）只显示字数。
    """
    parts: list[str] = []
    pages = getattr(row, "pages", None)
    ocr = getattr(row, "ocr_pages", ()) or ()
    chars = getattr(row, "chars", None)
    if pages:
        parts.append(f"{int(pages)} 页")
        parts.append(f"{max(0, int(pages) - len(ocr))} 页有文本")
        if ocr:
            parts.append(f"{len(ocr)} 页待 OCR")
    if chars:
        parts.append(f"{int(chars):,} 字")
    return " · ".join(parts)


def mini_row(item: knowledge_service.DocumentView, *, on_click: Any, show_reason: bool = False) -> None:
    """总览里的一行（最近导入 / 失败速览）：点一下进详情。"""
    with ui.row().classes("hp-row-item w-full items-center gap-2 hp-clickable") as row:
        ui.icon("description").classes("text-base hp-card-icon")
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(item.name).classes("hp-col-name")
            if show_reason and item.detail:
                ui.label(item.detail).classes("hp-field-help")
            elif parse_summary(item):
                ui.label(parse_summary(item)).classes("hp-field-help")
        with ui.element("div").classes("hp-col-status"):
            status_chip(item.status, done=item.vectors_done, total=item.vectors_total)
        ui.label(human_time(item.updated_at)).classes("hp-col-time")
    row.on("click", lambda _e: on_click())


def open_right_preview(doc_id: int) -> None:
    """右栏快速预览：不离开列表就能核对"导进来的到底是哪一份"。"""
    from app.ui.state.layout import layout

    def build() -> None:
        preview = knowledge_service.document_preview(doc_id)
        with ui.column().classes("w-full gap-2 hp-preview"):
            ui.label(str(preview["name"])).classes("hp-card-title")
            note = str(preview.get("note") or "")
            if note:
                ui.label(note).classes("hp-field-help")
            if preview["text"]:
                with ui.scroll_area().classes("w-full hp-preview-text"):
                    ui.label(str(preview["text"])).classes("hp-pre")
            chunks = knowledge_service.document_chunks(doc_id, page=1, size=20)
            ui.label(f"片段（{chunks['total']} 个，显示前 20）").classes("hp-field-help")
            for row in chunks["items"]:  # type: ignore[union-attr]
                where = f"第 {row['page']} 页 · " if row["page"] else ""
                section = f"{row['section']} · " if row["section"] else ""
                ui.label(f"#{row['seq']} {where}{section}{row['preview']}").classes("hp-field-help")

    layout().show_right(build, title="原件预览")
