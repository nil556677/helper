"""澄清/拒绝节点：`semantic_miss` 的三档出口（全模板化，零 LLM）。

三档按"手里还有没有可展示的候选"划分：
① 有可展示候选（≥ 展示下限）→ 选项模板；
② 有候选但全低于下限 → 开放澄清 + 业务预设入口；
③ 候选为空 → 拒绝/降级 + 人工入口。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.contracts.output import Candidate, candidate_id  # 复用澄清候选契约
from app.agent.contracts.routing import ClarifyOptions  # 澄清契约
from app.agent.context import current_run  # 当前 run（指标）
from app.agent.metrics import bump  # 指标累加

__all__ = ["MAX_CLARIFY_ROUNDS", "DISPLAY_MIN", "build_options", "clarify_node", "clarify_text", "resolve_choice"]  # 导出

#: 同一会话最多澄清几轮（防"澄清→miss→再澄清"死循环）。
MAX_CLARIFY_ROUNDS = 2  # 上限

#: 候选展示下限（低于它就是噪声，不给用户看）。
DISPLAY_MIN = 0.25  # 下限

#: 业务侧预设的高频入口（② 档用；不来自语义召回）。
PRESET_ENTRIES: tuple[tuple[str, str], ...] = (  # (展示名, route id)
    ("查时间", "time"),  # 时间
    ("查天气", "weather"),  # 天气
    ("查知识库", "knowledge"),  # 知识库
)  # 预设结束


def build_options(recall: dict[str, Any], attempt: int) -> ClarifyOptions:  # 三档选择
    """按候选可用性分档；轮次超限直接拒绝。"""
    if attempt >= MAX_CLARIFY_ROUNDS:  # 轮次用尽
        return ClarifyOptions(kind="reject", options=(), clarify_attempt=attempt)  # 拒绝
    candidates = [item for item in (recall.get("candidates") or []) if isinstance(item, dict)]  # 候选
    scores = [float(item.get("score") or 0.0) for item in candidates]  # 分数
    if candidates and max(scores) >= DISPLAY_MIN:  # ① 有可展示候选
        from app.agent.routing.labels import route_label  # 人话标签

        selected = [  # 取 2–3 个最高分
            Candidate(id=str(item.get("route") or ""), text=route_label(str(item.get("route") or "")), extra={"score": float(item.get("score") or 0.0)})
            for item in candidates[:3]
            if float(item.get("score") or 0.0) >= DISPLAY_MIN  # 达标
        ]
        if selected:  # 有标签候选
            return ClarifyOptions(kind="candidates", options=tuple(selected), clarify_attempt=attempt)  # 选项
    if candidates:  # ② 有候选但全是噪声
        presets = tuple(Candidate(id=route, text=label) for label, route in PRESET_ENTRIES)  # 业务预设
        return ClarifyOptions(kind="open", options=presets, clarify_attempt=attempt)  # 开放澄清
    return ClarifyOptions(kind="reject", options=(), clarify_attempt=attempt)  # ③ 无候选


def clarify_text(options: ClarifyOptions) -> str:  # 模板化话术
    """结构固定、候选动态；零 LLM，可审计。"""
    if options.kind == "candidates":  # 选项
        names = " / ".join(item.text for item in options.options)  # 候选名
        return f"您是想：{names}，还是其他？"  # 选项模板
    if options.kind == "open":  # 开放澄清
        names = " / ".join(item.text for item in options.options)  # 预设名
        return f"抱歉，我没能理解你的需求，请换一种方式描述，或选择：{names}。"  # 开放模板
    return "抱歉，这个问题超出了我能处理的范围，请换个说法或联系人工。"  # 拒绝模板


def clarify_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """写 clarify_options + 模板 answer；不调模型。"""
    del config  # 不用图配置
    attempt = int(state.get("clarify_attempt") or 0) + 1  # 本次是第几次
    recall = state.get("semantic_recall") or {}  # 语义召回
    options = build_options(recall if isinstance(recall, dict) else {}, attempt)  # 分档
    text = clarify_text(options)  # 话术
    run = current_run()  # 指标
    if run is not None:  # 有 run
        bump(run.metrics, "clarify_count")  # 澄清次数
    return {  # 写回
        "clarify_options": options.model_dump(mode="json"),  # 选项
        "clarify_attempted": True,  # 已澄清
        "clarify_attempt": attempt,  # 次数
        "answer": text,  # 直接作为回答
        "note": "",  # 无额外说明
    }  # 返回


def resolve_choice(value: str, options: ClarifyOptions) -> str | None:  # 用户选择 → route
    """选了明确候选就返回其 route（父图可直路由，避免二次 miss）；否则 None。"""
    choice = str(value or "").strip()  # 规范化
    if not choice:  # 空
        return None  # 无
    for item in options.options:  # 逐项
        if choice in {item.id, item.text}:  # 命中 id 或文案
            return item.id  # 返回 route
    return candidate_id(choice) if choice == "其他" else None  # 其他 → 自由输入