"""澄清/拒绝节点：三档分派、轮次上限、选择可接住。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.contracts.output import Candidate  # 候选
from app.agent.contracts.routing import ClarifyOptions  # 澄清契约
from app.agent.nodes.clarify.node import (  # 节点
    MAX_CLARIFY_ROUNDS,
    PRESET_ENTRIES,
    build_options,
    clarify_node,
    clarify_text,
    resolve_choice,
)


def _recall(*scores: float) -> dict:  # 造召回 JSON
    return {"candidates": [{"route": f"r{i}", "score": s} for i, s in enumerate(scores)]}  # 候选


def test_tier1_shows_candidates() -> None:  # ① 有可展示候选
    options = build_options(_recall(0.9, 0.8, 0.1), 1)  # 分档
    assert options.kind == "candidates"  # 档位
    assert [item.id for item in options.options] == ["r0", "r1"]  # 只留达标
    assert "您是想" in clarify_text(options)  # 模板


def test_tier2_open_with_presets() -> None:  # ② 全是噪声
    options = build_options(_recall(0.1, 0.05), 1)  # 分档
    assert options.kind == "open"  # 开放澄清
    assert [item.id for item in options.options] == [route for _, route in PRESET_ENTRIES]  # 业务预设
    assert "换一种方式" in clarify_text(options)  # 模板


def test_tier3_reject_when_empty() -> None:  # ③ 无候选
    options = build_options({}, 1)  # 分档
    assert options.kind == "reject" and options.options == ()  # 拒绝
    assert "超出" in clarify_text(options)  # 模板


def test_round_limit_forces_reject() -> None:  # 轮次上限
    options = build_options(_recall(0.99), MAX_CLARIFY_ROUNDS)  # 已达上限
    assert options.kind == "reject"  # 强制拒绝


def test_clarify_node_writes_state() -> None:  # 节点写 state
    out = clarify_node({"semantic_recall": _recall(0.9), "clarify_attempt": 0})  # 首次
    assert out["clarify_options"]["kind"] == "candidates"  # 档位
    assert out["clarify_attempt"] == 1  # 次数 +1
    assert out["answer"]  # 有模板话术
    again = clarify_node({"semantic_recall": _recall(0.9), "clarify_attempt": 2})  # 第三次
    assert again["clarify_options"]["kind"] == "reject"  # 超限拒绝


def test_resolve_choice() -> None:  # 选择可接住
    options = ClarifyOptions(kind="candidates", options=(Candidate(id="time", text="查时间"),))  # 选项
    assert resolve_choice("time", options) == "time"  # 按 id
    assert resolve_choice("查时间", options) == "time"  # 按文案
    assert resolve_choice("查天气", options) is None  # 不在选项内
    assert resolve_choice("其他", options) is not None  # 自由输入 → 稳定键