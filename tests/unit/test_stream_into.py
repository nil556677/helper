"""``_stream_into`` 的入库测试：解析 → 结构 → 切分 → 写入。

用**假嵌入**（不加载模型、不联网），逐段验证四件事真的发生了：

- **解析**：``iter_units`` 按格式产出单元，``on_progress`` 在单元边界回调；
- **结构**：块被补上 ``section_path``（写进 Qdrant payload）；
- **切分**：切出 parent + child 组，数量与写入的点对得上；
- **写入**：``chunks`` 与 ``parents`` 两个集合都有点，状态由 ``chunk_status`` 决定。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

SAMPLE = (
    "# 采购合同\n\n"
    "第一条 乙方应当在三十日内交付设备并开具发票。\n\n"
    "## 付款\n\n验收合格后付款。\n\n"
    "## 违约\n\n逾期按日万分之五计算违约金。\n"
)


def fake_embed(texts: Sequence[str]) -> np.ndarray:
    """确定性假嵌入：每段文本哈希到某一维，不加载 ONNX。"""
    from app.core.config import store as params_store

    size = int(params_store.load()["embed.dims"])
    rows = []
    for text in texts:
        vector = np.zeros(size, dtype=np.float32)
        vector[abs(hash(text)) % size] = 1.0
        rows.append(vector)
    return np.stack(rows)


def _write_doc(tmp_path: Path) -> Path:
    path = tmp_path / "合同.md"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_stream_into_writes_both_collections(rag_db: None, tmp_path: Path) -> None:
    """切出来的组要真的落到 Qdrant：child 数量 = outcome.rows，parent 数量 = outcome.groups。"""
    from app.core.vector import api
    from app.rag.ingest.streaming import _ImportRun, _stream_into

    outcome = _ImportRun()
    _stream_into(
        _write_doc(tmp_path),
        "合同.md",
        1,
        outcome,
        version=1,
        chunk_status="pending",
        embed=fake_embed,
    )

    assert outcome.groups >= 2, "三个标题应至少切出两个 parent"
    assert outcome.rows >= 2, "每个 parent 至少一个 child"
    assert outcome.text.strip(), "正文应被收集（供正文指纹判重用）"

    assert api.count(api.COLLECTION_CHUNKS) == outcome.rows
    assert api.count(api.COLLECTION_PARENTS) == outcome.groups


def test_duplicate_parent_is_written_once(rag_db: None) -> None:
    """两个章节的正文完全相同 → 父块只写一份，两个子块引用同一个 parent_key。"""
    from app.core.chunk import Splitter
    from app.core.parse.model import TYPE_HEADING, Block
    from app.core.vector import api, qdrant_store
    from app.rag import store

    splitter = Splitter(parent_size=1500, parent_overlap=150, child_size=300, child_overlap=45)
    groups = splitter.feed(
        [
            Block(text="甲", content_type=TYPE_HEADING, level=2, section_path=["甲"]),
            Block(text="完全相同的正文。"),
            Block(text="乙", content_type=TYPE_HEADING, level=2, section_path=["乙"]),
            Block(text="完全相同的正文。"),
        ]
    ) + splitter.flush()

    children, _ = store.write_tree(1, groups, start_seq=0, version=1, embed=fake_embed)
    assert children == 2, "两个子块都要写"
    assert api.count(api.COLLECTION_PARENTS) == 1, "内容相同的父块只建一份"
    rows = qdrant_store.scroll_filter(
        api.COLLECTION_CHUNKS, {"documents_id": 1}, with_vectors=False
    )
    parent_keys = {str(dict(r.payload or {}).get("parent_key")) for r in rows}
    assert len(parent_keys) == 1, "两个子块应引用同一个父块"


def test_stream_into_payload_carries_structure_and_status(rag_db: None, tmp_path: Path) -> None:
    """写入的 payload 必须带版本、状态、key、parent 关联与章节路径。"""
    from app.core.vector import api, qdrant_store
    from app.rag.ingest.streaming import _ImportRun, _stream_into

    _stream_into(
        _write_doc(tmp_path),
        "合同.md",
        7,
        _ImportRun(),
        version=3,
        chunk_status="pending",
        embed=fake_embed,
    )

    points = qdrant_store.scroll_filter(
        api.COLLECTION_CHUNKS, {"documents_id": 7}, with_vectors=False
    )
    assert points, "应能按 documents_id 查到 child"
    payload = dict(points[0].payload)

    assert payload["status"] == "pending", "导入期片段应是 pending，揭示后才 active"
    assert payload["version"] == 3
    assert str(payload["key"]).startswith("7:3:"), "key 应是 doc:version:seq"
    assert payload["text"].strip()
    assert payload["parent_key"], "child 必须挂到某个 parent"
    # 结构识别真的跑了：至少一条带非空章节路径
    assert any(
        dict(point.payload).get("section_path") not in (None, "", "[]") for point in points
    ), "section_path 应被填充"


def test_stream_into_honours_chunk_status(rag_db: None, tmp_path: Path) -> None:
    """``chunk_status`` 决定写入状态：传 active 就写 active。"""
    from app.core.vector import api, qdrant_store
    from app.rag.ingest.streaming import _ImportRun, _stream_into

    _stream_into(
        _write_doc(tmp_path),
        "合同.md",
        9,
        _ImportRun(),
        version=1,
        chunk_status="active",
        embed=fake_embed,
    )

    points = qdrant_store.scroll_filter(
        api.COLLECTION_CHUNKS, {"documents_id": 9}, with_vectors=False
    )
    assert points
    assert all(dict(point.payload)["status"] == "active" for point in points)


def test_stream_into_reports_progress_per_unit(rag_db: None, tmp_path: Path) -> None:
    """``on_progress`` 在每个结构单元边界回调（这里是 md → 一个单元）。"""
    from app.rag.ingest.streaming import _ImportRun, _stream_into

    calls: list[tuple[int, int, str]] = []
    _stream_into(
        _write_doc(tmp_path),
        "合同.md",
        11,
        _ImportRun(),
        chunk_status="pending",
        on_progress=lambda done, total, note: calls.append((done, total, note)),
        embed=fake_embed,
    )

    assert calls, "至少应回调一次进度"
    done, total, note = calls[0]
    assert done >= 1
    assert total >= 0
    assert note
