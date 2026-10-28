"""父图 + 批处理入口：统一到 RAG 异步图（方案 A：无路由/补参/工具）。"""

from __future__ import annotations  # 类型注解延迟求值

import pytest  # 测试框架

from app.agent.runtime import run_turn  # 批处理入口
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect  # 连接


@pytest.fixture(autouse=True)
def _db(fake_root):  # 每个用例独立库
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


def test_run_turn_returns_turn(fake_root, monkeypatch):  # 正常路径
    """图返回什么状态，批处理就还原成什么 Turn。"""
    from app.agent import runtime  # 局部导入

    class Stub:  # 假图
        async def ainvoke(self, state, config):  # 异步调用
            return {**state, "answer": "批处理答案", "fragments": [], "note": ""}  # 固定状态

    monkeypatch.setattr(runtime, "get_graph", lambda: Stub())  # 接桩
    turn = run_turn("你好", session="batch-1")  # 跑
    assert turn.answer == "批处理答案"  # 状态被还原
    assert turn.note_kind == "plain"  # 默认样式


def test_run_turn_graph_failure_is_wrapped(fake_root, monkeypatch):  # 失败路径
    """图抛异常 → 失败轮，且不把内部细节丢给用户。"""
    from app.agent import runtime  # 局部导入

    class Boom:  # 抛错的假图
        async def ainvoke(self, *_args, **_kwargs):  # 异步调用
            raise RuntimeError("retrieval exploded")  # 炸

    monkeypatch.setattr(runtime, "get_graph", lambda: Boom())  # 接桩
    turn = run_turn("查一下资料", session="batch-2")  # 跑
    assert turn.note_kind == "error"  # 失败样式
    assert "retrieval exploded" not in turn.note  # 不暴露细节


def test_run_turn_graph_unavailable(fake_root, monkeypatch):  # 取图失败
    """构图就失败 → 失败轮。"""
    from app.agent import runtime  # 局部导入

    def broken():  # 取图即炸
        raise RuntimeError("checkpoint db locked")  # 炸

    monkeypatch.setattr(runtime, "get_graph", broken)  # 接桩
    turn = run_turn("你好", session="batch-3")  # 跑
    assert turn.note_kind == "error"  # 失败样式
