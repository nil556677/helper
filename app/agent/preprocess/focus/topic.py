"""焦点 · 话题切换：词典打分 + 五信号判定 + 降权不删。

- `topic_scores(query)` 用词典给话题域打分；
- `detect_topic_switch(query, current_topic, focus_stack, new_domain)` 五信号组合；
- `apply_topic_decision` 切换时降权（0.3）不删除、uncertain 温和降权（0.7），每旧话题保留最近 2 个。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.contracts.focus import FocusStack  # 焦点栈

__all__ = ["DOMAIN_LEXICON", "EXPLICIT_SWITCH", "REFERENCE_WORDS", "apply_topic_decision", "detect_topic_switch", "topic_scores"]  # 导出

#: 话题域 → 词典词。
DOMAIN_LEXICON: dict[str, tuple[str, ...]] = {  # 词表
    "order": ("订单", "下单", "购买", "买的", "买"),  # 订单
    "logistics": ("物流", "快递", "运单", "配送", "到哪了", "签收", "发出", "到了"),  # 物流
    "refund": ("退款", "退货", "退钱", "售后"),  # 退款
    "policy": ("政策", "规则", "条款", "规定"),  # 政策
}  # 结束

#: 显式切换词（可短路）。
EXPLICIT_SWITCH: tuple[str, ...] = ("换个话题", "另外", "还有个问题", "不说这个", "问个别的", "顺便问")  # 切换词

#: 指代/指示词（密度高 → 反向信号判延续）。
REFERENCE_WORDS: tuple[str, ...] = ("它", "他", "这个", "那个", "该", "此", "其", "上面那个", "刚才的", "第二个", "第一个", "最后一个")  # 指示词

#: 每旧话题保留最近 N 个。
KEEP_PER_TOPIC = 2  # 保留数


def topic_scores(query: str) -> dict[str, float]:  # 词典打分
    scores: dict[str, float] = {}  # 话题 → 得分
    text = str(query or "")  # 文本
    for domain, words in DOMAIN_LEXICON.items():  # 逐话题
        hit = sum(1 for w in words if w in text)  # 命中词数
        if hit:  # 有命中
            scores[domain] = min(1.0, 0.1 + 0.2 * hit)  # 每词 +0.2
    return scores  # 返回


def detect_topic_switch(  # 五信号判定
    query: str,  # 本轮 query
    current_topic: str,  # 当前话题
    focus_stack: FocusStack | None,  # 焦点栈
    new_domain: str = "",  # 路由层判的 domain（可空）
    reference_density_threshold: float = 0.15,  # 强指代阈值
    topic_score_threshold: float = 0.3,  # 切换高分阈值
) -> str:  # switch / continue / uncertain
    """规则：显式切换词短路 → 强指代判延续(反向) → 实体连续判延续 → domain+词典 → 默认延续。"""
    text = str(query or "")  # 文本
    if any(w in text for w in EXPLICIT_SWITCH):  # 1) 显式切换词
        return "switch"  # 切换
    ref_len = len(text) or 1  # 长度
    density = sum(1 for w in REFERENCE_WORDS if w in text) / ref_len  # 指代密度
    if density > reference_density_threshold:  # 2) 强指代 → 反向判延续
        return "continue"  # 延续
    # 3) 实体连续：本轮实体与焦点栈顶部重叠
    stack = (focus_stack.stack if focus_stack is not None else None) or []  # 栈
    if stack and _overlaps_topic_topstack(text, stack[:3]):  # 与最近实体相关
        return "continue"  # 延续
    # 4) domain 变化 + 词典得分
    scores = topic_scores(text)  # 打分
    top = max(scores, key=scores.get) if scores else ""  # 最高话题
    if top and top != current_topic and scores[top] >= topic_score_threshold:  # 话题变了
        if new_domain == top:  # 路由层一致 → 强化切换
            return "switch"  # 切换
        return "uncertain"  # 信号冲突（保守）
    # 5) 默认延续
    return "continue"  # 兜底


def apply_topic_decision(  # 应用话题决策（降权不删）
    focus_stack: FocusStack,  # 焦点栈（就地改）
    decision: str,  # switch / continue / uncertain
    new_topic: str,  # 新话题
) -> None:  # 无返回
    if decision == "continue":  # 延续：不动，只更新 last_mentioned
        focus_stack.current_topic = new_topic or focus_stack.current_topic  # 更新当前话题
        return  # 结束
    factor = 0.3 if decision == "switch" else 0.7  # 切换狠降 / uncertain 温和降
    kept = dict.fromkeys(_all_topics(focus_stack), 0)  # 各话题已保留计数
    new_stack: list[Any] = []  # 保留的实体
    for e in focus_stack.stack:  # 逐个
        if e.topic and e.topic != new_topic:  # 旧话题实体
            e.weight *= factor  # 降权
            if kept.get(e.topic, 0) >= KEEP_PER_TOPIC:  # 该话题已保留够
                continue  # 淘汰
            kept[e.topic] = kept.get(e.topic, 0) + 1  # 计数
        new_stack.append(e)  # 保留（当前话题全保留）
    focus_stack.stack = new_stack  # 写回
    focus_stack.current_topic = new_topic or focus_stack.current_topic  # 更新当前话题


def _overlaps_topic_topstack(text: str, recent: list[Any]) -> bool:  # 本轮是否包含最近实体的表面形式
    return any(e.surface and e.surface in text for e in recent)  # 表面命中


def _all_topics(fs: FocusStack) -> set[str]:  # 栈里出现的话题
    return {e.topic for e in fs.stack if e.topic}  # 集合
