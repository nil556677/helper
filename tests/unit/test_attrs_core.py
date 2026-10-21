"""P0 基础改造：动态字段上移 core/attrs + 统一写入器。

覆盖：归一化、版本打标、旧属性清理、compiler 产物、writer 的集合开关。
"""

from __future__ import annotations

TEXT = (
    "合同金额人民币壹佰万元，2024年3月15日签订，第三条，标准 GB/T 1234，"
    "甲方：北京某某科技有限公司"
)


def test_build_attrs_normalized() -> None:
    from app.core.attrs import build_attrs

    attrs = build_attrs(TEXT)
    amounts = {item["key"]: item for item in attrs["attrs_num"]}
    assert amounts["amount"]["value"] == 1000000.0
    assert amounts["amount"]["unit"] == "CNY"

    dates = {item["key"]: item for item in attrs["attrs_date"]}
    assert dates["date"]["value"] == "2024-03-15"
    assert isinstance(dates["date"]["epoch"], int)

    assert "clause_no=3" in attrs["attrs_flat"]
    assert any(item.startswith("code=GB") for item in attrs["attrs_flat"])


def test_enrich_stamps_version_and_clears_stale() -> None:
    from app.core.attrs import ATTRS_VERSION, enrich

    payload = {"text": TEXT, "attrs_flat": ["code=OLD"]}
    enrich(payload, TEXT)
    assert payload["attrs_version"] == ATTRS_VERSION
    # 旧版本的属性被清掉，不会与新结果混在一起
    assert "code=OLD" not in payload["attrs_flat"]


def test_enrich_without_text_only_stamps_version() -> None:
    from app.core.attrs import ATTRS_VERSION, enrich

    payload: dict = {}
    enrich(payload, "")
    assert payload == {"attrs_version": ATTRS_VERSION}


def test_compiler_products() -> None:
    from app.core.attrs.compiler import facet_spec, filter_spec, json_schema

    spec = filter_spec()
    assert "amount" in spec
    assert "gt" in spec["amount"]["ops"]
    # keyword 字段不允许范围算子
    assert "clause_no" not in spec["amount"]["ops"]

    assert json_schema()["properties"]["amount"]["type"] == "number"
    assert any(item["field"] == "amount" for item in facet_spec())


def test_writer_enriches_and_respects_collection_flag(rag_db: None, monkeypatch) -> None:
    from app.core.attrs import ATTRS_VERSION
    from app.core.vector import api
    from app.core.vector.writer import StoredRecord, store_records

    captured: dict = {}

    class _FakeSession:
        def __init__(self, collection: str) -> None:
            captured["collection"] = collection
            captured["entries"] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def add(self, entries: list) -> None:
            captured["entries"].extend(entries)

    monkeypatch.setattr(api, "VectorSession", _FakeSession)

    store_records("rag_chunks", [StoredRecord(key="1:1:0", vector=None, payload={"text": TEXT})])
    child = captured["entries"][0]["payload"]
    assert child["attrs_version"] == ATTRS_VERSION
    assert child["attrs_num"][0]["value"] == 1000000.0

    # parent 集合 enrich_attrs=False：原样写入，不提取
    captured["entries"] = []
    store_records("rag_parents", [StoredRecord(key="1:1:0", vector=None, payload={"text": TEXT})])
    assert captured["entries"][0]["payload"] == {"text": TEXT}


def test_writer_dedupes_same_text_within_batch(rag_db: None, monkeypatch) -> None:
    from app.core.vector import api
    from app.core.vector.writer import StoredRecord, store_records

    calls: list[str] = []

    class _FakeSession:
        def __init__(self, collection: str) -> None:
            self.entries: list = []

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def add(self, entries: list) -> None:
            self.entries.extend(entries)

    monkeypatch.setattr(api, "VectorSession", _FakeSession)
    # 用 spy 包住提取，数它被调了几次
    import app.core.attrs.api as attrs_api

    original = attrs_api.build_attrs

    def spy(text: str):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(attrs_api, "build_attrs", spy)
    store_records(
        "rag_chunks",
        [
            StoredRecord(key="1:1:0", vector=None, payload={"text": TEXT}),
            StoredRecord(key="1:1:1", vector=None, payload={"text": TEXT}),
        ],
    )
    assert len(calls) == 1, "同批同文本只应提取一次"


def test_backfill_attrs_refreshes_stale_points(rag_db: None) -> None:
    """backfill_attrs 把 attrs_version 落后的点按 payload.text 重算。"""
    import numpy as np

    from app.core.attrs import ATTRS_VERSION
    from app.core.config import store as params_store
    from app.core.vector import api, qdrant_store
    from app.rag.ingest import importer
    from app.tasks.backfill_attrs import BackfillAttrs

    def fake_embed(texts):
        size = int(params_store.load()["embed.dims"])
        rows = []
        for text in texts:
            vector = np.zeros(size, dtype=np.float32)
            vector[abs(hash(text)) % size] = 1.0
            rows.append(vector)
        return np.stack(rows)

    result = importer.import_bytes(TEXT.encode(), "回填.md", embed=fake_embed)
    assert result.status == "imported", result.reason
    keys = api.keys(api.COLLECTION_CHUNKS)
    assert keys
    # 人为把版本打回 0、清空属性，模拟"老数据"
    api.set_payload(api.COLLECTION_CHUNKS, keys, {"attrs_version": 0, "attrs_num": []})

    class _Ctx:
        def report(self, *args: object, **kwargs: object) -> None:
            pass

    BackfillAttrs().run(_Ctx(), object())

    found = False
    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        for record in page:
            payload = dict(record.payload or {})
            assert payload.get("attrs_version") == ATTRS_VERSION
            if payload.get("attrs_num"):
                found = True
    assert found, "回填后应重新带上 attrs_num"


