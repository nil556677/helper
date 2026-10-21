"""内置视图的外链导航闸：只允许应用自己的 origin，外链交系统浏览器。"""

from __future__ import annotations

import pytest

from app.__main__ import _is_allowed_navigation

APP = "http://127.0.0.1:8080/"


def test_same_origin_is_allowed() -> None:
    assert _is_allowed_navigation("http://127.0.0.1:8080/", APP)
    assert _is_allowed_navigation("http://127.0.0.1:8080/#/chat", APP)
    assert _is_allowed_navigation("data:text/html,<b>x</b>", APP)
    assert _is_allowed_navigation("about:blank", APP)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://127.0.0.1:9999/",  # 同主机不同端口
        "http://127.0.0.1:8080.evil.com/",  # 非法端口伪装
        "http://127.0.0.1.evil.com:8080/",  # 后缀伪装
        "javascript:alert(1)",
        "file:///C:/Windows/win.ini",
    ],
)
def test_external_navigation_is_blocked(url: str) -> None:
    assert not _is_allowed_navigation(url, APP)
