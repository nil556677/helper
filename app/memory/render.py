"""画像渲染：把 active 条目渲染成 `<profile>` 块文本（供上下文装配）。"""

from __future__ import annotations  # 延迟值注解

from app.memory.model import ProfileEntry  # 条目

__all__ = ["render_profile_block"]  # 导出


def render_profile_block(entries: list[ProfileEntry]) -> str:  # 渲染画像块
    """把条目渲染成一行一条的块；空则返回空串（装配不产生空块）。

    ``[0.95]`` 显示可信度；低置信（<0.3，过 contest 的弱项）标 ``[可能]``，
    提醒模型按非事实对待（方案 6.3.3：低置信不直接作为事实注入）。
    """
    lines = []  # 行
    for entry in entries:  # 逐条
        conf = float(entry.confidence or 0)  # 可信度
        marker = "[可能]" if conf < 0.3 else f"[{conf:.2f}]"  # 低置信标注
        src = str(entry.source or "")  # 来源
        value_text = __import__("json").dumps(entry.value, ensure_ascii=False, default=str)  # 值序列化
        lines.append(f"- {marker} {entry.prop_key}={value_text} (source={src})")  # 一行
    return "\n".join(lines) if lines else ""  # 空则空串
