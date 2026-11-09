"""引用"查看整篇文档"：doc_id 贯通 + 整篇正文取数。"""

from __future__ import annotations  # 延迟值注解

import json  # 构造历史 turn_json

from app.services.knowledge import queries as knowledge_queries  # 文档取数


class _Cur:  # 假游标
    def __init__(self, rows):  # 构造
        self._rows = rows  # 行

    def fetchall(self):  # 取全部
        return self._rows  # 返回


class _Conn:  # 假连接
    def __init__(self, rows):  # 构造
        self._rows = rows  # 行

    def execute(self, sql, params):  # 执行（忽略 SQL）
        del sql, params  # 忽略
        return _Cur(self._rows)  # 游标


def test_document_full_joins_chunks(monkeypatch) -> None:  # 整篇 = 分块按序拼回
    monkeypatch.setattr(knowledge_queries.kb_store, "get_document", lambda doc_id: {"name": "制度.txt", "version": 1})  # 文档存在
    monkeypatch.setattr(knowledge_queries, "get_connection", lambda: _Conn([{"text": "第一段"}, {"text": "第二段"}, {"text": "  "}]))  # 分块
    data = knowledge_queries.document_full(1)  # 取
    assert data["found"] is True  # 找到
    assert data["text"] == "第一段\n\n第二段"  # 按序拼、空段跳过
    assert data["truncated"] is False  # 未截断


def test_document_full_truncates(monkeypatch) -> None:  # 超长截断
    monkeypatch.setattr(knowledge_queries.kb_store, "get_document", lambda doc_id: {"name": "大文件.txt", "version": 1})  # 存在
    monkeypatch.setattr(knowledge_queries, "get_connection", lambda: _Conn([{"text": "x" * 100}]))  # 超长
    data = knowledge_queries.document_full(1, limit=20)  # 限制 20 字
    assert data["truncated"] is True  # 截断
    assert len(str(data["text"])) == 20  # 长度正确
    assert "只显示前" in str(data["note"])  # 提示


def test_document_full_missing(monkeypatch) -> None:  # 文档不在库里
    monkeypatch.setattr(knowledge_queries.kb_store, "get_document", lambda doc_id: None)  # 不存在
    data = knowledge_queries.document_full(1)  # 取
    assert data["found"] is False and data["note"]  # 诚实说明


def test_fragment_list_carries_doc_id() -> None:  # 事件 → Fragment 带 doc_id
    from app.ui.screens.chat.events import _fragment_list  # 过滤

    frags = _fragment_list([{"index": 1, "text": "正文", "doc_id": 9}])  # 转换
    assert frags[0].doc_id == 9  # 贯通


def test_history_fragment_carries_doc_id() -> None:  # 历史恢复带 doc_id
    from app.ui.state.conversations import _turn_from_json  # 还原

    raw = json.dumps({"fragments": [{"index": 1, "text": "正文", "doc_id": 7}]})  # turn_json
    turn = _turn_from_json("答", raw)  # 还原
    assert turn.fragments[0].doc_id == 7  # 贯通