def test_store_records_writes_attrs_to_real_qdrant(rag_db: None) -> None:
    """真实 Qdrant：同文本两条记录都要带 attrs（去重不能漏掉后续记录）。"""
    import numpy as np

    from app.core.attrs import ATTRS_VERSION
    from app.core.config import store as params_store
    from app.core.vector import api, qdrant_store
    from app.core.vector.writer import StoredRecord, store_records

    api.ensure(api.COLLECTION_CHUNKS)
    size = int(params_store.load()["embed.dims"])
    vector = np.zeros(size, dtype=np.float32)
    vector[0] = 1.0
    base = {"text": TEXT, "status": "active", "documents_id": 9, "version": 1}

    store_records(
        "rag_chunks",
        [
            StoredRecord(key="9:1:0", vector=vector, payload={**base, "seq": 0}),
            StoredRecord(key="9:1:1", vector=vector, payload={**base, "seq": 1}),
        ],
    )

    found = 0
    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        for record in page:
            payload = dict(record.payload or {})
            assert payload.get("attrs_version") == ATTRS_VERSION
            assert payload.get("attrs_num"), payload
            found += 1
    assert found == 2


def test_rebuild_vectors_refreshes_attrs(rag_db: None, monkeypatch) -> None:
    """全量重嵌会按当前字段字典刷新 attrs（不只是保留原 payload）。"""
    import numpy as np

    from app.core.attrs import ATTRS_VERSION
    from app.core.config import store as params_store
    from app.core.vector import api, qdrant_store
    from app.rag.ingest import importer
    from app.tasks.rebuild_vectors import RebuildVectors

    def fake_embed(texts):
        size = int(params_store.load()["embed.dims"])
        rows = []
        for text in texts:
            vector = np.zeros(size, dtype=np.float32)
            vector[abs(hash(text)) % size] = 1.0
            rows.append(vector)
        return np.stack(rows)

    result = importer.import_bytes(TEXT.encode(), "重嵌.md", embed=fake_embed)
    assert result.status == "imported", result.reason
    keys = api.keys(api.COLLECTION_CHUNKS)
    assert keys
    api.set_payload(api.COLLECTION_CHUNKS, keys, {"attrs_version": 0, "attrs_num": []})

    monkeypatch.setattr("app.core.embed.embed_texts", fake_embed)

    class _Ctx:
        def report(self, *args: object, **kwargs: object) -> None:
            pass

    RebuildVectors().run(_Ctx(), object())

    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        for record in page:
            payload = dict(record.payload or {})
            assert payload.get("attrs_version") == ATTRS_VERSION


def test_describe_staleness_flags_attrs_version(rag_db: None) -> None:
    """index_meta 里记录的动态字段版本对不上时，报告 attrs_stale。"""
    from app.core.config import store as params_store

    config = params_store.load()

    params_store.clear_fingerprints()
    report = params_store.describe_staleness(config)
    assert report["attrs_stale"] is True

    # 记录一次当前指纹后，动态字段不再判过期
    params_store.store_fingerprints(params_store.compute_fingerprints(config), config)
    report2 = params_store.describe_staleness(config)
    assert report2["attrs_stale"] is False


def test_index_status_exposes_attrs_stale(rag_db: None) -> None:
    """服务层的 index_status 把 attrs_stale 透传给界面。"""
    from app.services import settings as settings_service

    settings_service.invalidate_index_status()
    status = settings_service.index_status()
    assert "attrs_stale" in status


def test_field_dict_is_consistent_and_matches_extractors() -> None:
    """字段字典：slot↔类型匹配、key 唯一、与提取器一一对应。"""
    from app.core.attrs import FIELD_DICT, field_dict_err
    from app.core.attrs.extractors import EXTRACTORS

    assert field_dict_err() is None
    assert set(EXTRACTORS) == set(FIELD_DICT), "提取器与字段字典不同步"


def test_type_mismatch_is_not_written() -> None:
    """类型不符的值不被写入（同 key 不会以两种类型落库）。"""
    from app.core.attrs.api import _is_valid
    from app.core.attrs.field_dict import FIELD_DICT

    amount = FIELD_DICT["amount"]
    assert _is_valid({"key": "amount", "value": "不止一个亿"}, amount) is False
    assert _is_valid({"key": "amount", "value": 1000000.0}, amount) is True

    date = FIELD_DICT["date"]
    # date 缺 epoch（归一化没跑成功）→ 丢弃，不能当日期 range 用
    assert _is_valid({"key": "date", "value": "2024-01-01"}, date) is False
    assert _is_valid({"key": "date", "value": "2024-01-01", "epoch": 1700000000}, date) is True

    clause = FIELD_DICT["clause_no"]
    assert _is_valid({"key": "clause_no", "value": "3"}, clause) is True
    assert _is_valid({"key": "clause_no", "value": 3}, clause) is False
