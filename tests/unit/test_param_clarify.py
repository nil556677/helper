"""必需参数缺失：模板追问 + 打字接住 + resume 注入。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑 async

import pytest  # 框架

import app.agent.agent as agent_mod  # 图
from app.agent.nodes.dispatch.node import _args_complete, _missing_param, _param_override  # 纯函数
from app.agent.nodes.triage.node import triage_node  # 分诊
from app.agent.routing.rules.loader import load_routes  # 路由


def _spec():  # weather 路由定义
    return {spec.route_id: spec for spec in load_routes()}["weather"]  # 取


def test_missing_param_detected() -> None:  # 缺参判定
    name, ask, options = _missing_param(_spec(), "天气怎么样")  # 缺城市
    assert name == "city" and "哪个城市" in ask and options  # 追问话术+快捷项
    ok_name, ok_ask, _ok = _missing_param(_spec(), "广州天气怎么样")  # 抽到
    assert ok_name == ""  # 不缺


def test_param_override_and_complete() -> None:  # 覆盖/完整性
    assert _param_override("weather", "广州") == {"city": "广州"}  # 覆盖
    assert _args_complete("weather", {"city": "广州"}) is True  # 齐
    assert _args_complete("weather", {"city": ""}) is False  # 缺
    assert _args_complete("time", {}) is True  # 无必需参数


def test_dispatch_asks_when_missing() -> None:  # 未开 HITL：模板追问
    out = asyncio.run(agent_mod.__dict__["dispatch_node"]({"question": "天气怎么样", "route": {"route": "weather"}}))  # 跑
    assert "哪个城市" in out["answer"]  # 追问话术
    assert out["pending_clarify"]["expect"] == "param"  # 待接住（补参模式）
    assert out["pending_clarify"]["param"] == "city"  # 参数名
    assert out["pending_clarify"]["route"] == "weather"  # 待执行路径


def test_triage_catches_typed_param() -> None:  # 下轮打字接住
    out = triage_node(  # 跑
        {
            "question": "广州",  # 用户答复
            "preprocessed": {"query": "广州", "phase": "followup"},  # 跟进轮
            "pending_clarify": {"expect": "param", "param": "city", "route": "weather", "choices": {}},  # 待补参
        }
    )
    assert out["route"]["route"] == "weather"  # 直接路由
    assert out["pending_param"]  # 参数已提取（含"广州"）


def test_weather_args_no_silent_default() -> None:  # 不再静默默认北京
    from app.agent.nodes.dispatch.node import _weather_args  # 被测

    assert _weather_args("天气怎么样")["city"] == ""  # 抽不到就空（由追问兜住）