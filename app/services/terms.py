"""设置页「领域词」的服务层：把 core 的 CRUD 转成界面直接能用的数据。

分层：界面只认这里的普通 dict / 字符串，不直接 import ``app.core.retrieval.terms``
（与 ``app/services/settings.py`` 同一约定）。
"""

from __future__ import annotations

from typing import Any

from app.core.retrieval import terms as core

#: 词条非法 / 重复 / 不存在时抛这个；界面统一 catch 它并弹提示。
TermError = core.TermError


def _to_dict(item: core.Term) -> dict[str, Any]:
    return {
        "term": item.term,
        "freq": item.freq,
        "enabled": item.enabled,
        "updated_at": item.updated_at,
    }


def list_terms() -> list[dict[str, Any]]:
    """全部词条（按词条字典序）。"""
    return [_to_dict(item) for item in core.list_terms()]


def add_term(term: str, *, freq: int | None = None) -> dict[str, Any]:
    """新增一条；重复 / 非法抛 :class:`TermError`。"""
    return _to_dict(core.add_term(term, freq=freq))


def rename_term(term: str, new_term: str) -> dict[str, Any]:
    """改名词条（主键变更）。"""
    return _to_dict(core.update_term(term, new_term=new_term))


def set_freq(term: str, freq: int | None) -> dict[str, Any]:
    """改词频；``None`` = 回到 jieba 默认。"""
    return _to_dict(core.update_term(term, freq=freq))


def set_enabled(term: str, enabled: bool) -> dict[str, Any]:
    """启用 / 停用（停用保留词条，只是不参与切词）。"""
    return _to_dict(core.update_term(term, enabled=enabled))


def delete_term(term: str) -> bool:
    """删除词条；返回是否真的删掉了。"""
    return core.delete_term(term)


def import_term_text(text: str) -> dict[str, Any]:
    """批量导入一段文本（一行一词）。返回 ``{added, updated, skipped, errors}``。"""
    result = core.import_terms(text)
    return {
        "added": result.added,
        "updated": result.updated,
        "skipped": result.skipped,
        "errors": list(result.errors),
    }


def export_term_text(*, enabled_only: bool = True) -> str:
    """导出成可再导入的文本。"""
    return core.export_terms(enabled_only=enabled_only)
