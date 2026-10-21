"""设置 · 领域词：全局自定义关键词（存 ``token_dict`` 表）。

**为什么单独一页**：领域词回答的是"分词别把专有名词切碎"，与知识库导入/检索参数不是
一件事；混进「通用」会让人找不到。词条改动只影响**词法**（重算词法即可，秒级），
不动向量。

版式：一张卡一件事——添加 / 列表 / 批量导入导出。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from app.services import terms as terms_service
from app.ui.components.card import card_head
from app.ui.components.dialog import confirm, custom, notify


def render() -> None:
    """画「领域词」分区（添加卡常驻；列表与批量卡可整体刷新）。"""
    with ui.column().classes("w-full gap-4"):
        with ui.column().classes("hp-page-head gap-1"):
            ui.label("领域词").classes("hp-page-title")
            ui.label(
                "分词时保持整词的专有名词（公司名、项目名、领域词）。"
                "增删改只需重算词法（秒级），不会重嵌向量。"
            ).classes("hp-field-help")

        body = ui.column().classes("w-full gap-4")

        def refresh() -> None:
            body.clear()
            with body:
                _render_list(on_done=refresh)
                _render_bulk(on_done=refresh)

        _render_add(on_done=refresh)
        refresh()


def _freq_from(value: object) -> int | None:
    """输入框值 → 词频；空值 = 默认词频。"""
    if value is None or value == "":
        return None
    return int(value)  # type: ignore[arg-type]


def _render_add(*, on_done: Callable[[], None]) -> None:
    """卡 1：添加一个词条。"""
    with ui.column().classes("hp-card w-full gap-3"):
        card_head("add_circle_outline", "添加词条")
        with ui.row().classes("items-end gap-3 flex-wrap"):
            term_input = ui.input(label="词条").props("outlined dense").classes("hp-input").mark(
                "term-new"
            )
            freq_input = ui.number(
                label="词频（可空）", value=None, min=0, precision=0
            ).props("outlined dense").classes("hp-input").mark("term-new-freq")

            def submit() -> None:
                try:
                    terms_service.add_term(
                        str(term_input.value or "").strip(),
                        freq=_freq_from(freq_input.value),
                    )
                except terms_service.TermError as exc:
                    notify(str(exc), kind="warning")
                    return
                except Exception as exc:  # 库不可用等
                    notify(f"添加失败：{exc}", kind="negative")
                    return
                term_input.value = ""
                freq_input.value = None
                notify("已添加；重算词法后生效", kind="positive")
                on_done()

            ui.button("添加", icon="add", on_click=submit).props("unelevated dense no-caps").classes(
                "hp-send w-auto"
            ).mark("term-add")


def _render_list(*, on_done: Callable[[], None]) -> None:
    """卡 2：词条列表（带本地筛选；每行可改 / 启停 / 删）。"""
    items = terms_service.list_terms()
    with ui.column().classes("hp-card w-full gap-3"):
        card_head("list_alt", "词条列表", f"共 {len(items)} 条")
        if not items:
            ui.label("还没有词条。分词把某个专有名词切碎时，把它加进来即可整词命中。").classes(
                "hp-field-help"
            )
            return

        search = ui.input(placeholder="筛选词条…").props("outlined dense clearable").classes(
            "hp-input"
        ).mark("term-search")
        rows = ui.column().classes("w-full gap-1")

        def draw() -> None:
            rows.clear()
            keyword = str(search.value or "").strip()
            shown = [item for item in items if not keyword or keyword in str(item["term"])]
            with rows:
                if not shown:
                    ui.label("没有匹配的词条").classes("hp-field-help")
                    return
                for item in shown:
                    _row(item, on_done=on_done)

        search.on("update:model-value", lambda _event: draw())
        draw()


def _row(item: dict[str, Any], *, on_done: Callable[[], None]) -> None:
    """一行词条：词条 + 状态/词频 + 改 / 启停 / 删。"""
    term = str(item["term"])
    enabled = bool(item["enabled"])
    freq = item["freq"]
    with ui.row().classes("hp-row-item w-full items-center gap-3 no-wrap"):
        ui.icon("sell").classes("text-base hp-card-icon")
        with ui.column().classes("hp-col-name gap-0"):
            ui.label(term).classes("hp-col-name")
            meta = "启用" if enabled else "已停用"
            if freq is not None:
                meta += f" · 词频 {freq}"
            ui.label(meta).classes("hp-field-help")
        ui.space()
        ui.button(
            icon="edit", on_click=lambda t=term: _edit(t, on_done=on_done)
        ).props("flat dense round size=sm").classes("hp-btn-icon").mark(
            f"term-edit-{term}"
        ).tooltip("修改")
        ui.button(
            icon="pause_circle_outline" if enabled else "play_circle_outline",
            on_click=lambda t=term, e=enabled: _toggle(t, not e, on_done=on_done),
        ).props("flat dense round size=sm").classes("hp-btn-icon").mark(
            f"term-toggle-{term}"
        ).tooltip("停用" if enabled else "启用")
        ui.button(
            icon="delete_outline", on_click=lambda t=term: _delete(t, on_done=on_done)
        ).props("flat dense round size=sm").classes("hp-btn-icon hp-icon-warn").mark(
            f"term-delete-{term}"
        ).tooltip("删除")


def _toggle(term: str, enabled: bool, *, on_done: Callable[[], None]) -> None:
    try:
        terms_service.set_enabled(term, enabled)
    except Exception as exc:
        notify(f"操作失败：{exc}", kind="negative")
        return
    notify("已启用" if enabled else "已停用", kind="positive")
    on_done()


def _delete(term: str, *, on_done: Callable[[], None]) -> None:
    def do() -> None:
        try:
            terms_service.delete_term(term)
        except Exception as exc:
            notify(f"删除失败：{exc}", kind="negative")
            return
        notify(f"已删除「{term}」", kind="positive")
        on_done()

    confirm(
        "删除词条",
        f"确定删除「{term}」？删除后需重算词法才生效。",
        do,
        confirm_label="删掉",
        danger=True,
        confirm_mark=f"term-delete-confirm-{term}",
    )


def _edit(term: str, *, on_done: Callable[[], None]) -> None:
    """修改弹窗：改名 + 词频（空 = 默认）。"""
    current = next((it for it in terms_service.list_terms() if it["term"] == term), None)
    if current is None:
        notify("这个词条已经不在了", kind="warning")
        on_done()
        return

    dialog, close = custom(title=f"修改「{term}」")
    with dialog:
        name_input = ui.input(label="词条", value=term).props("outlined dense").classes(
            "hp-input"
        ).mark("term-edit-name")
        freq_input = ui.number(
            label="词频（可空）", value=current["freq"], min=0, precision=0
        ).props("outlined dense").classes("hp-input").mark("term-edit-freq")

        def save() -> None:
            new_term = str(name_input.value or "").strip()
            freq = _freq_from(freq_input.value)
            try:
                if new_term != term:
                    terms_service.rename_term(term, new_term)
                if freq != current["freq"]:
                    terms_service.set_freq(new_term or term, freq)
            except terms_service.TermError as exc:
                notify(str(exc), kind="warning")
                return
            except Exception as exc:
                notify(f"保存失败：{exc}", kind="negative")
                return
            close()
            notify("已保存；重算词法后生效", kind="positive")
            on_done()

        with ui.row().classes("hp-actions w-full justify-end"):
            ui.button("保存", on_click=save).props("unelevated dense no-caps").classes(
                "hp-send w-auto"
            ).mark("term-edit-save")
    dialog.open()


def _render_bulk(*, on_done: Callable[[], None]) -> None:
    """卡 3：批量导入 / 导出（同一块文本框，导入后清空、导出后填入）。"""
    with ui.column().classes("hp-card w-full gap-3"):
        card_head("upload_file", "批量导入 / 导出")
        ui.label(
            "一行一词；`词 词频` 可带词频；`#` 开头是注释。导入是 upsert："
            "已有词条会被启用，只有该行带词频时才改词频。"
        ).classes("hp-field-help")
        area = ui.textarea(placeholder="履约保证金\n供应商 100000\n# 注释行会被忽略").props(
            "outlined dense rows=6"
        ).classes("w-full hp-resize-textarea").mark("term-import-text")

        with ui.row().classes("hp-actions"):
            def do_import() -> None:
                try:
                    result = terms_service.import_term_text(str(area.value or ""))
                except Exception as exc:
                    notify(f"导入失败：{exc}", kind="negative")
                    return
                changed = int(result["added"]) + int(result["updated"])
                notify(
                    f"新增 {result['added']} / 更新 {result['updated']} / 跳过 {result['skipped']}"
                    + ("；重算词法后生效" if changed else ""),
                    kind="positive" if changed else "warning",
                )
                errors = list(result["errors"])
                if errors:
                    tail = "…" if len(errors) > 3 else ""
                    notify(f"有 {len(errors)} 行跳过：{'；'.join(errors[:3])}{tail}", kind="warning")
                if changed:
                    area.value = ""
                    on_done()

            ui.button("导入", icon="upload", on_click=do_import).props(
                "unelevated dense no-caps"
            ).classes("hp-send w-auto").mark("term-import")

            def do_export() -> None:
                try:
                    text = terms_service.export_term_text()
                except Exception as exc:
                    notify(f"导出失败：{exc}", kind="negative")
                    return
                area.value = text
                notify("已导出到上方文本框，可全选复制", kind="info")

            ui.button("导出启用的词条", icon="download", on_click=do_export).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("term-export")
