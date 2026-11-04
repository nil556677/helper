"""改写走统一网关：未配置/失败一律回退原句（不阻断检索）。"""

from __future__ import annotations  # 延迟求值注解

import asyncio  # 跑异步 rewrite
from types import SimpleNamespace  # 挂普通 async 函数(不被 self 绑定)

from app.agent.llm.types import ModelUnavailable  # 槽位不可用
import app.rag.rewrite as rw  # 被测改写


def _fake_gateway(generate) -> SimpleNamespace:  # 造假网关
    """把 async 函数实例属性上(不是类型方法), 调用即直通 2 参不绑 self。"""
    return SimpleNamespace(generate=generate)  # 挂实例属性


def test_rewrite_unconfigured_falls_back(monkeypatch) -> None:  # 未配置 → 回退原句
    async def generate(request, params):  # 假网关
        del request, params  # 忽略
        raise ModelUnavailable("no slot")  # 未配置

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换网关

    result = asyncio.run(  # 异步跑
        rw.rewrite("它现在几点", history=[{"role": "user", "content": "昨天几号"}])  # 多轮触发改写
    )  # 结束
    assert result.reason == "unconfigured"  # 未配置
    assert len(result.queries) == 1  # 只回原句


def test_rewrite_gateway_failure_falls_back(monkeypatch) -> None:  # 网关失败 → 回退原句
    async def generate(request, params):  # 假网关
        del request, params  # 忽略
        raise RuntimeError("boom")  # 其他失败

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换网关

    result = asyncio.run(  # 异步跑
        rw.rewrite("她几点出发", history=[{"role": "user", "content": "行程"}])  # 多轮触发
    )  # 结束
    assert result.reason == "failed"  # 失败
    assert len(result.queries) == 1  # 只回原句