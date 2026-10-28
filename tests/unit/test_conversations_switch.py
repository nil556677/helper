"""会话切换/删除：当前会话删除后自动切下一个。"""

from __future__ import annotations  # 类型注解延迟求值

from app.ui.state import conversations as ui  # UI 会话


def test_select_and_delete(fake_root) -> None:  # 切换/删除
    """切换有回调，删除当前会切到剩余会话。"""
    ui.reset()  # 清空
    first = ui.new()  # 会话 1
    second = ui.new()  # 会话 2
    selected: list[str] = []  # 回调记录
    ui.subscribe_select(lambda: selected.append(ui.current().id))  # 订阅
    ui.select(first.id)  # 切 1
    assert selected[-1] == first.id  # 回调
    ui.select(second.id)  # 切 2
    assert selected[-1] == second.id  # 回调
    ui.delete(second.id)  # 删当前
    assert ui.current().id == first.id  # 自动切回
    assert selected[-1] == first.id  # 重画回调
    assert len(ui.conversations()) == 1  # 只剩一个
    ui.reset()  # 清理
