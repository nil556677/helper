"""异常/健壮性测试：嵌套动态字段提取、归一化、写入器、回填在畸形输入下不崩、不吐脏。

目标（对齐 ``tests/unit/test_exceptions.py`` 的口径）：
- 畸形文本/畸形中文数字/非法日期 → 跳过、不抛；
- 缺字段/错类型的 payload 与记录 → 不被写入脏值、或给出干净可分类的错；
- 字段字典被改坏 → ``field_dict_err()`` 能识别；
- ``store_records`` 缺向量 → 给 ``VectorError``（不是 numpy 原始错）。
"""

from __future__ import annotations

import pytest

# ---------------- 归一化：畸形 / 非法 ----------------

def test_normalize_money_skips_malformed_and_keeps_valid() -> None:
    """中文金额畸形（十百/一百零）不抛、不产脏值；有效金额照常归一化。"""
    from app.core.attrs.normalize import money_values

    values = {value for value, _raw in money_values("人民币十百元 人民币一百零 人民币壹佰万元")}
    assert 1000000.0 in values  # 有效的那条在
    assert all(isinstance(value, (int, float)) for value in values)


def test_money_requires_unit_not_quantity() -> None:
    """不带单位（元/万/亿/圆）的数字跟在人民币后，**不能**当金额（防量词误报）。"""
    from app.core.attrs.normalize import money_values

    # 普通量词/裸数不是金额
    assert money_values("人民币三个") == []
    assert money_values("人民币十个") == []
    assert money_values("人民币若干") == []
    # 有单位才提取
    vals = {value for value, _raw in money_values("人民币三元 人民币十万元 人民币若干个")}
    assert 3.0 in vals
    assert 100000.0 in vals


def test_normalize_date_skips_invalid() -> None:
    """非法日期（2024-02-30 / 2024年13月40日）不做成可范围比较的东西。"""
    from app.core.attrs.normalize import date_values

    dates = {iso for iso, _epoch, _raw in date_values("2024年13月40日 2024-02-30 2024年3月15日")}
    assert "2024-03-15" in dates
    assert "2024-02-30" not in dates  # 非法日被丢
    assert all(len(iso) == 10 for iso in dates)  # 都是规整 ISO


# ---------------- 提取：畸形文本 / 类型 ----------------

def test_build_attrs_hostile_text_does_not_crash() -> None:
    """超长 + 控制字符 + 孤立代理项文本进 build_attrs：不抛，槽内只会有 list。"""
    from app.core.attrs import build_attrs

    hostile = (
        "第" * 5000 + "条" * 5000 + "人民币" * 5000
        + "\x00\x01\x02" + "\ud800" + "甲方：" * 3000
        + "2024年99月1日 GB/T / 1234"
    )
    attrs = build_attrs(hostile)
    assert isinstance(attrs, dict)
    for slot in ("attrs", "attrs_num", "attrs_flat", "attrs_date"):
        if slot in attrs:
            assert isinstance(attrs[slot], list)


def test_apply_attrs_rejects_non_dict_attrs() -> None:
    """apply_attrs 拿到非 dict 的属性 → 抛错（契约：提取结果必须是 dict）。"""
    from app.core.attrs.api import apply_attrs

    with pytest.raises((TypeError, ValueError)):
        apply_attrs({}, ["不是", "dict"])


def test_enrich_empty_text_only_stamps_version() -> None:
    """空正文 enrich：只打版本，不产生任何 attrs 槽。"""
    from app.core.attrs import ATTRS_VERSION, enrich

    payload: dict = {"text": "   "}
    enrich(payload, "   ")
    assert payload["attrs_version"] == ATTRS_VERSION
    for slot in ("attrs", "attrs_num", "attrs_flat", "attrs_date"):
        assert slot not in payload


# ---------------- 字段字典校验 ----------------

