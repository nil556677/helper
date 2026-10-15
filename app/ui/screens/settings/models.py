"""设置 · 模型配置：每个槽位**各自保存、各自「测试连接」**。

三条设计事实（都是用户拍板的）：

- **字段全部手填**：服务商 / 模型名 / 接口地址都是输入框，不做"选服务商自动带出"；
- **高级参数不上界面**：温度 / 超时 / 最大输出用 `llm_slots` 表里的默认值；
- **api key 只显示尾号**：明文只在写入那一刻存在，库里只有 DPAPI 密文。

**为什么每个槽位都要能单独测**：模型配置最常见的毛病是"填了但通不了"，而两个模型互不影响——
一个通了不代表另一个也通。测试是**真发一次极小请求**：推理模型发一条对话、OCR 模型发一张
1×1 的图；失败会分类说明原因（key 错 / 地址错 / 模型名错 / 限流 / 超时 / 连不上）。

**版式**：一张卡一个槽位；卡内"左名称右控件"，底部一行放动作与测试结果。
**没配好时「测试连接」是灰的**——点了只会得到一句"先填好…"，不如直接不让点。
"""

from __future__ import annotations

from typing import Any

from nicegui import run, ui

from app.services import models as models_service

#: 每个槽位的图标（卡片头用）
_SLOT_ICONS = {"reasoning": "smart_toy", "ocr": "image"}


def render() -> None:
    """画「模型配置」分区的内容（每个槽位一张卡）。"""
    with ui.column().classes("w-full gap-4"):
        ui.label("模型配置").classes("hp-page-title")
        ui.label("两个槽位，各自保存、各自测试连接；api key 加密后只存密文，界面只显示尾号。").classes(
            "hp-hint"
        )
        for item in models_service.load_all():
            _render_slot(item)

        with (
            ui.expansion("说明：这些配置存在哪", icon="help_outline").classes(
                "w-full text-sm hp-sources"
            ),
            ui.column().classes("w-full gap-1 p-2"),
        ):
            ui.label(
                "配置写进**数据库**的 llm_slots 表（每个槽位一行）；api key 只存 DPAPI 密文，明文不落盘。"
            ).classes("hp-field-help")
            ui.label(
                "config.json 只留「打开数据库之前就必须知道」的两项：日志级别与数据目录。"
            ).classes("hp-field-help")
            ui.label(
                "采样参数、超时、输出上限这些高级项不在界面上暴露，统一用数据库里的默认值。"
            ).classes("hp-field-help")


def _mask(tail: str) -> str:
    """密钥的展示形式：掩码 + 尾号（只够辨认"是不是我那一把"）。"""
    return ("•" * 8 + tail) if tail else ""


def _slot_item(slot_key: str) -> dict[str, Any]:
    """取某个槽位的最新状态（保存后要立刻反映到界面上）。"""
    return next(item for item in models_service.load_all() if item["key"] == slot_key)


def _field(name: str, help_text: str, value: str, marker: str) -> Any:
    """一行表单：左边名称与说明，右边输入框。返回输入框。"""
    with ui.row().classes("hp-field-row"):
        with ui.column().classes("hp-field-main gap-0"):
            ui.label(name).classes("hp-field-name")
            ui.label(help_text).classes("hp-field-help")
        return ui.input(value=value).props("outlined dense").classes("hp-input").mark(marker)


