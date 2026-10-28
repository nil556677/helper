"""浏览器控制台噪声过滤：只降级**已知无害**的 Quasar tooltip 锚点报错。

背景：``Element.tooltip()`` 用 ``#c<id>`` 作字符串 target，若目标元素比 tooltip 晚挂载，
Quasar 会 ``console.error`` 报"锚点没找到"（上游 issue，纯 UI 提示无功能影响）。
入口的 ``_js_message_level`` 应把它降级到 DEBUG，其余消息保持原级别。
"""

from __future__ import annotations

import pytest  # 参数化与断言


@pytest.mark.parametrize(
    ("level", "message", "expected"),
    [
        # 已知无害：Quasar 的 Anchor 找不到锚点 → 降级 DEBUG
        ("ErrorMessageLevel", 'Anchor: target "#c1402" not found', "DEBUG"),
        # 前后空白也应被识别（整条 strip 后再精确匹配）
        ("ErrorMessageLevel", '  Anchor: target "#c99" not found  ', "DEBUG"),
        # 非 "#c<数字>" 的锚点不静默：保持 ERROR（避免误伤真问题）
        ("ErrorMessageLevel", 'Anchor: target "#foo" not found', "ERROR"),
        # 普通错误不受影响
        ("ErrorMessageLevel", "Uncaught TypeError: x is undefined", "ERROR"),
        # 其它级别沿用原映射
        ("WarningMessageLevel", "deprecated API", "WARNING"),
        ("InfoMessageLevel", "hello", "DEBUG"),
    ],
)
def test_js_message_level(level: str, message: str, expected: str) -> None:
    """已知无害消息降级 DEBUG，其它消息沿用级别映射。"""
    from app.__main__ import _js_message_level  # 被测函数

    assert _js_message_level(level, message) == expected  # 断言分级结果
