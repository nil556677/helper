"""设置 · 数据源：MySQL 连接配置（只到库；表名工具代码写死）。"""

from __future__ import annotations  # 延迟值注解

from nicegui import ui  # UI

from app.services import settings as settings_service  # 设置读写（走 services，不碰 app.core）

__all__ = ["render"]  # 导出

_KEYS = (  # 后台配置键（agent.mysql.*）
    ("agent.mysql.host", "主机地址", ""),  # 主机
    ("agent.mysql.port", "端口", "3306"),  # 端口
    ("agent.mysql.user", "账号", ""),  # 账号
    ("agent.mysql.password", "密码", ""),  # 密码
    ("agent.mysql.database", "数据库名", ""),  # 库
)  # 结束


def render() -> None:  # 画分区
    """MySQL 连接配置表（只到库）；orders/order_items 表名写死在工具里。"""
    inputs: dict[str, ui.input] = {}  # 字段句柄
    with ui.column().classes("w-full gap-3 hp-card px-3 py-3"):  # 卡片
        ui.label("MySQL 订单库").classes("hp-col-name")  # 标题
        for key, label, default in _KEYS:  # 逐字段
            current = settings_service.get_value(key, default)  # 现值（字符串，不 bool 强转）
            with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 行
                ui.label(label).classes("hp-field-help").style("width:96px;")  # 标签
                inputs[label] = ui.input(str(current)).props("outlined dense").classes("flex-1")  # 输入框
        with ui.row().classes("w-full items-center gap-2"):  # 操作行
            ui.button("保存", on_click=lambda: _save(inputs)).props("unelevated dense")  # 保存
            ui.button("刷新", on_click=lambda: _reload(inputs)).props("flat dense")  # 刷新


def _save(inputs: dict[str, ui.input]) -> None:  # 保存
    for key, _label, _default in _KEYS:  # 逐键
        settings_service.set_value(key, str(inputs[_label].value or ""))  # 写配置
    ui.notify("MySQL 配置已保存", type="positive")  # 提示


def _reload(inputs: dict[str, ui.input]) -> None:  # 重载现值
    for key, _label, default in _KEYS:  # 逐键
        inputs[_label].value = settings_service.get_value(key, default)  # 回填
    ui.notify("已刷新", type="info")  # 提示