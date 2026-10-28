"""界面用的格式化（时间 / 体积）。

**为什么单独一个模块**：任务页与知识库页都要显示"多久以前"和"多大"，原来两边各写了一份
（改一处忘一处）。两个以上页面用的、与业务无关的小工具放 `app/components/`。
"""

from __future__ import annotations

from datetime import datetime


def human_time(stamp: float | None, *, empty: str = "还没") -> str:
    """时间戳 → 人看的相对时间（"刚刚 / 2 分钟前 / 3 小时前 / 2 天前 / 2025-10-19"）。

    参数：
        stamp: 时间戳；``None`` 或 0 表示没有。
        empty: 没有时间时显示什么（"还没导过"、"还没轮询过"…）。

    "刚刚"阈值是 10 秒（60 秒太宽：50 秒前的事显示"刚刚"，与自动刷新的
    "刚刚更新"（<3 秒）打架）；超过 30 天直接给日期——"367 天前"不如日期可读。
    """
    if not stamp:
        return empty
    try:
        stamp = float(stamp)  # 归一：坏数据（字符串时间戳）不炸界面，直接当作"没有"
    except (TypeError, ValueError):
        return empty
    seconds = max(0.0, datetime.now().timestamp() - stamp)
    if seconds < 10:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)} 天前"
    return f"{datetime.fromtimestamp(stamp):%Y-%m-%d}"


def human_size(size: int | None) -> str:
    """字节 → 人看的单位（列表里不显示 1234567 这种数）。"""
    value = int(size or 0)
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    if value < 1024**3:
        return f"{value / 1024 / 1024:.1f} MB"
    if value < 1024**4:
        return f"{value / 1024**3:.2f} GB"
    return f"{value / 1024**4:.2f} TB"


def human_duration(seconds: float | None) -> str:
    """时长 → 人看的写法（"<1 秒 / 0.8 秒 / 12 秒 / 3 分 05 秒 / 1 小时 02 分"）。

    任务详情用它显示"这次跑了多久"；``None``（还没开始/没结束）显示 ``—``。
    """
    if seconds is None:
        return "—"
    value = max(0.0, float(seconds))
    if value < 0.05:
        return "<0.1 秒"
    if value < 1:
        return f"{value:.1f} 秒"
    if value < 60:
        return f"{value:.0f} 秒"
    if value < 3600:
        return f"{int(value // 60)} 分 {int(value % 60):02d} 秒"
    return f"{int(value // 3600)} 小时 {int((value % 3600) // 60):02d} 分"
