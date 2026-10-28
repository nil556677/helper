"""聊天页的弹窗（本轮设置/调试信息）。"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.ui.components.format import human_duration


def _open_settings_dialog(
    settings: dict[str, Any],  # 本轮设置
    *,
    usage: dict[str, Any] | None = None,  # 用量（可选）
    duration_ms: int | None = None,  # 总耗时（可选）
    ttft_ms: int | None = None,  # 首字耗时（可选）
    rewrite: str | None = None,  # 改写结论（可选）
    queries: list[str] | None = None,  # 实际查过的查询（可选）
    gate: str | None = None,  # 门限结论（可选）
) -> None:
    """本轮设置/调试信息；只读。只在调试模式有按钮进来。"""
    data = settings or {}  # 数据
    usage_data = usage or {}  # 用量
    with ui.dialog() as dialog, ui.card().classes("w-[760px] max-w-full gap-2"):  # 弹窗
        ui.label("本轮设置").classes("hp-card-title")  # 标题
        ui.label(f"模型：{data.get('model') or '未配置'}").classes("hp-field-help")  # 模型
        ui.label(  # 温度 / 引用上限 / 证据数
            f"温度：{data.get('temperature', 0.2)}"
            f" · 引用上限：{data.get('top_k', 6)}"
            f" · 证据段：{data.get('evidence', 0)}"
        ).classes("hp-field-help")  # 一行
        if rewrite is not None:  # 有改写结论
            ui.label(f"改写：{rewrite}").classes("hp-field-help")  # 结论
        if queries:  # 有实际查询
            ui.label("查询：" + " ｜ ".join(queries)).classes("hp-field-help")  # 列表
        if gate is not None:  # 有门限结论
            ui.label(f"门限：{gate}").classes("hp-field-help")  # pass/weak/reject
        if duration_ms is not None or ttft_ms is not None:  # 有耗时
            ui.label(  # 耗时
                f"总耗时 {human_duration((duration_ms or 0) / 1000)}"
                f" · 首字 {human_duration((ttft_ms or 0) / 1000)}"
            ).classes("hp-field-help")  # 一行
        if usage_data:  # 有 token 用量
            tokens = [  # 挑有值的项
                f"{name}={value}"  # 片段
                for name, value in (  # 遍历
                    (
                        ("输入", usage_data.get("prompt_tokens")),  # 输入
                        ("输出", usage_data.get("completion_tokens")),  # 输出
                        ("缓存读", usage_data.get("cache_read_tokens")),  # 缓存读
                        ("缓存写", usage_data.get("cache_creation_tokens") or usage_data.get("cache_write_tokens")),  # 缓存写（旧数据叫 cache_write_tokens）
                        ("思考", usage_data.get("reasoning_tokens")),  # 思考
                        ("字符", usage_data.get("completion_chars")),  # 字符
                    )
                )  # 结束
                if value  # 只在有值时展示
            ]  # 结束
            if tokens:  # 有可展示项
                ui.label(" · ".join(tokens)).classes("hp-field-help")  # 用量
        ui.label("系统提示词").classes("hp-field-name pt-2")  # 标题
        ui.textarea(value=str(data.get("system_prompt") or "（暂无）")).props(
            "readonly autogrow"
        ).classes("w-full hp-pre").style("max-height: 360px; overflow: auto;")  # 提示词
        with ui.row().classes("w-full justify-end"):  # 底部
            ui.button("关闭", on_click=dialog.close).props("flat")  # 关闭
    dialog.open()  # 打开
