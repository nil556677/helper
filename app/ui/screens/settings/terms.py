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
from app.ui.components.ime import IME_ENTER_GUARD  # 输入法组词保护
from app.ui.components.pager import PageState, render_pager  # 列表分页


def render() -> None:
    """画「领域词」分区：**列表** 与 **添加/批量** 分成两个页签（互不挤在一起）。"""
    list_box: ui.column | None = None  # 列表容器（添加成功后只刷新它）

    def refresh_list() -> None:
        """只重画列表页（不动添加/批量页签里正在填的内容）。"""
        if list_box is None or list_box.is_deleted:
            return
        list_box.clear()
        with list_box:
            _render_list(on_done=refresh_list)

    with ui.column().classes("w-full gap-3"):
        ui.label(
            "分词时保持整词的专有名词（公司名、项目名、领域词）。"
            "增删改只需重算词法（秒级），不会重嵌向量。"
        ).classes("hp-field-help")  # 说明（标题在左栏，不再重复）

        with ui.row().classes("w-full items-center gap-2 no-wrap hp-toolbar"):
            with ui.tabs().classes("hp-tabs").props("no-caps dense align=left") as tabs:
                ui.tab("list", "词条列表", icon="list_alt").mark("term-tab-list")  # 列表页签
                ui.tab("add", "添加 / 批量", icon="add_circle_outline").mark("term-tab-add")  # 录入页签
            ui.space()  # 撑开

        with ui.tab_panels(tabs, value="list").classes("w-full"):
            with ui.tab_panel("list"), ui.column().classes("w-full gap-3"):
                list_box = ui.column().classes("w-full gap-3")  # 列表容器
            with ui.tab_panel("add"), ui.column().classes("w-full gap-3"):
                _render_add(on_done=refresh_list)  # 添加卡（加完留在本页，不跳走）
                _render_bulk(on_done=refresh_list)  # 批量导入/导出卡

    def _tab_switch(event: Any) -> None:
        """切到「词条列表」页签时重画一次，保证看到最新数据（添加/导入都在后台刷新过）。"""
        try:
            if str(getattr(event, "value", "") or "") == "list":
                refresh_list()
        except Exception:  # 客户端已删等：这一帧跳过即可
            pass

    tabs.on("update:model-value", _tab_switch)  # 页签切换
    refresh_list()  # 首次渲染列表


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
                """添加一个词条：重复/非法给明确提示，**并清空输入**（别让用户反复点出同一条报错）。"""
                text = str(term_input.value or "").strip()  # 词条
                try:
                    terms_service.add_term(text, freq=_freq_from(freq_input.value))
                except terms_service.TermError as exc:
                    message = str(exc)  # 业务校验文案
                    term_input.value = ""  # 不管哪种失败都清空，避免重复提交同一句
                    freq_input.value = None
                    if message.startswith("词条已存在"):  # 重复：当成"已经有了"，不算错误
                        notify(f"「{text}」已经在词条列表里了，不用重复添加", kind="info")
                        on_done()  # 刷新列表（方便用户去核对）
                    else:
                        notify(message, kind="warning")
                    return
                except Exception as exc:  # 库不可用等
                    notify(f"添加失败：{exc}", kind="negative")
                    return
                term_input.value = ""  # 清空词条
                freq_input.value = None  # 清空词频
                notify(f"已添加「{text}」；重算词法后生效", kind="positive")
                on_done()  # 刷新列表 + 切到列表页

            ui.button("添加", icon="add", on_click=submit).props("unelevated dense no-caps").classes(
                "hp-send w-auto"
            ).mark("term-add")


