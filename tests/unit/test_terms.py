"""全局领域词后台 CRUD 的单测（``token_dict`` 表，不碰 Qdrant / 分词）。"""

from __future__ import annotations

import pytest

from app.core.retrieval import terms


def test_add_and_list(ingest_db: None) -> None:
    item = terms.add_term("合同违约金", freq=100)

    assert item.term == "合同违约金"
    assert item.freq == 100
    assert item.enabled is True
    assert [t.term for t in terms.list_terms()] == ["合同违约金"]
    assert terms.get_term("合同违约金") == item


def test_add_strips_surrounding_space(ingest_db: None) -> None:
    assert terms.add_term("  明德公司  ").term == "明德公司"


def test_add_duplicate_rejected(ingest_db: None) -> None:
    terms.add_term("Qdrant")
    with pytest.raises(terms.TermError):
        terms.add_term("Qdrant")


def test_list_is_sorted(ingest_db: None) -> None:
    for word in ("丙", "甲", "乙"):
        terms.add_term(word)
    assert [t.term for t in terms.list_terms()] == ["丙", "乙", "甲"]


def test_update_freq_and_enabled(ingest_db: None) -> None:
    terms.add_term("票据贴现", freq=1)
    updated = terms.update_term("票据贴现", freq=500, enabled=False)

    assert updated.freq == 500
    assert updated.enabled is False
    assert terms.list_terms(enabled_only=True) == []


def test_update_rename(ingest_db: None) -> None:
    terms.add_term("旧名")
    renamed = terms.update_term("旧名", new_term="新名")

    assert renamed.term == "新名"
    assert terms.get_term("旧名") is None
    assert terms.get_term("新名") is not None


def test_update_rename_to_existing_rejected(ingest_db: None) -> None:
    terms.add_term("甲")
    terms.add_term("乙")
    with pytest.raises(terms.TermError):
        terms.update_term("甲", new_term="乙")


def test_update_missing_rejected(ingest_db: None) -> None:
    with pytest.raises(terms.TermError):
        terms.update_term("不存在", freq=1)


def test_delete(ingest_db: None) -> None:
    terms.add_term("待删")
    assert terms.delete_term("待删") is True
    assert terms.delete_term("待删") is False
    assert terms.list_terms() == []


@pytest.mark.parametrize("bad", ["", "   ", "含 空 白", "x" * (terms.MAX_TERM_CHARS + 1)])
def test_invalid_term_rejected(ingest_db: None, bad: str) -> None:
    with pytest.raises(terms.TermError):
        terms.add_term(bad)


@pytest.mark.parametrize("bad_freq", [-1, True, 1.5, "10"])
def test_invalid_freq_rejected(ingest_db: None, bad_freq: object) -> None:
    with pytest.raises(terms.TermError):
        terms.add_term("合法词", freq=bad_freq)  # type: ignore[arg-type]


def test_load_terms_only_enabled(ingest_db: None) -> None:
    terms.add_term("甲", freq=7)
    terms.add_term("乙")
    terms.update_term("乙", enabled=False)

    assert terms.load_terms() == [("甲", 7)]


def test_terms_hash_stable_and_changes(ingest_db: None) -> None:
    assert terms.terms_hash() == "empty"

    terms.add_term("甲", freq=7)
    first = terms.terms_hash()
    assert first.startswith("sha256:")

    # 同一内容反复算，哈希稳定
    assert terms.terms_hash() == first

    terms.update_term("甲", freq=8)
    assert terms.terms_hash() != first

    terms.delete_term("甲")
    assert terms.terms_hash() == "empty"


def test_terms_drive_tokenizer(ingest_db: None) -> None:
    """新增词条后分词器**不用重启**就会把它当整词（靠 generation 失效缓存）。"""
    from app.core.retrieval import tokenize

    tokenize.reset()
    try:
        terms.add_term("甲乙丙丁戊", freq=100000)
        assert tokenize.cut("甲乙丙丁戊") == ["甲乙丙丁戊"]
    finally:
        tokenize.reset()


def test_terms_hash_only_affects_lexical_fingerprint(ingest_db: None) -> None:
    """增删词条只改词法指纹，不动向量指纹（不触发重嵌）。"""
    from app.core.config import store as params_store
    from app.core.config.params import Aspect

    config = params_store.load()
    before = params_store.compute_fingerprints(config)

    terms.add_term("新的领域词")

    after = params_store.compute_fingerprints(config)
    assert before[Aspect.LEXICAL] != after[Aspect.LEXICAL]
    assert before[Aspect.VECTOR] == after[Aspect.VECTOR]


def test_import_export_roundtrip(ingest_db: None) -> None:
    terms.add_term("旧词", freq=5)
    text = terms.export_terms()
    assert "旧词 5" in text
    assert text.startswith("#")

    terms.delete_term("旧词")
    result = terms.import_terms(text)

    assert result.added == 1
    assert result.skipped == 0
    assert terms.get_term("旧词").freq == 5  # type: ignore[union-attr]


def test_import_upsert_and_bad_lines(ingest_db: None) -> None:
    terms.add_term("已有", freq=3)

    result = terms.import_terms("已有\n新词 7\n新词 8\n坏 词频 多\n# 注释\n\n")

    # "已有" 没带词频 → 只更新/启用，保留原词频；"新词 7" 新增；后两行跳过
    assert result.added == 1
    assert result.updated == 1
    assert result.skipped == 2
    assert terms.get_term("已有").freq == 3  # type: ignore[union-attr]
    assert terms.get_term("新词").freq == 7  # type: ignore[union-attr]
    assert len(result.errors) == 2


def test_import_reenables_disabled(ingest_db: None) -> None:
    terms.add_term("停用词")
    terms.update_term("停用词", enabled=False)

    result = terms.import_terms("停用词")

    assert result.updated == 1
    assert terms.get_term("停用词").enabled is True  # type: ignore[union-attr]


def test_export_enabled_only_by_default(ingest_db: None) -> None:
    terms.add_term("启用词")
    terms.add_term("停用词")
    terms.update_term("停用词", enabled=False)

    assert "启用词" in terms.export_terms()
    assert "停用词" not in terms.export_terms()
    assert "停用词" in terms.export_terms(enabled_only=False)


def test_service_wrappers(ingest_db: None) -> None:
    """服务层转成界面用的 dict、且导入结果字段齐全。"""
    from app.services import terms as terms_service

    terms_service.add_term("服务层词", freq=9)
    rows = terms_service.list_terms()
    assert rows[0]["term"] == "服务层词"
    assert rows[0]["freq"] == 9

    result = terms_service.import_term_text("再来一个")
    assert result == {"added": 1, "updated": 0, "skipped": 0, "errors": []}
    assert "再来一个" in terms_service.export_term_text()
