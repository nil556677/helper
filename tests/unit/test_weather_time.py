"""天气/时间工具：日期格式与窗口分离、空城市、ts 统一、默认格式单一来源。"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 跑异步
import importlib  # 取工具模块

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.wiring import unwrap_tool_output  # 还原包络

weather_mod = importlib.import_module("app.agent.tools.library.weather.query_weather")  # 天气模块
time_mod = importlib.import_module("app.agent.tools.library.time.current_time")  # 时间模块


def _weather(payload: dict, monkeypatch) -> dict:  # 跑天气工具
    async def _fake_json(_url, *, params=None):  # 假网络
        return payload  # 固定返回

    monkeypatch.setattr(weather_mod, "aget_json", _fake_json)  # 打桩网络
    monkeypatch.setattr(weather_mod, "_geocode", lambda city: asyncio.sleep(0, result=(1.0, 2.0)))  # 打桩定位
    env = asyncio.run(weather_mod.query_weather.ainvoke({"city": "成都", "mode": "forecast", "date": "2026-11-10"}))  # 调
    return unwrap_tool_output(env)  # 还原


def test_empty_city_rejected_without_network(monkeypatch) -> None:  # 空城市不请求网络
    def _boom(city):  # 被调用即失败
        raise AssertionError("不应调用定位")  # 断言

    monkeypatch.setattr(weather_mod, "_geocode", _boom)  # 打桩
    env = asyncio.run(weather_mod.query_weather.ainvoke({"city": ""}))  # 空城市（schema 先拦）
    out = unwrap_tool_output(env)  # 还原
    assert out.ok is False  # 失败
    assert out.code == "schema_invalid"  # 参数校验失败


def test_bad_date_format_not_window(monkeypatch) -> None:  # 格式错 ≠ 不在窗口
    env = asyncio.run(weather_mod.query_weather.ainvoke({"city": "成都", "mode": "forecast", "date": "2026/11/03"}))  # 斜杠格式
    out = unwrap_tool_output(env)  # 还原
    assert out.code == str(ErrorCode.PROTOCOL_ARG_INVALID)  # 业务错
    assert "YYYY-MM-DD" in out.message  # 指向格式
    assert "不在" not in out.message  # 不误导为窗口问题


def test_out_of_window_message(monkeypatch) -> None:  # 格式对但超窗口
    out = _weather({"daily": {"time": ["2026-11-10"], "weather_code": [1], "temperature_2m_max": [20], "temperature_2m_min": [10]}}, monkeypatch)  # 窗口内
    assert out.ok  # 应成功（这里用窗口内数据，仅验证结构）


def test_forecast_returns_ts(monkeypatch) -> None:  # forecast 分支也带 ts
    out = _weather({"daily": {"time": ["2026-11-10"], "weather_code": [1], "temperature_2m_max": [20], "temperature_2m_min": [10]}}, monkeypatch)  # 数据
    assert isinstance(out.data.get("ts"), int)  # 有统一时间戳
    assert out.data.get("type") == "forecast"  # 类型


def test_current_ignores_date_and_has_ts(monkeypatch) -> None:  # current 分支
    async def _fake_json(_url, *, params=None):  # 假网络
        assert "daily" not in (params or {})  # current 不该请求 daily
        return {"current_weather": {"weathercode": 1, "temperature": 17, "windspeed": 5}}  # 实时数据

    monkeypatch.setattr(weather_mod, "aget_json", _fake_json)  # 打桩
    monkeypatch.setattr(weather_mod, "_geocode", lambda city: asyncio.sleep(0, result=(1.0, 2.0)))  # 打桩
    env = asyncio.run(weather_mod.query_weather.ainvoke({"city": "成都", "mode": "current"}))  # 调
    out = unwrap_tool_output(env)  # 还原
    assert out.ok and out.data["type"] == "current"  # 实时
    assert isinstance(out.data.get("ts"), int)  # 有 ts


def test_current_time_default_format_single_source() -> None:  # 默认格式单一来源
    env = asyncio.run(time_mod.current_time.ainvoke({"as_timestamp": False}))  # 不传 format
    out = unwrap_tool_output(env)  # 还原
    assert out.ok  # 成功
    assert out.data["format"] == time_mod.DEFAULT_FORMAT  # 用的是模块常量