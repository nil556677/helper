"""P6：动态字段提取 + 入库 payload 带 attrs。"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

TEXT = (
    "本合同由甲方：北京某某科技有限公司与乙方：上海某某贸易有限公司于2024年3月15日签订。"
    "第三条：合同总价为人民币壹佰贰拾万元整（¥1,200,000.00），标准 GB/T 1234。"
)


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        v = np.zeros(size, dtype=np.float32)
        v[abs(hash(text)) % size] = 1.0
        rows.append(v)
    return np.stack(rows)


def test_extract_amount_date_clause_party_code() -> None:
    from app.core.attrs import build_attrs

    attrs = build_attrs(TEXT)
    nums = {item["key"]: item["value"] for item in attrs.get("attrs_num", [])}
    assert nums.get("amount") == 1200000.0
    flat = set(attrs.get("attrs_flat", []))
    assert "clause_no=3" in flat, flat
    assert any(v.startswith("code=GB") for v in flat), flat
    parties = {item["value"] for item in attrs.get("attrs", [])}
    assert any("甲方=" in p for p in parties)
    dates = {item["value"] for item in attrs.get("attrs_date", [])}
    assert any("2024" in d for d in dates)


def test_ingest_payload_carries_attrs(rag_db: None) -> None:
    from app.core.vector import qdrant_store
    from app.rag.ingest import importer

    result = importer.import_bytes(TEXT.encode(), "合同.md", embed=fake_embed)
    assert result.status == "imported", result.reason

    found = False
    for page in qdrant_store.scroll_all("rag_chunks", with_vectors=False):
        for rec in page:
            pl = dict(rec.payload or {})
            if pl.get("attrs_num") or pl.get("attrs_flat"):
                found = True
    assert found, "child payload 应带动态字段"