def _render_list(*, on_done: Callable[[], None]) -> None:
    """卡 2：词条列表（筛选 + 分页；每行可改 / 启停 / 删）。"""
    items = terms_service.list_terms()
    state = PageState(size=50)  # 分页状态（默认每页 50，多数用户一页看全）
    visible: dict[str, str] = {"kind": ""}  # 启用状态筛选："" 全部 / on 启用 / off 停用
    # `draw` 在函数末尾才定义，但控件（select/input）**初始化时会先触发一次 on_change**。
    # 直接引用 `draw` 会 NameError；用可变容器持有，构建期没装好就先跳过。
    holder: dict[str, Any] = {"draw": None}
    with ui.column().classes("hp-card w-full gap-3"):
        with ui.row().classes("hp-card-head w-full no-wrap"):  # 卡片头（左标题 + 右页大小）
            ui.icon("list_alt").classes("text-base hp-card-icon")
            ui.label("全部词条").classes("hp-card-title")
            count_label = ui.label(f"共 {len(items)} 条").classes("hp-field-help")
            ui.space()
            ui.select(
                {20: "每页 20", 50: "每页 50", 100: "每页 100", 0: "全部"},
                value=50,
                on_change=lambda event: _size_changed(event, state, holder),
            ).props("outline dense options-dense").classes("hp-page-size").mark("term-page-size")
        if not items:
            ui.label("还没有词条。分词把某个专有名词切碎时，把它加进来即可整词命中。").classes(
                "hp-field-help"
            )
            return

        with ui.row().classes("hp-toolbar w-full items-center gap-2"):
            search = ui.input(placeholder="筛选词条…").props(
                "outline dense clearable debounce=300"  # 防抖：别每敲一个字就重画
            ).classes("hp-input").mark("term-search")
            ui.select(
                {"": "全部", "on": "启用", "off": "停用"},
                value="",
                on_change=lambda event: _kind_changed(str(event.value or ""), visible, holder),
            ).props("outline dense options-dense").classes("hp-filter").mark("term-kind")
        rows = ui.column().classes("w-full gap-1")
        pager_box = ui.column().classes("w-full gap-0")

        def draw() -> None:
            """按筛选条件重画当前页 + 分页控件。"""
            rows.clear()
            keyword = str(search.value or "").strip()  # 关键词
            kind = visible["kind"]  # 启停筛选
            shown = [  # 过滤后的全部词条
                item
                for item in items
                if (not keyword or keyword in str(item["term"]))
                and (not kind or (bool(item["enabled"]) == (kind == "on")))
            ]
            count_label.text = f"共 {len(shown)} 条"  # 总数（筛选后）
            count_label.update()
            state.total = len(shown)  # 分页总数
            state.clamp()  # 夹住页码
            start = (state.page - 1) * state.size  # 起点
            page_items = shown[start : start + state.size]  # 本页
            with rows:
                if not shown:
                    ui.label("没有匹配的词条").classes("hp-field-help")
                for item in page_items:
                    _row(item, on_done=on_done)
            pager_box.clear()
            with pager_box:
                if len(shown) > state.size:  # 只有超过一页才画分页
                    render_pager(state, on_change=lambda *_: holder["draw"] and holder["draw"](), noun="条")

        holder["draw"] = draw  # 把 draw 装进 holder（控件初始化触发 on_change 时先跳过）
        search.on("update:model-value", lambda _event: holder["draw"] and holder["draw"]())
        draw()


def _size_changed(event: Any, state: PageState, holder: dict[str, Any]) -> None:
    """每页条数（0 = 全部）：改完回第一页重画。"""
    draw = holder.get("draw")  # 构建期可能还没装好（select 初始化先触发一次）
    if draw is None:
        return
    try:
        size = int(getattr(event, "value", 0) or 0)  # 选择值
    except (TypeError, ValueError):
        size = 50  # 兜底
    state.size = size if size > 0 else max(1, state.total)  # 0 = 全部（用当前总数）
    state.page = 1  # 回第一页
    draw()  # 重画


def _kind_changed(value: str, visible: dict[str, str], holder: dict[str, Any]) -> None:
    """启停筛选变化：记下并重画。"""
    draw = holder.get("draw")  # 构建期可能还没装好
    if draw is None:
        return
    visible["kind"] = value  # 记录
    draw()  # 重画


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
        # 动作**常显**（管理列表：删除/修改要一眼能找到，不藏在 hover 里）
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

    def _body(close: Any) -> None:
        """正文（画在卡片里）：词条 + 词频 + 保存。"""
        name_input = ui.input(label="词条", value=term).props("outlined dense").classes(
            "hp-input"
        ).mark("term-edit-name")  # 词条输入
        freq_input = ui.number(
            label="词频（可空）", value=current["freq"], min=0, precision=0
        ).props("outlined dense").classes("hp-input").mark("term-edit-freq")  # 词频输入

        def save() -> None:
            """保存：改名词条、改词频（空 = 默认）。"""
            new_term = str(name_input.value or "").strip()  # 新词
            freq = _freq_from(freq_input.value)  # 新词频
            try:
                if new_term != term:
                    terms_service.rename_term(term, new_term)  # 改名
                if freq != current["freq"]:
                    terms_service.set_freq(new_term or term, freq)  # 改词频
            except terms_service.TermError as exc:
                notify(str(exc), kind="warning")  # 业务校验
                return
            except Exception as exc:
                notify(f"保存失败：{exc}", kind="negative")  # 库异常
                return
            close()  # 关弹窗
            notify("已保存；重算词法后生效", kind="positive")  # 提示
            on_done()  # 刷新列表

        # 回车保存（输入法组词中不触发）
        for element in (name_input, freq_input):
            element.on("keydown.enter", lambda _e=None: save(), js_handler=IME_ENTER_GUARD)

        with ui.row().classes("hp-actions w-full justify-end"):
            ui.button("保存", on_click=save).props("unelevated dense no-caps").classes(
                "hp-send w-auto"
            ).mark("term-edit-save")  # 保存按钮

    dialog, _close = custom(title=f"修改「{term}」", content=_body)  # 统一壳（正文在卡片里）
    dialog.open()  # 打开


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