def test_field_dict_validator_catches_bad_type_in_slot(monkeypatch) -> None:
    """改成错的 slot↔类型组合，field_dict_err 必须报错。"""
    from app.core.attrs import FIELD_DICT, field_dict_err
    from app.core.attrs.field_dict import AttrSpec

    monkeypatch.setitem(FIELD_DICT, "amount", AttrSpec(type="string", slot="attrs_num"))
    assert field_dict_err() is not None  # attrs_num 只收 float


def test_field_dict_validator_catches_keyword_in_string_slot(monkeypatch) -> None:
    """keyword 类型塞进 attrs（string 槽）→ 报错。"""
    from app.core.attrs import FIELD_DICT, field_dict_err
    from app.core.attrs.field_dict import AttrSpec

    monkeypatch.setitem(FIELD_DICT, "code", AttrSpec(type="keyword", slot="attrs"))
    assert field_dict_err() is not None


# ---------------- quantity 字段：确定数量 vs 模糊/条款 ----------------

def test_quantity_extracts_clear_counts() -> None:
    """三个→3、十个人→10、两个→2、五台→5；同值去重。"""
    from app.core.attrs import build_attrs

    attrs = build_attrs("需要三个箱子、十个人、两台设备、五台主机和三个备件")
    quantities = {item["value"] for item in attrs.get("attrs_num", []) if item["key"] == "quantity"}
    assert {3, 10, 2, 5} <= quantities


def test_quantity_skips_vague_and_clause_number() -> None:
    """模糊数（几个/若干/几十）不是数量；「第X条」是条款号、不是数量。"""
    from app.core.attrs.extractors import extract_quantities

    assert [i["value"] for i in extract_quantities("几个 若干 几十")] == []
    assert [i["value"] for i in extract_quantities("第三条 十个人")] == [10]  # 第X条被排除
    assert [i["value"] for i in extract_quantities("三个")] == [3]


# ---------------- 写入器：缺向量 / 空集 / extract=/缺文本 ----------------

def test_store_records_rejects_missing_vector(rag_db: None) -> None:
    """向量集合写入缺 vector → 干净 VectorError（不是 numpy 原始错）。"""
    from app.core.vector import api
    from app.core.vector.writer import StoredRecord, store_records

    with pytest.raises(api.VectorError):
        store_records("rag_chunks", [StoredRecord(key="9:1:0", vector=None, payload={"text": "内容"})])


def test_store_records_empty_is_noop() -> None:
    """空记录列表 → 返回 0、不碰 Qdrant/DB。"""
    from app.core.vector.writer import store_records

    assert store_records("rag_chunks", []) == 0


def test_store_records_extract_false_keeps_payload_clean(rag_db: None, tmp_path) -> None:
    """extract=False 的记录：不补 attrs、不写版本，payload 原样。"""
    import numpy as np

    from app.core.config import store as params_store
    from app.core.vector import api, qdrant_store
    from app.core.vector.writer import StoredRecord, store_records

    api.ensure(api.COLLECTION_CHUNKS)
    size = int(params_store.load()["embed.dims"])
    vector = np.zeros(size, dtype=np.float32)
    vector[0] = 1.0

    store_records(
        "rag_chunks",
        [StoredRecord(key="9:1:0", vector=vector, payload={"text": "金额人民币壹佰万元"}, extract=False)],
    )
    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        for record in page:
            payload = dict(record.payload or {})
            assert "attrs_num" not in payload
            assert "attrs_version" not in payload


# ---------------- 回填：空 / 空文本 / 已是最新 ----------------

def _put(rag_db: None, entries) -> None:
    """向 rag_chunks 直接写几条可控点（绕过 store_records，便于模拟老数据）。"""
    import numpy as np

    from app.core.config import store as params_store
    from app.core.vector import api

    api.ensure(api.COLLECTION_CHUNKS)
    size = int(params_store.load()["embed.dims"])
    vector = np.zeros(size, dtype=np.float32)
    vector[0] = 1.0
    with api.VectorSession(api.COLLECTION_CHUNKS) as session:
        session.add(
            [{"key": key, "vector": vector, "payload": {**payload, "key": key}}
             for key, payload in entries]
        )


