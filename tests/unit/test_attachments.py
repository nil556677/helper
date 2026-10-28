"""附件服务与队列附件：落盘、可读文本、公开字段。"""

from __future__ import annotations  # 类型注解延迟求值

from app.services import attachments  # 附件服务
from app.ui.state.chat_queue import MessageQueue  # 队列


def test_attachment_roundtrip(fake_root) -> None:  # 附件
    """文本附件可读，公开字段不带正文。"""
    path, safe = attachments.prepare("说明.txt")  # 目标
    path.write_text("附件正文", encoding="utf-8")  # 写
    item = attachments.finalize(path, safe)  # 元数据
    assert item["kind"] == "text"  # 类型
    assert item["text"] == "附件正文"  # 文本
    assert "text" not in attachments.public(item)  # 公开不带正文
    assert "附件正文" in attachments.prompt_block([item])  # 拼提示词
    attachments.remove(item)  # 删除
    assert not path.exists()  # 已删


def test_queue_allows_attachment_only(fake_root) -> None:  # 只有附件
    """没有文字但有附件也能入队。"""
    queue = MessageQueue()  # 队列
    reason = queue.push("", "c1", attachments=[{"name": "a.txt"}])  # 入队
    assert reason is None  # 成功
    assert queue.items()[0].text == "请查看附件。"  # 占位
    assert queue.items()[0].attachments  # 附件保留


def test_queue_clear_by_conversation() -> None:  # 取消清队列
    """只清指定会话的排队；别的会话保留。"""
    queue = MessageQueue()  # 队列
    queue.push("A1", "c1")  # c1
    queue.push("A2", "c1")  # c1
    queue.push("B1", "c2")  # c2
    removed = queue.clear("c1")  # 清 c1
    assert removed == 2  # 删了两条
    assert [item.text for item in queue.items()] == ["B1"]  # c2 保留
    assert queue.clear() == 1  # 全清
    assert not queue  # 空
