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


def test_trigger_simple_chinese_skipped() -> None:  # 正常中文句 → 不改写
    assert rw.trigger("今天天气怎么样", None) == ""  # 不触发
    assert rw.trigger("年假有几天", None) == ""  # 不触发


def test_trigger_vague_and_pronoun() -> None:  # 含糊/代词触发
    assert rw.trigger("年假怎么清", None) == "vague"  # 动词含糊
    assert rw.trigger("这个怎么算", [{"role": "user", "content": "年假怎么算"}]) == "pronoun"  # 代指
    assert rw.trigger("它几点出发", [{"role": "user", "content": "行程"}]) == "pronoun"  # 代词+多轮


def test_trigger_term_and_cross_lang() -> None:  # 术语/跨语言触发
    assert rw.trigger("annual leave 怎么结", None) == "term"  # 多词术语 → 规则
    assert rw.trigger("how to reset the password", None) == "cross_lang"  # 整句外文 → LLM


def test_term_rule_without_llm(monkeypatch) -> None:  # 术语 → 规则直接改，不调 LLM
    calls = []  # 记录是否调 LLM

    async def generate(request, params):  # 假网关（不应被调）
        del request, params  # 忽略
        calls.append(1)  # 被调
        raise RuntimeError("不应到 LLM")  # 触发即失败

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换
    result = asyncio.run(rw.rewrite("annual leave 怎么算"))  # 跑
    assert result.reason == "term_map"  # 规则映射
    assert result.model == "rule"  # 规则产出
    assert "年假" in result.queries[1]  # 中文等价变体
    assert result.queries[0] == "annual leave 怎么算"  # 原句锚
    assert calls == []  # 没进 LLM


def test_vague_goes_to_llm(monkeypatch) -> None:  # 含糊 → LLM 消歧（mock 多读法）
    async def generate(request, params):  # 假 LLM 返回多读法
        del params  # 忽略
        return SimpleNamespace(  # 文本
            text='["年假怎么清", "如何申请带薪年休假", "年假余额怎么查询"]'  # 消歧变体
        )  # 返回文本

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换
    result = asyncio.run(rw.rewrite("年假怎么清"))  # 跑
    assert result.reason == "used"  # 命中（LLM）
    assert len(result.queries) >= 2  # 原句 + 变体
    assert result.queries[0] == "年假怎么清"  # 原句锚
    assert any("申请" in q or "查询" in q for q in result.queries[1:])  # 有消歧变体


def test_cross_lang_goes_to_llm(monkeypatch) -> None:  # 整句外文 → LLM 双语
    async def generate(request, params):  # 假 LLM 返回双语
        del params  # 忽略
        return SimpleNamespace(  # 文本
            text='["how to reset the password", "如何重置密码"]'  # 原文+中文
        )  # 返回

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换
    result = asyncio.run(rw.rewrite("how to reset the password"))  # 跑
    assert result.queries[0] == "how to reset the password"  # 原文锚
    assert any("重置密码" in q for q in result.queries[1:])  # 有中译


def test_rejected_output_falls_back(monkeypatch) -> None:  # 模型产出不合格 → 回退原句
    async def generate(request, params):  # 假 LLM 返回乱码/解释
        del request, params  # 忽略
        return SimpleNamespace(text="抱歉，我无法改写。")  # 非数组

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换
    result = asyncio.run(rw.rewrite("这个怎么弄", history=[{"role": "user", "content": "报销流程"}]))  # 跑
    assert result.reason == "rejected"  # 拒答回退
    assert len(result.queries) == 1  # 只原句


def test_cache_hits(monkeypatch) -> None:  # 缓存命中不重复调 LLM
    calls = []  # 计数
    text = '["这个怎么算", "年假怎么算"]'  # 变体

    async def generate(request, params):  # 假 LLM
        del request, params  # 忽略
        calls.append(1)  # 计数
        return SimpleNamespace(text=text)  # 返回

    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 替换
    h = [{"role": "user", "content": "年假"}]
    r1 = asyncio.run(rw.rewrite("这个怎么算", history=h))  # 第一次
    monkeypatch.setattr(rw, "_GATEWAY", _fake_gateway(generate))  # 重新注入
    r2 = asyncio.run(rw.rewrite("这个怎么算", history=h))  # 第二次（应命中缓存）
    assert len(calls) == 1  # 只调一次
    assert r2.reason == "used"  # 命中
    assert r1.queries == r2.queries  # 一致
