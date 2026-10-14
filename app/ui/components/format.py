"""界面用的格式化（时间 / 体积）。

**为什么单独一个模块**：任务页与知识库页都要显示"多久以前"和"多大"，原来两边各写了一份
（改一处忘一处）。两个以上页面用的、与业务无关的小工具放 `app/components/`。
"""

from __future__ import annotations

from datetime import datetime


def human_time(stamp: float | None, *, empty: str = "还没") -> str:
    """时间戳 → 人看的相对时间（"刚刚 / 2 分钟前 / 3 小时前 / 2 天前"）。

    参数：
        stamp: 时间戳；``None`` 或 0 表示没有。
        empty: 没有时间时显示什么（"还没导过"、"还没轮询过"…）。
    """
    if not stamp:
        return empty
    seconds = max(0.0, datetime.now().timestamp() - stamp)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def human_size(size: int | None) -> str:
    """字节 → 人看的单位（列表里不显示 1234567 这种数）。"""
    value = int(size or 0)
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / 1024 / 1024:.1f} MB"
