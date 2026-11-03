"""共享 HTTP 客户端单测：成功解析 JSON / 配置错(404)不重试直接抛。"""

from __future__ import annotations  # 延迟求值注解

from unittest import mock  # 打桩

import httpx  # 构造错误
import pytest  # 断言抛异常

from app.core.http_client import get_json  # 被测入口（同步）


def test_get_json_ok() -> None:  # 成功路径
    resp = mock.Mock()  # 假响应
    resp.raise_for_status.return_value = None  # 不抛
    resp.json.return_value = {"ok": 1}  # JSON
    with mock.patch("app.core.http_client.httpx.request", return_value=resp):  # 打桩请求
        assert get_json("http://x") == {"ok": 1}  # 解析正确


def test_get_json_config_error_raises_no_retry() -> None:  # 404 配置错
    err = httpx.HTTPStatusError(  # 404 错误
        "404",  # 信息
        request=httpx.Request("GET", "http://x"),  # 请求
        response=httpx.Response(404),  # 404 响应
    )  # 构造结束

    def boom(*args, **kwargs):  # 抛错
        del args, kwargs  # 忽略
        raise err  # 抛

    with mock.patch("app.core.http_client.httpx.request", side_effect=boom):  # 打桩抛错
        with pytest.raises(httpx.HTTPStatusError):  # 应当直接抛（config 不重试）
            get_json("http://x")  # 调用