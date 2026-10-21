"""``codes`` 精确字段——**v12 起不再入 SQLite**（codes 表已删）。

精确字段按重构设计走 Qdrant `attrs_flat`（见 docs/向量层重构设计.md §7），
该能力在阶段 3（P6 动态字段）落地。当前阶段先把这些入口做成安全空实现，
避免调用方在 SQLite 表已删的情况下报错。
"""

from __future__ import annotations

from collections.abc import Sequence


def ingest_codes(
    _conn: object, _doc_id: int, _version: int, *, _from_seq: int = 0, _to_seq: int | None = None
) -> int:
    """阶段 3 前：精确字段暂不落库（改为 attrs_flat）。返回 0。"""
    return 0


def delete_codes(_conn: object, _doc_id: int) -> None:
    """阶段 3 前：无 SQLite 侧数据可删。"""
    return None


def lookup_codes(
    _value: str,
    *,
    _kind: str | None = None,
    _exact: bool = True,
    _doc_ids: Sequence[int] | None = None,
    _limit: int = 50,
) -> list[object]:
    """阶段 3 前：精确查找改由 Qdrant attrs_flat 提供，这里先返回空。"""
    return []


def codes_for_document(
    _doc_id: int, *, _kind: str | None = None, _limit: int = 300
) -> list[object]:
    """阶段 3 前：返回空（详情页精确字段卡暂时为空）。"""
    return []


def code_kinds(_doc_id: int) -> dict[str, int]:
    """阶段 3 前：无分类计数。"""
    return {}
