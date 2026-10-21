"""设置 · 通用：日志位置 + 调试模式 + 本机服务（Qdrant / worker）+ 版本。

"数据目录（备份）"已并进「数据与存储」分区（见 `app/screens/settings/data.py`），
这里只留最基础的几件事。版式：**一张卡一件事**，行内"左名称右控件"。
"""

from __future__ import annotations

from typing import Any

from nicegui import run, ui

from app.services import services as services_service
from app.services import settings as settings_service
from app.ui.components.card import card_head
from app.ui.components.dialog import notify


def render() -> None:
    """画「通用」分区的内容。"""
    overview = settings_service.data_overview()

    with ui.column().classes("w-full gap-4"):
        with ui.column().classes("hp-page-head gap-1"):
            ui.label("通用").classes("hp-page-title")

        # ---- 日志 ----
        # **界面上不提供日志级别**（技术方案 §10.4 第 12 条）：它只由 config.json 给。
        # 用户把级别调成 ERROR 就再也拿不到现场，而且出问题时无法确定"当时是什么级别"；
        # 这里只告诉他日志在哪、怎么打开。
        with ui.column().classes("hp-card w-full gap-3"):
            card_head("subject", "日志")
            with ui.row().classes("hp-field-row"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("日志文件").classes("hp-field-name")
                    ui.label(
                        "按天记录，只留最近 7 天；需要更详细的现场时打开下面的「调试模式」。"
                    ).classes("hp-field-help")
                ui.label(overview.log_file).classes("hp-path")

        # ---- 调试 ----
        with ui.column().classes("hp-card w-full gap-3"):
            card_head("bug_report", "调试模式")
            with ui.row().classes("hp-field-row"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("调试模式").classes("hp-field-name")
                    ui.label(
                        "打开后错误与细节会**打到控制台**（logs/console-*.log 与真实 stderr），"
                        "方便排查问题；平时关着。改动立即生效，不用重启。"
                    ).classes("hp-field-help")
                debug_switch = ui.switch(value=settings_service.get_log_debug()).mark("general-debug")

                def _toggle_debug(event: Any) -> None:
                    on = bool(event.value)
                    settings_service.set_log_debug(on)
                    notify(
                        "调试模式已开启：错误与细节会打到控制台"
                        if on
                        else "调试模式已关闭",
                        kind="positive" if on else "info",
                    )

                debug_switch.on_value_change(_toggle_debug)

        # ---- 本机服务（Qdrant / worker）----
        # 生命周期与主程序一致：启动时拉起、退出时停止（见 app/__main__.py 与 core/proc/services）。
        with ui.column().classes("hp-card w-full gap-3"):
            card_head("dns", "本机服务")
            service_status = ui.label("").classes("hp-field-name")
            service_detail = ui.label("").classes("hp-field-help")

            def _paint_service(snapshot: dict[str, Any]) -> None:
                qdrant = snapshot["qdrant"]
                worker = snapshot["worker"]
                if snapshot["mode"] == "embedded":
                    service_status.text = "本地嵌入式（Qdrant 随主程序打开，不单独起进程）"
                else:
                    service_status.text = (
                        f"服务端 · Qdrant {'运行中' if qdrant['running'] else '未运行'}"
                        f" · worker {'运行中' if worker['running'] else '未运行'}"
                    )
                service_status.update()
                service_detail.text = (
                    f"地址：{snapshot['url'] or '本地 data/qdrant'} · "
                    f"{'远程 Qdrant' if not qdrant.get('local', True) else ('已安装' if qdrant['installed'] else '未安装 bin/qdrant/qdrant.exe')}"
                    " · 服务端随 helper 启动/退出，不单独常驻"
                )
                service_detail.update()

            async def refresh_service() -> None:
                # 服务状态里有 HTTP 健康检查/DB 读取：放线程里，别卡事件循环
                _paint_service(await run.io_bound(services_service.status))

            _paint_service(services_service.status())
            with ui.row().classes("hp-field-row items-end"):
                with ui.column().classes("hp-field-main gap-0"):
                    ui.label("Qdrant HTTP 端口").classes("hp-field-name")
                    ui.label(
                        "端口被占用时换一个再保存。本地嵌入式 → 服务端需要重启 helper 生效；"
                        "服务端使用独立存储，切换后资料需要重建/重新导入。"
                    ).classes("hp-field-help")
                port_input = ui.number(
                    value=services_service.status()["port"], min=1, max=65534, step=1
                ).props("outlined dense").classes("w-32")

            applying = False

            async def apply_service() -> None:
                nonlocal applying
                if applying:
                    return
                applying = True
                try:
                    port = int(port_input.value or services_service.status()["port"])
                    snapshot = await run.io_bound(services_service.status)
                    if snapshot["mode"] == "server":
                        result = await run.io_bound(
                            lambda: services_service.restart(port=port)
                        )
                    else:
                        result = await run.io_bound(
                            lambda: services_service.enable_server(port)
                        )
                    notify(
                        str(result.get("message") or "已处理"),
                        kind="positive" if result.get("ok") else "negative",
                    )
                    await refresh_service()
                finally:
                    applying = False

            with ui.row().classes("hp-actions"):
                ui.button("保存端口并应用", icon="save", on_click=apply_service).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("service-apply")
                ui.button("刷新状态", icon="refresh", on_click=refresh_service).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("service-refresh")
            # 状态自动刷新（3 秒）：Qdrant 崩了/端口被占，界面不用手动点才知道
            ui.timer(3.0, refresh_service)

        # ---- 关于 ----
        with ui.column().classes("hp-card w-full gap-2"):
            card_head("info", "关于")
            with ui.row().classes("items-baseline gap-2"):
                ui.label("helper").classes("hp-brand")
                ui.chip(f"v{settings_service.app_version()}", color="grey-6").props("dense outline").classes("hp-tag-chip")
            ui.label(
                "本地桌面 AI 助手：资料在本机、检索在本机，只把命中的资料片段与问题交给云端大模型。"
            ).classes("hp-field-help")
