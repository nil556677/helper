"""时间工具：返回当前本地时间（含星期）。"""

from __future__ import annotations

from datetime import datetime

from app.core.intent import RouteDecision
from app.ui.state.chat_model import Turn

_WEEKDAYS = "一二三四五六日"


def handler(text: str = "", decision: RouteDecision | None = None) -> Turn:
    del text, decision
    now = datetime.now().astimezone()
    return Turn(answer=f"现在是 {now:%Y-%m-%d %H:%M:%S}（周{_WEEKDAYS[now.weekday()]}）。")
