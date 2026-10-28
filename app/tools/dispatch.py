"""工具分发：查注册表 → 校验启用/参数 → 执行或明确降级。

**工具失败绝不回落 search_kb**：用户问订单，搜资料库是答非所问。
"""

from __future__ import annotations

import re

from app.core.intent import RouteDecision
from app.core.intent.slots import label as param_label
from app.core.intent.slots import normalize_param
from app.tools.registry import enabled, get
from app.ui.state.chat_model import Turn


def validate(spec, decision: RouteDecision) -> tuple[str, str]:
    """按 ``param_schema`` 校验；返回 ``(kind, detail)``。

    kind: ``""`` 通过 / ``"missing"`` 缺参数（detail=参数名）/ ``"bad"`` 格式错（detail=文案）。
    """
    for name, rule in spec.param_schema.items():
        raw = str(decision.slots.get(name) or "").strip()
        value = normalize_param(name, raw) if raw else ""
        if raw and not value:
            return "bad", f"参数 {param_label(name)} 格式不对"
        if rule.get("required") and not value:
            return "missing", name
        pattern = rule.get("pattern")
        if value and pattern and re.fullmatch(str(pattern), value) is None:
            return "bad", f"参数 {param_label(name)} 格式不对"
    return "", ""


def validate_decision(decision: RouteDecision) -> tuple[str, str]:
    """校验一个决策（search_kb/未知工具不校验参数）。"""
    spec = get(decision.tool or "")
    if spec is None or spec.name == "search_kb":
        return "", ""
    return validate(spec, decision)


def tool_handler(text: str, decision: RouteDecision) -> Turn:
    spec = get(decision.tool or "")
    if spec is None:
        return Turn(note=f"没有「{decision.tool or ''}」这个工具。")
    if not enabled(spec.name):
        return Turn(note=f"「{spec.description}」已停用。")
    # search_kb 的 query 就是用户原文，不走 param_schema（否则永远缺参数）。
    if spec.name != "search_kb":
        kind, detail = validate(spec, decision)
        if kind == "missing":
            return Turn(note=f"缺少参数：{detail}")
        if kind == "bad":
            return Turn(note=detail)
    if spec.handler is None:
        return Turn(note="这个工具没有实现。", note_kind="error")
    return spec.handler(text, decision)