def _scroll_payloads() -> list[dict]:
    """读回 rag_chunks 的所有 payload（每份都带写入时塞的 ``key``）。"""
    from app.core.vector import api, qdrant_store

    out: list[dict] = []
    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        for record in page:
            out.append(dict(record.payload or {}))
    return out


def test_backfill_refills_stale_and_skips_empty_and_current(rag_db: None) -> None:
    """回填：落后且有空文本→补上；空文本→跳过；已最新→跳过；不崩。"""
    from app.core.attrs import ATTRS_VERSION
    from app.tasks.backfill_attrs import BackfillAttrs

    _put(
        rag_db,
        [
            # 落后 + 有正文 → 会被回填
            ("9:1:0", {"text": "金额人民币壹佰万元，2024年3月15日", "status": "active",
                       "documents_id": 9, "version": 1, "seq": 0, "attrs_version": 0}),
            # 空正文 → 跳过（无法提取，也不该崩）
            ("9:1:1", {"text": "", "status": "active", "documents_id": 9,
                       "version": 1, "seq": 1, "attrs_version": 0}),
            # 已最新 → 跳过
            ("9:1:2", {"text": "内容", "status": "active", "documents_id": 9,
                       "version": 1, "seq": 2, "attrs_version": ATTRS_VERSION}),
        ],
    )

    class _Ctx:
        def report(self, *args: object, **kwargs: object) -> None:
            pass

    note = BackfillAttrs().run(_Ctx(), object()).note
    assert note == "已回填 1 块", note

    by_key = {p["key"]: p for p in _scroll_payloads()}
    first = by_key.get("9:1:0", {})
    assert first.get("attrs_version") == ATTRS_VERSION
    assert first.get("attrs_num"), "落后且有正文的点应补上 attrs_num"
    second = by_key.get("9:1:1", {})
    assert second.get("attrs_version") == 0, "空正文点应被跳过、不误标"
    third = by_key.get("9:1:2", {})
    assert third.get("attrs_version") == ATTRS_VERSION


# ---------------- 额外异常面（第二轮） ----------------

def _unit_vector() -> object:
    """给一片单元维度设为 1 的向量（写入用）。"""
    import numpy as np

    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    vector = np.zeros(size, dtype=np.float32)
    vector[0] = 1.0
    return vector


def test_store_records_writes_hostile_text_to_real_qdrant(rag_db: None) -> None:
    """真实 Qdrant + 超长/含控制符文本：不崩、已写、槽内是干净 list。"""
    from app.core.vector import api, qdrant_store
    from app.core.vector.writer import StoredRecord, store_records

    api.ensure(api.COLLECTION_CHUNKS)
    hostile = "第" * 8000 + "人民币" * 4000 + "\x00\x01" + "甲方：" * 3000 + "三点五"
    store_records(
        "rag_chunks",
        [StoredRecord(key="9:1:0", vector=_unit_vector(), payload={"text": hostile})],
    )
    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        for record in page:
            payload = dict(record.payload or {})
            assert payload.get("attrs_version")
            for slot in ("attrs", "attrs_num", "attrs_flat", "attrs_date"):
                if slot in payload:
                    assert isinstance(payload[slot], list)


def test_enrich_rejects_non_str_text() -> None:
    """enrich 传 bytes 正文 → TypeError（契约：text 必须是 str）。"""
    from app.core.attrs import enrich

    with pytest.raises(TypeError):
        enrich({}, b"not str")


def test_store_records_rejects_non_dict_payload() -> None:
    """payload 不是 dict → dict() 抛 TypeError（契约：payload 必须是 dict）。"""
    from app.core.vector.writer import StoredRecord, store_records

    with pytest.raises(TypeError):
        store_records("rag_chunks", [StoredRecord(key="x", vector=None, payload=[1, 2, 3])])


