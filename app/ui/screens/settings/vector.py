"""设置 · 向量（P1.12）：两张卡——向量数据 / Qdrant 控制台。

**为什么单独一页**：向量是本产品最"重"的一层（模型、维度、后端、缓存、对账），
把它塞进"通用"会让人找不到；这一页只留"看数据"和"打开控制台"。

两条规矩：
1. **危险动作不放在这一页**（清空向量集合已移除）；
2. **只展示能解释的数字**（条数/维度），不展示容易误导的目录大小。
"""

from __future__ import annotations

import subprocess
import sys

from nicegui import ui

from app.services import vector_admin as admin_service
from app.ui.components.card import card_head, stat
from app.ui.components.dialog import notify


def render() -> None:
    """画「向量」分区（真数据；刷新只重画数据卡）。"""
    body = ui.column().classes("w-full gap-4")

    def refresh() -> None:
        data = admin_service.overview()
        body.clear()
        with body:
            _render_collections(data)
            _render_engine()

    refresh()


#: 集合名太技术，界面用中文；未知集合原样显示。
_COLLECTION_LABELS = {
    "rag_chunks": "检索片段",
    "rag_parents": "父块",
}


def _render_collections(data: dict) -> None:
    """卡 1：向量数据（只看条数与维度）。"""
    with ui.card().classes("hp-card w-full gap-3"):
        card_head("storage", "向量数据")
        if data.get("error"):
            ui.label("向量库暂不可用").classes("hp-field-name text-negative")
            ui.label(str(data["error"])).classes("hp-field-help")
            return
        if not data["collections"]:
            ui.label("还没有任何向量：导入资料后会自动排队建。").classes("hp-field-help")
            return
        with ui.element("div").classes("hp-stats w-full"):
            for item in data["collections"]:
                name = _COLLECTION_LABELS.get(str(item["name"]), str(item["name"]))
                stat(f"{item['vectors']:,}", f"{name} · {item['dims']} 维")


def _copy_key(key: str) -> bool:
    """把 key 放进系统剪贴板（优先 clip.exe，避免从 NiceGUI 线程碰 Qt 剪贴板）。"""
    if not key:
        return False
    if sys.platform == "win32":
        try:
            # clip.exe 是系统自带；输入走 stdin，不把 key 放进命令行/日志。
            proc = subprocess.run(
                ["clip"],
                input=key,
                text=True,
                capture_output=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                return True
        except Exception:
            pass
    try:
        from PySide6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(key)
            return True
    except Exception:
        pass
    return False


def _render_engine() -> None:
    """卡 2：Qdrant 控制台（地址 + 复制 key）。"""
    url = admin_service.dashboard_url()
    with ui.card().classes("hp-card w-full gap-3"):
        card_head("dns", "Qdrant 控制台")
        ui.label("在本机服务端运行；控制台可查看集合与 point。").classes("hp-field-help")
        if not url:
            ui.label("本地嵌入式，没有 HTTP 控制台").classes("hp-field-help")
            return
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.label("地址").classes("hp-field-name")
            # 点击由主程序导航守卫交给系统浏览器打开（应用内不加载外部页面）
            ui.link(url, url).props("target=_blank").classes("hp-path").mark("vec-url")

            def _copy_key_click() -> None:
                key = admin_service.dashboard_key()
                if not key:
                    notify("当前没有配置 Qdrant API Key", kind="warning")
                    return
                if _copy_key(key):
                    notify("API Key 已复制；到控制台右上角「API Key」里 Ctrl+V", kind="positive")
                else:
                    notify("复制 API Key 失败，请检查系统剪贴板权限", kind="negative")

            ui.button("复制 API Key", icon="content_copy", on_click=_copy_key_click).props(
                "flat dense no-caps"
            ).mark("vec-copy-key")
        if not admin_service.dashboard_web_ui_ready():
            ui.label("缺少 Web UI 静态文件（bin/qdrant/static），地址可能打不开").classes(
                "hp-field-help text-negative"
            )
        ui.label("打开后点右上角 API Key，Ctrl+V 粘贴已复制的 key。").classes("hp-field-help")
