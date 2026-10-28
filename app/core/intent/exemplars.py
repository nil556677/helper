"""L2 向量 kNN：复用嵌入模型，范例向量进程内缓存。

只对 **L1 未命中** 的输入运行；top1 + margin + 每去向阈值都过才采用。
embed 异常 → 返回 None（由上层落默认 search_kb），不阻断整轮。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.embed import embed_text
from app.core.intent import Intent
from app.core.intent.schema import RouteDecision
from app.core.intent.thresholds import MARGIN, THRESHOLDS


@dataclass(frozen=True)
class Exemplar:
    text: str
    intent: Intent
    tool: str = ""


#: 范例：每个去向 5 条左右，覆盖同义改写/口语化。
EXEMPLARS: tuple[Exemplar, ...] = (
    # search_kb（资料问答，默认去向）
    Exemplar("帮我查下知识库里的内容", Intent.TOOL_CALL, "search_kb"),
    Exemplar("资料里是怎么说的", Intent.TOOL_CALL, "search_kb"),
    Exemplar("有没有关于这个的文档", Intent.TOOL_CALL, "search_kb"),
    Exemplar("根据本地资料回答我", Intent.TOOL_CALL, "search_kb"),
    Exemplar("知识库里有没有相关说明", Intent.TOOL_CALL, "search_kb"),
    # chitchat
    Exemplar("你好呀", Intent.CHITCHAT),
    Exemplar("早上好", Intent.CHITCHAT),
    Exemplar("谢谢你", Intent.CHITCHAT),
    Exemplar("回头聊", Intent.CHITCHAT),
    # query_order
    Exemplar("我的订单到哪了", Intent.TOOL_CALL, "query_order"),
    Exemplar("帮我看下订单状态", Intent.TOOL_CALL, "query_order"),
    Exemplar("买的东西发货没", Intent.TOOL_CALL, "query_order"),
    # query_express
    Exemplar("查快递单号", Intent.TOOL_CALL, "query_express"),
    Exemplar("快递到哪了", Intent.TOOL_CALL, "query_express"),
    Exemplar("帮我查物流信息", Intent.TOOL_CALL, "query_express"),
    Exemplar("运单号查询", Intent.TOOL_CALL, "query_express"),
    # time
    Exemplar("现在几点了", Intent.TOOL_CALL, "time"),
    Exemplar("今天几号", Intent.TOOL_CALL, "time"),
    Exemplar("当前时间是多少", Intent.TOOL_CALL, "time"),
    Exemplar("现在是什么时间", Intent.TOOL_CALL, "time"),
    # weather
    Exemplar("今天天气怎么样", Intent.TOOL_CALL, "weather"),
    Exemplar("明天要带伞吗", Intent.TOOL_CALL, "weather"),
    Exemplar("北京冷不冷", Intent.TOOL_CALL, "weather"),
    # human_handoff
    Exemplar("我要找人工", Intent.HUMAN_HANDOFF, "human_handoff"),
    Exemplar("转人工客服", Intent.HUMAN_HANDOFF, "human_handoff"),
    Exemplar("我要投诉", Intent.HUMAN_HANDOFF, "human_handoff"),
)

_vectors: list[tuple[Exemplar, list[float]]] | None = None


def _embed(text: str) -> list[float]:
    """嵌入入口（测试可 monkeypatch 成确定性向量）。"""
    return [float(value) for value in embed_text(text)]


def reset_cache() -> None:
    """清空范例向量缓存（测试 / 换模型后）。"""
    global _vectors
    _vectors = None


def _matrix() -> list[tuple[Exemplar, list[float]]]:
    global _vectors
    if _vectors is None:
        _vectors = [(ex, _embed(ex.text)) for ex in EXEMPLARS if ex.text.strip()]
    return _vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _route_key(ex: Exemplar) -> str:
    if ex.intent is Intent.CHITCHAT:
        return "chitchat"
    if ex.intent is Intent.HUMAN_HANDOFF:
        return "human_handoff"
    return ex.tool or "search_kb"


def score_all(text: str) -> list[tuple[str, float, Exemplar]]:
    """返回按余弦降序的 ``[(route_key, score, exemplar), ...]``（标定/调试用）。"""
    if not text.strip():
        return []
    query = _embed(text)
    scored = [(_route_key(ex), _cosine(query, vec), ex) for ex, vec in _matrix()]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def classify(text: str) -> RouteDecision | None:
    """L2：top1 + margin + 每去向阈值；不通过/异常返回 ``None``。"""
    try:
        scored = score_all(text)
    except Exception:
        return None
    if not scored:
        return None
    route, best_score, best = scored[0]
    second = scored[1][1] if len(scored) > 1 else 0.0
    if best_score - second < MARGIN:
        return None
    if best_score < THRESHOLDS.get(route, 0.6):
        return None
    return RouteDecision(
        intent=best.intent,
        tool=best.tool,
        confidence=round(best_score, 4),
        evidence=f"向量范例：{best.text}（{best_score:.2f}）",
        stage="vector",
    )
