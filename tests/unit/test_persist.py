"""落库收口：原子写整轮、空答案也配对、幂等、失败不抛（类 D 优化）。"""

from __future__ import annotations  # 类型注解延迟求值

import pytest  # 测试框架

from app.agent.context import RunContext  # 运行上下文
from app.agent.persist import persist_turn  # 统一落库
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect  # 连接
from app.services import conversations  # 会话服务


@pytest.fixture(autouse=True)
def _db(fake_root):  # 隔离库
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


def _ctx(conversation_id: str) -> RunContext:  # 造上下文
    return RunContext(run_id="r", question="q", conversation_id=conversation_id)  # 新


def test_append_turn_writes_user_and_assistant_at_once(fake_root) -> None:  # 原子写
    """一次写入 user+assistant 两行，seq 连续、标题被更新。"""
    conversations.append_turn("c1", question="违约金怎么算", answer="按合同", turn_json={"answer": "按合同"})  # 写
    rows = conversations.load_messages("c1")  # 读
    assert [row["role"] for row in rows] == ["user", "assistant"]  # 配对
    assert [row["seq"] for row in rows] == [0, 1]  # 序号连续
    assert conversations.list_conversations()[0]["title"] == "违约金怎么算"  # 标题


def test_persist_turn_writes_assistant_even_when_answer_empty(fake_root) -> None:  # 取消也配对
    """取消时答案为空：助手行仍写，历史配对不断。"""
    ctx = _ctx("c2")  # 上下文
    ok = persist_turn(ctx, conversation_id="c2", question="问题", answer="", note="已停止生成。")  # 写
    assert ok is True  # 成功
    assert ctx.persisted is True  # 已标记
    rows = conversations.load_messages("c2")  # 读
    assert ctx.turn_id == str(rows[0]["id"])  # 逻辑轮次 id = user 消息 id
    assert [row["role"] for row in rows] == ["user", "assistant"]  # 配对
    assert rows[1]["content"] == ""  # 内容可为空
    assert "已停止生成" in str(rows[1]["turn_json"])  # 说明进 turn_json


def test_persist_turn_is_idempotent(fake_root) -> None:  # 幂等
    """已落过再调不重复写。"""
    ctx = _ctx("c3")  # 上下文
    assert persist_turn(ctx, conversation_id="c3", question="q", answer="a") is True  # 第一次
    assert persist_turn(ctx, conversation_id="c3", question="q", answer="a") is True  # 第二次
    assert len(conversations.load_messages("c3")) == 2  # 只有一轮


def test_persist_turn_failure_does_not_raise(fake_root, monkeypatch) -> None:  # 失败不阻断
    """落库失败返回 False，不置 persisted，也不抛。"""
    def boom(*_args, **_kwargs):  # 炸
        raise RuntimeError("db down")  # 抛

    monkeypatch.setattr("app.agent.persist.conversations.append_turn", boom)  # 注入失败
    ctx = _ctx("c4")  # 上下文
    assert persist_turn(ctx, conversation_id="c4", question="q", answer="a") is False  # 失败
    assert ctx.persisted is False  # 未标记
