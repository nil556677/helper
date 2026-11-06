"""L1 Lazy Rewrite：**仅当规则判定需要**才触发一次，失败回退原 query。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, Callable  # 类型

from app.agent.contracts.triage import EntityBundle  # 契约

__all__ = ["RewriteFn", "maybe_rewrite", "needs_rewrite"]  # 导出

RewriteFn = Callable[[str, EntityBundle], str]  # 改写函数（一次）

#: 需要改写的指代词（无显式实体时）。
_PRONOUNS: tuple[str, ...] = ("它", "他", "她", "那个", "这个", "该", "上述")  # 指代


def needs_rewrite(query: str, entities: EntityBundle) -> bool:  # 是否需要改写
    """有指代且没有显式实体 → 需要一次改写。"""
    text = str(query or "")  # 原文
    has_pronoun = any(word in text for word in _PRONOUNS)  # 有指代
    has_entity = bool(entities.explicit)  # 有显式实体
    return has_pronoun and not has_entity  # 两者同时成立才改写


def maybe_rewrite(  # 可能改写（最多一次）
    query: str,  # 原 query
    entities: EntityBundle,  # 实体
    flags: dict[str, Any],  # 硬约束
    rewrite: RewriteFn | None = None,  # 改写实现（可注入；None 表示不改写）
) -> tuple[str, bool, str]:  # (query, rewritten, reason)
    """flagged no_rewrite / 不需要 / 无改写实现 都不改写。"""
    if flags.get("no_rewrite"):  # 被 flag 禁止
        return query, False, "flag_blocked"  # 不改
    if not needs_rewrite(query, entities):  # 不需要
        return query, False, ""  # 不改
    if rewrite is None:  # 无实现
        return query, False, "no_rewriter"  # 不改
    try:  # 改写一次
        rewritten = str(rewrite(query, entities) or "").strip()  # 调
    except Exception:  # 失败回退
        return query, False, "rewrite_failed"  # 原 query
    if not rewritten:  # 空结果
        return query, False, "rewrite_empty"  # 原 query
    return rewritten, True, "rewritten"  # 新 query
