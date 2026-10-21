"""P7 审查修复的回归测试（每条钉一个"会出事故"的行为）。

覆盖：
- 高危1：重建失败/取消时，**在服旧版本的向量必须还在**（旧实现先删光再重建）；
- 高危2：崩溃重跑认领占位行时，**向量 payload 的 version 必须与 DB 行一致**；
- 高危3：Qdrant 删点失败时**不能删 DB 行**（否则留下 active 孤儿向量被检索召回）；
- 高危5：同一文件并发登记**只落一条占位行**；
- 中危6：超长文本截断要有计数（不能静默丢内容）。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

SAMPLE = (
    "# 采购合同\n\n第一条 乙方应当在三十日内交付设备并开具发票。\n\n"
    "## 付款\n\n验收合格后付款。\n\n## 违约\n\n逾期按日万分之五计算违约金。\n"
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


def _statuses(keys: list[str]) -> dict[str, str]:
    """按 key 取 payload.status（用来断言"哪些版本在服"）。"""
    from app.core.vector import api

    out = {}
    for coll in (api.COLLECTION_CHUNKS, api.COLLECTION_PARENTS):
        for point in api.iter_points(coll):
            key = str(point["payload"].get("key") or "")
            if key in keys:
                out[key] = str(point["payload"].get("status") or "")
    return out


def test_rebuild_failure_keeps_serving_vectors(rag_db: None) -> None:
    """重建失败不能动在服版本：旧实现的"先删后建"会让这份资料静默失搜。"""
    from app.core import paths
    from app.rag import store
    from app.rag.ingest import importer, rebuilder

    result = importer.import_bytes(SAMPLE.encode(), "合同.md", embed=fake_embed)
    doc_id = int(result.doc_id)
    old_version = int(store.get(doc_id)["version"])
    old_keys = store.child_keys(doc_id, old_version)
    assert old_keys, "导入后应该有当前版本的 child 点"

    # 把副本换成空文件 → 重建必然 _NothingToIndex（旧实现此时已把点删光）
    row = store.get(doc_id)
    copy = paths.resolve_stored_path(row["stored_path"])
    assert copy is not None
    copy.write_bytes(b"")

    failed = rebuilder.rebuild_document(doc_id, embed=fake_embed)
    assert failed.status == "failed"
    # **旧向量仍在、仍 active**：资料可以继续被检索
    statuses = _statuses(old_keys)
    assert statuses and all(v == "active" for v in statuses.values()), statuses
    # 候选版本的点一个都不该留下（失败要收干净）
    assert not store.child_keys(doc_id, old_version + 1)


def test_rebuild_success_bumps_version_and_switches(rag_db: None) -> None:
    """重建成功 = 版本 +1、新版本 active、旧版本 superseded（点都还在，可回滚）。"""
    from app.rag import store
    from app.rag.ingest import importer, rebuilder

    result = importer.import_bytes(SAMPLE.encode(), "合同v.md", embed=fake_embed)
    doc_id = int(result.doc_id)
    old_version = int(store.get(doc_id)["version"])
    old_keys = store.child_keys(doc_id, old_version)

    ok = rebuilder.rebuild_document(doc_id, embed=fake_embed)
    assert ok.status == "imported", ok.reason
    assert int(store.get(doc_id)["version"]) == old_version + 1
    statuses = _statuses(old_keys)
    assert all(v == "superseded" for v in statuses.values()), statuses
    new_keys = store.child_keys(doc_id, old_version + 1)
    assert new_keys
    assert all(v == "active" for v in _statuses(new_keys).values())


def test_reclaim_keeps_version_aligned(rag_db: None) -> None:
    """崩溃重跑：认领占位行后，向量 payload version 必须等于 DB 行的 version。"""
    from app.rag import store
    from app.rag.ingest import importer

    # 先有一份 ready 的 v1
    first = importer.import_bytes(SAMPLE.encode(), "合同c.md", embed=fake_embed)
    doc_id = int(first.doc_id)
    assert int(store.get(doc_id)["version"]) == 1

    # 模拟"排队中占位行"（version=2），再带着 claim 导入
    from app.rag import store as st

    placeholder = st.insert_pending("合同c.md", size=len(SAMPLE), file_hash="f" * 64)
    claimed_version = int(st.get(placeholder)["version"])
    assert claimed_version == 2

    second = importer.import_path(
        _staged_copy("合同c.md2", SAMPLE + "\n\n补充条款：逾期三十日可解除。"),  # 内容不同 → 新版本
        "合同c.md",
        claim_doc_id=placeholder,
        embed=fake_embed,
    )
    assert second.status == "imported", second.reason
    row = store.get(placeholder)
    # DB 行版本仍是被分配的那个（没有被重算成 3）
    assert int(row["version"]) == claimed_version, row["version"]
    keys = store.child_keys(placeholder, claimed_version)
    assert keys, "新版本的点应该写在 DB 记录的 version 上"


def test_delete_failure_keeps_db_row(rag_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Qdrant 删点失败 → 不删 DB 行（否则 payload 还是 active 的孤儿点会被检索召回）。"""
    from app.rag import store
    from app.rag.ingest import importer

    result = importer.import_bytes(SAMPLE.encode(), "合同d.md", embed=fake_embed)
    doc_id = int(result.doc_id)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("qdrant 不可用")

    monkeypatch.setattr(store, "delete_chunks", boom)
    removed = importer.delete_documents([doc_id])
    assert removed == 0
    assert store.get(doc_id) is not None, "删点失败时行必须保留（可重试删除）"