def test_apply_attrs_resets_malformed_slots() -> None:
    """payload 里已有的 attrs 槽被塞了脏值（字符串）→ apply_attrs 重置为 list，不残留。"""
    from app.core.attrs.api import apply_attrs, build_attrs

    payload = {"text": "金额人民币壹佰万元", "attrs_num": "garbage", "attrs_flat": 123}
    apply_attrs(payload, build_attrs(payload["text"]))
    assert isinstance(payload["attrs_num"], list)
    assert {i["value"] for i in payload["attrs_num"]} & {1000000.0}
    assert isinstance(payload.get("attrs_flat", []), list)


def test_cn_parsing_pathological_no_crash() -> None:
    """畸形/超长中文数字组合：不抛、返回 None 或数字（不产生垃圾值）。"""
    from app.core.attrs.extractors import _cn_positive_int
    from app.core.attrs.normalize import _cn_number

    for seq in ("万亿亿亿", "兆", "点", "一二三亿亿", "负三", "十万百万", "零"):
        # 不抛即可；None = 判定不可归一的畸形，数字 = 确定性结果
        _cn_number(seq)
        _cn_positive_int(seq)


def test_build_attrs_large_noise_no_crash() -> None:
    """大量混合噪声（含量词、条款、金额、日期模式）→ 不崩、槽内是 list。"""
    from app.core.attrs import build_attrs

    noise = ("三个 第五条 人民币十元 2023年1月1日 GB/T 1 甲方：某某公司 两 十个人 若干 几个 "
             "一点五 " * 3000)
    attrs = build_attrs(noise)
    assert isinstance(attrs, dict)
    for slot in ("attrs", "attrs_num", "attrs_flat", "attrs_date"):
        if slot in attrs:
            assert isinstance(attrs[slot], list)


def test_quantity_fractional_no_crash() -> None:
    """带小数点的数量：能解析就转 int、不能就丢，反正不崩。"""
    from app.core.attrs.extractors import extract_quantities

    result = extract_quantities("一点五三个 十个人")
    values = [i["value"] for i in result]
    assert 10 in values
    assert all(isinstance(v, int) for v in values)


def test_backfill_no_collection_is_noop(rag_db: None) -> None:
    """集合不存在时回填：空转 0 条、不崩。"""
    from app.tasks.backfill_attrs import BackfillAttrs

    class _Ctx:
        def report(self, *args: object, **kwargs: object) -> None:
            pass

    # rag_db 不 ensure 集合 → rag_chunks 不存在 → 空转
    note = BackfillAttrs().run(_Ctx(), object()).note
    assert note == "已回填 0 块", note


def test_store_records_duplicate_key_upserts_once(rag_db: None) -> None:
    """同一 key 写两次 → Qdrant 幂等覆盖，只留 1 条。"""
    from app.core.vector import api, qdrant_store
    from app.core.vector.writer import StoredRecord, store_records

    api.ensure(api.COLLECTION_CHUNKS)
    record = StoredRecord(key="9:1:0", vector=_unit_vector(), payload={"text": "三个箱子"})
    store_records("rag_chunks", [record])
    store_records("rag_chunks", [record])  # 重复投递

    count = 0
    for page in qdrant_store.scroll_all(api.COLLECTION_CHUNKS, with_vectors=False):
        count += len(page)
    assert count == 1, "重复 key 应被 upsert 覆盖成 1 条"


def test_describe_staleness_survives_corrupt_stored_attrs_version(rag_db: None) -> None:
    """index_meta 里 attrs.version 存了非数字垃圾 → attrs_stale=True、不崩。"""
    from app.core.config import store as params_store
    from app.core.config.params import KEY_ATTRS_VERSION

    config = params_store.load()
    params_store.clear_fingerprints()
    params_store._upsert_meta(KEY_ATTRS_VERSION, "not-a-number")
    report = params_store.describe_staleness(config)
    assert report["attrs_stale"] is True
