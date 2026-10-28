"""自由聊天处理器（L1 命中寒暄后调用；LLM 出口预留）。"""

from __future__ import annotations

from app.chat import template
from app.core.intent import RouteDecision
from app.ui.state.chat_model import Turn

_PHRASES = {
    "greet": template.GREET,
    "thanks": template.THANKS,
    "bye": template.BYE,
}


def chitchat_handler(text: str = "", decision: RouteDecision | None = None) -> Turn:
    """按寒暄子类挑话术；没有子类就回闲聊兜底。"""
    subtype = ""
    if decision is not None:
        subtype = str(decision.slots.get("subtype") or "")
    phrase = _PHRASES.get(subtype, template.CHITCHAT_FALLBACK)
    del text
    return Turn(answer=phrase)
