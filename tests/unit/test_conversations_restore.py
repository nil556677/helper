"""历史恢复的坏数据：turn_json 损坏不能拖垮聊天页。"""

from __future__ import annotations  # 类型注解延迟求值

from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接
from app.ui.state import conversations as ui_conversations  # UI 会话
from app.ui.state.conversations import _turn_from_json  # 被测函数


def _db(fake_root) -> None:  # 建库
    """初始化 v1 schema。"""
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关闭


def test_bad_turn_json_falls_back_to_text(fake_root) -> None:  # 坏 JSON
    """坏 turn_json 退回纯文本。"""
    turn = _turn_from_json("原文", "{bad")  # 解析失败
    assert turn.answer == "原文"  # 文本保留


def test_bad_fragments_are_skipped(fake_root) -> None:  # 坏引用
    """缺字段的引用被跳过，不抛异常。"""
    raw = '{"answer":"a","fragments":[{"text":"x"},{"index":1,"source_name":"s","text":"ok"}]}'  # 坏+好
    turn = _turn_from_json("a", raw)  # 还原
    assert turn.answer == "a"  # 答案
    assert len(turn.fragments) == 1  # 只留好的一条
    assert turn.fragments[0].index == 1  # 序号


def test_restore_with_bad_turn_json(fake_root) -> None:  # 恢复坏历史
    """带坏 turn_json 的历史仍能恢复。"""
    _db(fake_root)  # 建库
    from app.services import conversations as service  # 服务

    service.ensure_conversation("c1", title="t")  # 会话
    service.append_message("c1", role="user", content="q")  # 用户
    get_connection().execute(  # 直接插坏 turn_json
        "INSERT INTO messages(conversation_id, seq, role, content, turn_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("c1", 1, "assistant", "a", "{bad", 1.0),  # 坏数据
    )
    get_connection().commit()  # 提交
    ui_conversations.reset()  # 清空内存
    ui_conversations.restore_from_db()  # 恢复
    items = ui_conversations.conversations()  # 会话
    assert items  # 有会话
    assert items[0].entries[0].turn.answer == "a"  # 退回文本
    ui_conversations.reset()  # 清理
