"""Skill P0：发现/校验(交叉 catalog)/门控(默认关)/组装/审计。"""

from __future__ import annotations  # 延迟值注解

from unittest.mock import patch  # 打桩

from app.agent.skills import store  # 审计
from app.agent.skills.models import SkillManifest, SkillToolRole  # 模型
from app.agent.skills.registry import (  # 注册
    assemble,
    discover,
    enabled,
    registered,
    resolve_tools,
    validate,
)


def test_discover_finds_bundled() -> None:  # 发现内置示例 skill
    ms = discover()  # 全部
    assert any(m.id == "policy_qa" for m in ms)  # policy_qa 在


def test_prompt_inlined_from_path() -> None:  # PROMPT.md 已内联
    m = next(m for m in discover() if m.id == "policy_qa")  # 取示例
    assert "政策" in m.prompt  # 内容真正读进来了


def test_validate_cross_catalog() -> None:  # 工具必须存在（交叉校验）
    good = SkillManifest(id="x", description="x", tools=[SkillToolRole(name="current_time")])  # 存在工具
    assert validate(good) == []  # 通过
    bad = SkillManifest(id="y", description="y", tools=[SkillToolRole(name="no_such_tool")])  # 不存在
    assert validate(bad)  # 有错误


def test_gate_off_by_default() -> None:  # 默认关
    m = next(m for m in discover() if m.id == "policy_qa")  # 示例
    assert enabled(m) is False  # 环境没开 → 关


def test_gate_on_by_env(monkeypatch) -> None:  # 环境变量显式开
    monkeypatch.setenv("HELPER_SKILL_POLICY_QA", "on")  # 开
    m = next(m for m in discover() if m.id == "policy_qa")  # 示例
    assert enabled(m) is True  # 开


def test_registered_works() -> None:  # 注册表可建（无重复不抛）
    ms = registered()  # 注册
    assert "policy_qa" in ms  # 有示例


def test_assemble_off_returns_none() -> None:  # 未启用不组装
    m = next(m for m in discover() if m.id == "policy_qa")  # 示例
    assert assemble(m, model=object()) is None  # 关 → None


def test_assemble_enabled_resolves_tools(monkeypatch) -> None:  # 启用后解析工具并组装
    from types import SimpleNamespace  # 假模型

    monkeypatch.setenv("HELPER_SKILL_POLICY_QA", "on")  # 开
    m = next(m for m in discover() if m.id == "policy_qa")  # 示例
    tools = resolve_tools(m)  # 解析
    assert any(getattr(t, "name", "") == "search_knowledge_base" for t in tools)  # 主工具在
    fake = SimpleNamespace()  # 假模型（不真绑）

    def _fake_build(model, tools, prompt):  # 桩
        del model, prompt  # 不用
        return {"graph": True, "n_tools": len(tools)}  # 伪图

    with patch("app.agent.react.graph.build_react", _fake_build):  # 打桩 build_react
        g = assemble(m, model=fake)  # 组装
        assert g and g.get("graph") is True and g["n_tools"] >= 1  # 组到且有用具


def test_extra_tools_seam_validates(monkeypatch) -> None:  # 引用 EXTRA_TOOLS 可过校验（MCP 缝）
    from app.agent.skills import registry  # 注册

    monkeypatch.setattr(registry, "EXTRA_TOOLS", {"mcp_http_get": object()})  # 注入
    m = SkillManifest(id="z", description="z", tools=[SkillToolRole(name="mcp_http_get")])  # 清单
    assert validate(m) == []  # 通过（来自 EXTRA_TOOLS）


def test_store_record_and_counts(rag_db) -> None:  # skill 审计（只追加 + 聚合）
    store.record_event(skill_id="policy_qa", event_type="enabled", version=1, actor_id="user")  # 记启用
    store.record_event(skill_id="policy_qa", event_type="scored", version=1, detail={"score": 0.9})  # 记评分
    c = store.counts("policy_qa")  # 该 skill
    assert c.get("enabled", 0) >= 1  # 有启用
    assert c.get("scored", 0) >= 1  # 有评分