def _render_slot(item: dict[str, Any]) -> None:
    """一个模型槽位的卡片：手填三项 + api key（只显示尾号）+ 自己的「测试连接」与「保存」。"""
    slot_key = str(item["key"])
    state = {"tail": str(item["key_tail"]), "editing": False}

    with ui.column().classes("hp-card hp-slot w-full gap-3"):
        with ui.row().classes("hp-card-head w-full"):
            ui.icon(_SLOT_ICONS.get(slot_key, "smart_toy")).classes("text-base hp-card-icon")
            ui.label(str(item["title"])).classes("hp-card-title")
            ui.icon("info").classes("text-xs hp-card-icon").tooltip(f"配置项名称：{slot_key}")
            ui.space()
            status = ui.label("").classes("hp-status")
        ui.label(str(item["purpose"])).classes("hp-field-help")

        with ui.column().classes("w-full gap-2"):
            provider = _field(
                "服务商", "手填，例如 火山方舟 / DeepSeek / OpenAI", str(item["provider"]),
                f"slot-{slot_key}-provider",
            )
            model = _field(
                "模型名", f"手填，例如 {str(item['example']).split(' / ')[-1]}", str(item["model"]),
                f"slot-{slot_key}-model",
            )
            base_url = _field(
                "接口地址", "手填，例如 https://ark.cn-beijing.volces.com/api/v3",
                str(item["base_url"]), f"slot-{slot_key}-base_url",
            )
            with ui.row().classes("hp-field-row"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("API Key").classes("hp-field-name")
                    key_hint = ui.label("只显示尾号；点「更换」才能粘贴新的。").classes("hp-field-help")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    key_input = (
                        ui.input(value=_mask(state["tail"]))
                        .props("outlined dense readonly")
                        .classes("hp-input")
                        .mark(f"slot-{slot_key}-key")
                    )
                    ui.button("更换", icon="key").props("flat dense no-caps").classes(
                        "hp-row-button w-auto"
                    ).mark(f"slot-{slot_key}-change").on_click(
                        lambda: _start_change_key(key_input, state, key_hint)
                    )

        with ui.row().classes("hp-actions w-full"):
            test_button = ui.button("测试连接", icon="bolt").props("unelevated dense no-caps").classes(
                "hp-send"
            ).mark(f"slot-{slot_key}-test")
            save_button = ui.button("保存", icon="save").props("flat dense no-caps").classes(
                "hp-row-button w-auto"
            ).mark(f"slot-{slot_key}-save")
            ui.space()
            result = ui.label("还没测过").classes("hp-status")

    def refresh_status() -> None:
        fresh = _slot_item(slot_key)
        configured = bool(fresh["configured"] and fresh["has_key"])
        status.text = "已配置" if configured else "未配置"
        status.classes(add="hp-status-ok") if configured else status.classes(remove="hp-status-ok")
        # 没配好就别让人点测试：点了只会得到一句"先填好…"，不如直接不给点
        test_button.set_enabled(configured)

    def do_save() -> None:
        # 没在"更换"状态就不动已存的 key（api_key=None 的语义）
        api_key = str(key_input.value or "").strip() if state["editing"] else None
        try:
            models_service.save(
                slot_key,
                provider=str(provider.value or ""),
                base_url=str(base_url.value or ""),
                model=str(model.value or ""),
                api_key=api_key,
            )
        except (ValueError, models_service.SecurityError) as exc:
            ui.notify(f"保存失败：{exc}", type="negative")
            return

        state["editing"] = False
        state["tail"] = str(_slot_item(slot_key)["key_tail"])
        key_input.value = _mask(state["tail"])
        key_input.props("outlined dense readonly")
        key_input.props(remove="password")
        key_hint.text = "只显示尾号；点「更换」才能粘贴新的。"
        key_hint.update()
        refresh_status()
        ui.notify(f"{item['title']}已保存", type="positive")

    async def do_test() -> None:
        test_button.props("loading")
        result.text = "正在测试…"
        result.classes(remove="hp-status-ok hp-status-bad")
        result.update()
        try:
            # 网络请求放线程池：探测最长要等 20 秒，绝不能卡住界面
            outcome = await run.io_bound(models_service.test_connection, slot_key)
        finally:
            test_button.props(remove="loading")

        if outcome["ok"]:
            result.text = f"测试通过 · {outcome['elapsed_ms']} ms"
            result.classes(add="hp-status-ok")
            ui.notify(f"{item['title']}：{outcome['detail']}", type="positive")
        else:
            result.text = f"测试失败 · {outcome['detail']}"
            result.classes(add="hp-status-bad")
            result.classes(remove="hp-status-ok")
            ui.notify(f"{item['title']}连接失败：{outcome['detail']}", type="negative")
        result.update()

    save_button.on_click(do_save)
    test_button.on_click(do_test)
    # 在输入框里按回车就保存：填完三个字段不用去够鼠标
    for element in (provider, model, base_url):
        element.on('keydown.enter', do_save)
    refresh_status()


def _start_change_key(key_input: Any, state: dict[str, Any], key_hint: Any) -> None:
    """点「更换」：清空并解锁输入框、切成密码框，让用户粘一把新的（保存前不动库里的）。"""
    state["editing"] = True
    key_input.value = ""
    key_input.props("outlined dense password autofocus")
    key_input.props(remove="readonly")
    key_hint.text = "粘贴新的 key（保存后又会变回只显示尾号）。"
    key_hint.update()
