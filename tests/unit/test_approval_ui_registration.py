"""审批 UI：设置页已注册「审批」分区。"""

from __future__ import annotations  # 延迟值注解

from app.ui.screens import settings as settings_mod  # 设置外壳


def test_approvals_section_registered() -> None:  # 分区已注册
    keys = [key for key, _title, _icon in settings_mod.SECTIONS]  # 分区 key
    assert "approvals" in keys  # 有审批分区
    assert callable(settings_mod.RENDERERS["approvals"])  # 渲染函数可调