def test_concurrent_register_creates_single_row(rag_db: None) -> None:
    """同一文件并发登记只能落一条占位行（查重+插入在同一写事务里，TOCTOU 已封）。"""
    from app.services import knowledge as knowledge_service

    target = knowledge_service.inbox_target("并发.md")
    target.write_bytes(SAMPLE.encode())
    results: list[dict[str, str]] = []
    lock = threading.Lock()

    def register() -> None:
        payload = knowledge_service.register_staged("并发.md", target)
        with lock:
            results.append(payload)

    threads = [threading.Thread(target=register) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4
    doc_ids = {item.get("doc_id") for item in results if item.get("doc_id")}
    skips = [item for item in results if item.get("skip")]
    assert len(doc_ids) == 1, results
    assert len(skips) == 3, results


#: 工作区里真实的 tokenizer（fixture 会把 project_root 指到 tmp，所以这里用文件自身定位）
_REAL_TOKENIZER = Path(__file__).resolve().parents[2] / "models" / "tokenizer.json"


def test_heal_payload_status_repairs_drift_and_orphans(rag_db: None) -> None:
    """对账自愈：ready 文档被翻掉的状态要翻回来；没有资料行认领的点要删掉。

    这条同时钉住"自愈调用的是 api 真实存在的函数"——之前 `api.delete_points` 不存在，
    cleanup 任务直接崩在 AttributeError 上（真实事故）。
    """
    from app.core.vector import api
    from app.rag import store
    from app.rag.ingest import importer
    from app.services import maintenance

    result = importer.import_bytes(SAMPLE.encode(), "自愈.md", embed=fake_embed)
    doc_id = int(result.doc_id)
    version = int(store.get(doc_id)["version"])
    keys = store.child_keys(doc_id, version)
    assert keys

    # 1) 制造"切换中途被杀"的漂移：当前版本的第一个点被翻成 superseded
    api.set_payload(api.COLLECTION_CHUNKS, keys[:1], {"status": "superseded"})
    # 2) 制造孤儿点：一个不存在的资料 id（行都删了，没人能回滚它）
    from app.core.vector import qdrant_store

    qdrant_store.upsert_points_vectorless(
        api.COLLECTION_PARENTS,
        [("424242:1:0", {"documents_id": 424242, "status": "active", "version": 1})],
    )

    healed = maintenance.heal_payload_status()
    assert healed["patched_active"] >= 1, healed
    assert healed["deleted_orphans"] >= 1, healed

    # 漂移被修回 active；孤儿点已消失
    statuses = _statuses(keys)
    assert all(v == "active" for v in statuses.values()), statuses
    remaining = {
        str(point["payload"].get("documents_id"))
        for point in api.iter_points(api.COLLECTION_PARENTS)
    }
    assert "424242" not in remaining


def test_truncation_is_counted(rag_db: None) -> None:
    """超长文本被截断必须计数（静默丢内容 = 数据不精准）。"""
    import importlib.util

    from app.core.embed import tokenization

    if importlib.util.find_spec("tokenizers") is None or not _REAL_TOKENIZER.is_file():
        pytest.skip("本机没有 tokenizers 或 tokenizer.json")

    tokenization.reset_truncation_count()
    try:
        tokenizer = tokenization.load(_REAL_TOKENIZER)
        long_text = "一" * 800  # 远超 320 token
        tokenization.encode(tokenizer, [long_text], max_tokens=320, pad_to=320)
        assert tokenization.truncation_count() == 1
        # 正常长度不计数
        tokenization.encode(tokenizer, ["短句。"], max_tokens=320, pad_to=320)
        assert tokenization.truncation_count() == 1
    finally:
        tokenization.reset_truncation_count()


def test_shutdown_blocks_reopen(rag_db: None) -> None:
    """退出后不允许再把连接打开。

    真实事故：`on_quit` 里 close() 之后，界面 1 秒定时器又跳了一次 → 把客户端重新打开 →
    对象活到解释器卸载，`__del__` 打出 `ImportError: sys.meta_path is None`。
    修法：退出走 `shutdown()`（置最终关闭闸），此后 `client()` 直接报错。
    """
    from app.core.vector import qdrant_store

    qdrant_store.client()  # 先正常打开一次（证明可用）
    qdrant_store.shutdown()
    try:
        with pytest.raises(qdrant_store.VectorStoreError, match="正在退出"):
            qdrant_store.client()
    finally:
        # **直接复位**（不能用 monkeypatch：它会把"已被 shutdown 改成的 True"当成原值恢复回来，
        # 结果污染后面的用例）。shutdown 是进程级终态，测试里手工解除。
        qdrant_store._final = False


def test_close_allows_reopen(rag_db: None) -> None:
    """`close()` 只是关连接（测试/切连接用）——之后还能正常重开。"""
    from app.core.vector import qdrant_store

    qdrant_store.client()
    qdrant_store.close()
    assert qdrant_store.client() is not None


def _staged_copy(name: str, text: str) -> object:
    """把内容落到 tmp 目录并返回路径（供带 claim 的 import_path 用）。"""
    from app.core import paths

    folder = paths.tmp_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.md"
    target.write_bytes(text.encode())
    return target
