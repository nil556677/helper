"""知识库 · 视图四：资料详情（点列表里的记录进入）。

一份资料的完整视图：**该份总览**（子块/父块/向量/版本/时间）· **分块明细**（分页 + 类型筛选）
· **精确字段**（编号/日期/金额/条款号）· **副本原文**（有界读）· **版本列表**（可回滚）。

这一页把"看细节"的卡片拆成小函数：每个函数只画一张卡（`_header` / `_overview` / `_chunks` …）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import run, ui

from app.services import knowledge as knowledge_service
from app.ui.components.dialog import confirm, notify
from app.ui.components.format import human_size, human_time
from app.ui.components.pager import PageState
from app.ui.screens.settings.knowledge.widgets import (
    CODE_LABELS,
    KIND_LABELS,
    card_head,
    parse_summary,
    stat,
    status_chip,
    status_label,
)


def render(container: ui.column, doc_id: int, *, back: Any) -> Callable[[], None]:
    """画详情页，返回它的 `refresh`。"""
    chunk_state = PageState(size=20)
    chunk_filter: dict[str, str] = {"kind": "", "heading": ""}
    #: 当前**展开着的片段行**（seq 集合）。详情页在自动刷新里会重建，重建时按它恢复——
    #: 否则展开一秒后就被刷没了（用户实测："打开一些就关闭了"）。
    opened: set[int] = set()
    #: 上次渲染的"内容签名"。签名没变就**不重建**：保住展开态、滚动位置，也省掉每秒重画的抖动。
    last: dict[str, Any] = {"sig": None}

    with container:
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.button("返回列表", icon="arrow_back", on_click=lambda: back()).props(
                "flat dense no-caps"
            ).mark("kb-back")
            ui.space()
        body = ui.column().classes("w-full gap-4")

    def _signature(detail: dict[str, Any] | None) -> tuple[Any, ...] | None:
        """决定"要不要重画"的指纹：资料状态/计数/时间 + 片段的分页与筛选。"""
        if detail is None:
            return None
        return (
            detail.get("status"),
            detail.get("version"),
            detail.get("vectors_done"),
            detail.get("vectors_total"),
            detail.get("detail"),
            detail.get("updated_at"),
            detail.get("children"),
            detail.get("parents"),
            len(detail.get("versions") or ()),
            int(chunk_state.page),
            int(chunk_state.size),
            str(chunk_filter.get("kind") or ""),
            str(chunk_filter.get("heading") or ""),
        )

    def refresh() -> None:
        detail = knowledge_service.document_detail(doc_id)
        sig = _signature(detail)
        if sig is not None and sig == last["sig"]:
            return  # 内容没变：不重建（展开态与滚动位置都保住）
        last["sig"] = sig
        body.clear()
        if detail is None:
            with body:
                ui.label("这份资料已经不在了（可能已被删除）。").classes("hp-hint")
            return
        with body:
            _header(detail, on_refresh=refresh, back=back)
            _overview(detail)
            _chunks(detail, chunk_state, chunk_filter, on_refresh=refresh, opened=opened)
            _codes(detail)
            _preview(doc_id)
            _versions(detail, on_refresh=refresh)

    refresh()
    return refresh


# ---------------------------------------------------------------------------
# 每张卡一个小函数
# ---------------------------------------------------------------------------


def _header(detail: dict[str, Any], *, on_refresh: Any, back: Any) -> None:
    """头部卡：名称 / 版本 / 状态 + 四个动作。"""
    with ui.card().classes("hp-card w-full gap-2"):
        with ui.row().classes("items-center gap-2 w-full no-wrap"):
            ui.icon("description").classes("text-base hp-card-icon")
            with ui.column().classes("gap-0 flex-1 min-w-0"):
                ui.label(str(detail["name"])).classes("hp-card-title")
                if detail.get("doc_title"):
                    ui.label(f"文档标题：{detail['doc_title']}").classes("hp-field-help")
                meta = f"第 {detail['version']} 版 · {human_size(int(detail['size']))}"
                if detail["detail"]:
                    meta += f" · {detail['detail']}"
                ui.label(meta).classes("hp-field-help")
            status_chip(
                str(detail["status"]),
                done=int(detail["vectors_done"]),
                total=int(detail["vectors_total"]),
            )
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button(
                "重建",
                icon="refresh",
                on_click=lambda: (
                    knowledge_service.enqueue_rebuild(int(detail["id"])),
                    notify("已加入重建队列（任务页可见进度）", kind="positive"),
                    on_refresh(),
                ),
            ).props("flat dense no-caps").mark("kb-detail-rebuild")
            ui.button(
                "重新向量化",
                icon="memory",
                on_click=lambda: (
                    knowledge_service.enqueue_rebuild(int(detail["id"])),
                    notify("已加入重建队列（任务页可见进度）", kind="positive"),
                    on_refresh(),
                ),
            ).props("flat dense no-caps").mark("kb-detail-embed")
            ui.button(
                "导出",
                icon="ios_share",
                on_click=lambda: _export_one(int(detail["id"]), str(detail["name"])),
            ).props("flat dense no-caps").mark("kb-detail-export")
            ui.button(
                "删除",
                icon="delete_outline",
                on_click=lambda: _ask_delete(detail, back=back),
            ).props("flat dense no-caps").classes("hp-danger").mark("kb-detail-delete")


def _ask_delete(detail: dict[str, Any], *, back: Any) -> None:
    """详情页删除：确认后丢线程池删，删完回列表。"""

    async def do() -> None:
        try:
            removed = await run.io_bound(knowledge_service.delete, [int(detail["id"])])
            notify(f"已删除「{detail['name']}」" if removed else "这份已经不在了", kind="positive")
        except Exception as exc:
            notify(f"删除失败：{exc}", kind="negative")
        back()

    confirm(
        f"删掉「{detail['name']}」？",
        "它的片段与索引会一起删掉，副本文件也会清掉。",
        do,
        confirm_label="删掉",
        danger=True,
        confirm_mark="kb-detail-delete-confirm",
    )


def _overview(detail: dict[str, Any]) -> None:
    """这份资料的总览卡。"""
    with ui.card().classes("hp-card w-full gap-3"):
        card_head("insights", "这份资料")
        children = int(detail["children"])
        parents = int(detail["parents"])
        with ui.element("div").classes("hp-stats w-full"):
            stat(str(children), "个可检索片段")
            stat(str(parents), "个上下文父块")
            stat(str(children + parents), "分块总数")
            stat(
                f"{int(detail['vectors_done'])}/{int(detail['vectors_total'])}",
                "向量已建",
            )
            stat(f"第 {int(detail['version'])} 版", "版本")
            stat(str(len(detail["versions"])), "历史版本")
        summary = parse_summary(_DetailRow(detail))
        if summary:
            ui.label(summary).classes("hp-field-help")
        ui.label(
            f"导入 {human_time(float(detail['created_at']))} · "
            f"更新 {human_time(float(detail['updated_at']))}"
        ).classes("hp-field-help")


class _DetailRow:
    """把详情 dict 包装成"像一行记录"（复用 `parse_summary` 的字段访问）。"""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.pages = detail.get("pages")
        self.ocr_pages = detail.get("ocr_pages") or ()
        self.chars = detail.get("chars")


def _chunks(
    detail: dict[str, Any],
    state: PageState,
    chunk_filter: dict[str, str],
    *,
    on_refresh: Any,
    opened: set[int] | None = None,
) -> None:
    """分块明细卡：类型筛选 + 分页 + **行内展开**（展开状态跨刷新保留）。"""
    opened = opened if opened is not None else set()
    doc_id = int(detail["id"])
    kind = chunk_filter.get("kind") or None
    heading = chunk_filter.get("heading") or None
    page = knowledge_service.document_chunks(
        doc_id, page=state.page, size=state.size, kind=kind, heading=heading
    )
    state.total = int(page["total"])
    state.page = int(page["page"])
    kinds: list[str] = list(page.get("kinds") or [])
    with ui.card().classes("hp-card w-full gap-2"):
        card_head("segment", "分块明细", f"{page['total']} 个子块")
        with ui.row().classes("items-center gap-2 w-full"):
            ui.input(
                placeholder="按标题筛选（如 相关工作 / 2.1）",
                value=chunk_filter.get("heading", ""),
                on_change=lambda event: _heading_changed(
                    str(event.value or ""), chunk_filter, state, on_refresh
                ),
            ).props("outline dense clearable").classes("hp-filter").mark("kb-chunk-heading")
            options = {"": "全部类型"}
            options.update({item: KIND_LABELS.get(item, item) for item in kinds})
            ui.select(
                options,
                value=chunk_filter.get("kind", ""),
                on_change=lambda event: _kind_changed(
                    str(event.value or ""), chunk_filter, state, on_refresh
                ),
            ).props("outline dense options-dense").classes("hp-filter").mark("kb-chunk-kind")
            ui.space()
            if int(page["total"]) > state.size:
                ui.button(
                    "上一页",
                    icon="chevron_left",
                    on_click=lambda: _page(state, -1, on_refresh),
                ).props("flat dense no-caps").mark("kb-chunk-prev")
                ui.label(
                    f"{page['page']}/{max(1, -(-int(page['total']) // state.size))}"
                ).classes("hp-field-help")
                ui.button(
                    "下一页",
                    icon="chevron_right",
                    on_click=lambda: _page(state, 1, on_refresh),
                ).props("flat dense no-caps").mark("kb-chunk-next")
        if not page["items"]:
            ui.label("这一版还没有片段（可能正在解析，或已被清空）。").classes("hp-field-help")
            return
        ui.label(
            "点任意一行展开：key / 标题 / 定位 / 父块（含父块正文）/ 正文 / 送进向量的文本。"
        ).classes("hp-field-help")
        for row in page["items"]:  # type: ignore[union-attr]
            # **行内展开**（替代原来的 ⓘ 图标 + 弹窗）：详情页直接看得到"向量库里存了什么"。
            # 展开状态记进 `opened`：页面重建（自动刷新/改筛选）后按它恢复，不会"开一秒就关"。
            seq = int(row["seq"])
            section_text = " › ".join(str(p) for p in (row.get("section_path") or []))
            if not section_text:
                section_text = {
                    "front_matter": "（文档前置）",
                    "back_matter": "（文档后置）",
                }.get(str(row.get("section_kind") or ""), "—")
            header = "　".join(
                part
                for part in (
                    f"#{row['seq']}",
                    KIND_LABELS.get(str(row["kind"]), str(row["kind"])),
                    "—" if row["page"] is None else f"第 {row['page']} 页",
                    section_text,
                    str(row["preview"]),
                )
                if part
            )

            def _remember(event: Any, seq: int = seq) -> None:
                if bool(getattr(event, "value", False)):
                    opened.add(seq)
                else:
                    opened.discard(seq)

            with (
                ui.expansion(header, value=seq in opened, on_value_change=_remember)
                .classes("w-full hp-chunk-expansion")
                # `group` = 手风琴：**同时只开一个**（点另一行会关掉上一行），页面不会被撑长。
                .props("dense group=hp-chunk-detail")
            ):
                _chunk_detail(row)


def _format_loc(loc: object) -> str:
    """把 payload.loc 拼成一句人话（页 / 段 / 行 / 表行）。"""
    if not isinstance(loc, dict) or not loc:
        return "—"
    parts: list[str] = []
    if loc.get("page") is not None:
        parts.append(f"第 {loc['page']} 页")
    if loc.get("para") is not None:
        parts.append(f"第 {int(loc['para']) + 1} 段")
    if loc.get("line_from") is not None:
        parts.append(f"第 {loc['line_from']}–{loc.get('line_to', loc['line_from'])} 行")
    if loc.get("table"):
        ref = str(loc["table"])
        if loc.get("row_from") is not None:
            rows = f"第 {loc['row_from']} 行"
            if loc.get("row_to") and loc["row_to"] != loc["row_from"]:
                rows = f"第 {loc['row_from']}–{loc['row_to']} 行"
            ref += f" {rows}"
        if loc.get("col_from") is not None:
            cols = f"列 {loc['col_from']}"
            if loc.get("col_to") and loc["col_to"] != loc["col_from"]:
                cols = f"列 {loc['col_from']}–{loc['col_to']}"
            ref += f" · {cols}"
        parts.append(ref)
    return " · ".join(parts) or "—"


def _chunk_detail(item: dict[str, Any]) -> None:
    """一条片段的**全部存储信息**（行内展开的内容）：payload 字段 + 正文 + 送进向量的文本。"""
    fields = [
        ("key", item.get("key") or "—"),
        ("类型", KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))),
        (
            "位置",
            {
                "section": "章节内",
                "front_matter": "文档前置",
                "back_matter": "文档后置",
            }.get(str(item.get("section_kind") or "section"), "章节内"),
        ),
        ("标题（payload.heading）", item.get("heading") or "—"),
        ("层级", "—" if item.get("level") is None else str(item.get("level"))),
        ("页码", "—" if item.get("page") is None else str(item.get("page"))),
        ("定位（loc）", _format_loc(item.get("loc"))),
        ("父块 key", item.get("parent_key") or "—"),
        ("父块标题", item.get("parent_heading") or "—"),
        ("父块字数", str(len(str(item.get("parent_text") or "")))),
        ("正文字数", str(item.get("chars") or 0)),
        ("content_hash", item.get("content_hash") or "—"),
        ("policy_version", str(item.get("policy_version") or "—")),
        ("embedding_version", str(item.get("embedding_version") or "—")),
    ]
    with ui.column().classes("w-full gap-1"):
        for label, value in fields:
            with ui.row().classes("w-full gap-2 no-wrap items-start"):
                ui.label(label).classes("hp-field-name").style("min-width: 132px;")
                ui.label(str(value)).classes("hp-field-help").style("word-break: break-all;")
        ui.separator()
        ui.label("正文（payload.text，引用用）").classes("hp-field-name")
        ui.label(str(item.get("text") or "")).classes("hp-detail-mono").style(
            "max-height: 180px; overflow: auto;"
        )
        ui.label("送进向量（标题 + 换行 + 正文）").classes("hp-field-name")
        ui.label(str(item.get("index_text") or "")).classes("hp-detail-mono").style(
            "max-height: 180px; overflow: auto;"
        )
        parent_text = str(item.get("parent_text") or "")
        ui.label("父块正文（送给模型的证据：标题 + 该节正文）").classes("hp-field-name")
        if parent_text:
            ui.label(parent_text).classes("hp-detail-mono").style(
                "max-height: 220px; overflow: auto;"
            )
        else:
            ui.label("—（父块没取到，可能还在写入或已被清空）").classes("hp-field-help")


def _kind_changed(value: str, chunk_filter: dict[str, str], state: PageState, on_refresh: Any) -> None:
    chunk_filter["kind"] = value
    state.page = 1
    on_refresh()


def _heading_changed(
    value: str, chunk_filter: dict[str, str], state: PageState, on_refresh: Any
) -> None:
    """按标题筛选：匹配整条 section_path（"相关工作"/"2.1" 都能筛到）。"""
    chunk_filter["heading"] = value.strip()
    state.page = 1
    on_refresh()


def _page(state: PageState, delta: int, on_refresh: Any) -> None:
    state.page = max(1, state.page + delta)
    on_refresh()


def _codes(detail: dict[str, Any]) -> None:
    """精确字段卡（P1.10 抽出来的编号/日期/金额/条款号）。"""
    kinds: dict[str, int] = dict(detail.get("code_kinds") or {})
    if not kinds:
        return
    rows = knowledge_service.document_codes(int(detail["id"]), limit=100)
    with ui.card().classes("hp-card w-full gap-2"):
        card_head(
            "tag",
            "精确字段",
            " · ".join(f"{CODE_LABELS.get(key, key)} {value}" for key, value in sorted(kinds.items())),
        )
        ui.label("这些值可以直接精确查找（不必靠相似度）。").classes("hp-field-help")
        with ui.column().classes("w-full gap-1"):
            for row in rows:
                where = f"第 {row['page']} 页 · " if row["page"] else ""
                ui.label(
                    f"{CODE_LABELS.get(str(row['kind']), str(row['kind']))}："
                    f"{row['value']}　{where}{row['raw']}"
                ).classes("hp-field-help")


def _preview(doc_id: int) -> None:
    """副本原文卡（有界读，只显示开头）。"""
    preview = knowledge_service.document_preview(doc_id)
    with ui.card().classes("hp-card w-full gap-2"):
        card_head("article", "副本原文", "只读")
        note = str(preview.get("note") or "")
        if note:
            ui.label(note).classes("hp-field-help")
        if preview["text"]:
            with ui.scroll_area().classes("w-full hp-preview-text"):
                ui.label(str(preview["text"])).classes("hp-pre")
        else:
            ui.label("没有可预览的原文。").classes("hp-field-help")


def _versions(detail: dict[str, Any], *, on_refresh: Any) -> None:
    """版本列表卡（P1.7 的版本链）：可回滚到历史版本。"""
    versions: list[dict[str, Any]] = list(detail.get("versions") or [])
    if len(versions) <= 1:
        return
    with ui.card().classes("hp-card w-full gap-2"):
        card_head("history", "版本", f"{len(versions)} 个")
        for item in versions:
            current = int(item["id"]) == int(detail["id"])
            with ui.row().classes("hp-row-item w-full items-center gap-3"):
                ui.label(f"第 {item['version']} 版").classes("hp-col")
                ui.label(status_label(str(item["status"]))).classes("hp-col hp-status")
                ui.label(human_time(float(item["updated_at"]))).classes("hp-field-help")
                ui.space()
                if current:
                    ui.label("当前查看").classes("hp-field-help")
                elif str(item["status"]) == "superseded":
                    ui.button(
                        "回滚到这一版",
                        icon="restore",
                        on_click=lambda version_id=int(item["id"]): _rollback(
                            version_id, on_refresh
                        ),
                    ).props("flat dense no-caps").mark(f"kb-rollback-{item['version']}")


def _rollback(version_doc_id: int, on_refresh: Any) -> None:
    """回滚到历史版本（确认后执行；切换是原子的，见 P1.7）。**走服务层**，不直接碰 core。"""

    def do() -> None:
        try:
            notify(knowledge_service.rollback_to_version(version_doc_id), kind="positive")
        except Exception as exc:
            notify(f"回滚失败：{exc}", kind="negative")
        on_refresh()

    confirm("回滚到这个版本？", "当前版本会退场（保留可再切回），检索立刻只看回滚后的版本。", do)


def _export_one(doc_id: int, name: str) -> None:
    """导出单份（复用批量导出的同一实现）。"""
    safe = name.rsplit(".", 1)[0]
    target = Path.home() / "Downloads" / f"{safe}.zip"
    report = knowledge_service.export([doc_id], target)
    notify(f"已导出 → {report['path']}", kind="positive")
