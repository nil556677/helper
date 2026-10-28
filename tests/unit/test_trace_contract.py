"""M0 契约：证据碎片必须带齐溯源字段，且缺字段时不炸。"""

from __future__ import annotations  # 类型注解延迟求值

from langchain_core.documents import Document  # 文档形状

from app.agent.nodes.answer import node as answer_mod  # 片段组装
from app.rag import evidence as evidence_mod  # 证据组装


def _doc(**meta) -> Document:  # 造一条命中
    """默认一条 parent 回填（fetch_mode=1）的命中。"""
    base = {  # 基础 metadata
        "doc_id": 3,  # 文档
        "chunk_id": 11,  # 命中 child
        "parent_id": 7,  # 父块
        "version": 2,  # 版本
        "source_name": "合同.docx",  # 来源
        "score": 0.5,  # 分数
        "score_kind": "rrf",  # 语义
        "loc": {"page": 1},  # 定位
        "fetch_mode": 1,  # 取父块
    }  # 基础结束
    base.update(meta)  # 覆盖
    return Document(page_content="命中片段正文", metadata=base)  # 文档


def test_evidence_carries_trace_fields(monkeypatch) -> None:  # 正
    """正常路径：五个溯源字段都要有值。"""
    monkeypatch.setattr(  # 父块
        evidence_mod.kb_store, "parents_for", lambda ids: {7: {"text": "父块正文", "heading": "标题"}}
    )  # 替换结束
    monkeypatch.setattr(  # 文档行
        evidence_mod.kb_store, "documents_by_ids", lambda ids: {3: {"text_hash": "hash-3"}}
    )  # 替换结束
    items = evidence_mod.build_evidence([_doc()], top_k=3)  # 组装
    assert len(items) == 1  # 一条
    item = items[0]  # 取
    assert item["doc_id"] == 3  # 文档
    assert item["chunk_id"] == 11  # child
    assert item["parent_id"] == 7  # 父块
    assert item["version"] == 2  # 版本
    assert item["text_hash"] == "hash-3"  # 内容指纹


def test_evidence_survives_missing_document_row(monkeypatch) -> None:  # 反
    """读库失败时字段降级为空，但不能让检索失败。"""
    monkeypatch.setattr(evidence_mod.kb_store, "parents_for", lambda ids: {7: {"text": "父块正文"}})  # 父块
    def boom(ids):  # 假故障
        raise RuntimeError("库挂了")  # 抛
    monkeypatch.setattr(evidence_mod.kb_store, "documents_by_ids", boom)  # 替换
    items = evidence_mod.build_evidence([_doc()], top_k=3)  # 组装
    assert items[0]["text_hash"] == ""  # 降级为空
    assert items[0]["doc_id"] == 3  # id 仍在


def test_evidence_text_branch_carries_trace_fields() -> None:  # 正（另一分支）
    """`evidence_text` 分支（精排后已拼好的证据）也要带字段。"""
    doc = _doc(evidence_text="拼好的证据", relation_ok=True)  # 带拼好文本
    items = evidence_mod.build_evidence([doc], top_k=3)  # 组装
    assert items[0]["doc_id"] == 3  # 文档
    assert items[0]["chunk_id"] == 11  # child
    assert items[0]["parent_id"] == 7  # 父块
    assert items[0]["version"] == 2  # 版本


def test_fragments_keep_trace_fields() -> None:  # 正
    """答案节点把溯源字段原样带进 fragments（落库后历史轮仍可校验）。"""
    evidence = [  # 一条证据
        {  # 证据
            "index": 1,  # 序号
            "source_name": "合同.docx",  # 来源
            "origin": "第 1 页",  # 出处
            "score": 0.5,  # 分数
            "score_kind": "rrf",  # 语义
            "text": "正文",  # 正文
            "snippet": "片段",  # 命中段
            "doc_id": 3,  # 文档
            "chunk_id": 11,  # child
            "parent_id": 7,  # 父块
            "version": 2,  # 版本
            "text_hash": "hash-3",  # 指纹
        }  # 证据结束
    ]  # 列表结束
    fragments = answer_mod._fragments([1], evidence)  # 组装
    assert fragments[0]["doc_id"] == 3  # 文档
    assert fragments[0]["chunk_id"] == 11  # child
    assert fragments[0]["parent_id"] == 7  # 父块
    assert fragments[0]["version"] == 2  # 版本
    assert fragments[0]["text_hash"] == "hash-3"  # 指纹
