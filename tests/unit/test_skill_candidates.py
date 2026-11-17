"""Skill 沉淀流水线：从成功运行提炼草案 → 候选区 → 校验 Gate → 决策 → 入库。"""

from __future__ import annotations  # 延迟值注解

from pathlib import Path  # 路径

from app.agent.skills import candidates  # 候选区
from app.agent.skills.condenser import draft_from_run, propose_from_run  # 提炼
from app.agent.skills.registry import discover  # 发现


def _done_state() -> dict:  # 一次成功完整的 plan_execute 运行
    return {  # 状态
        "question": "查订单 12345 和 67890 的物流",  # 问题
        "resolved_query": "查订单 12345 和 67890 的物流",  # 消解后
        "completion": {"all_done": True, "finalized": True},  # 完成
        "last_plan": [  # 计划
            {"step": "查订单 12345", "agent": "knowledge_agent", "status": "done"},  # 步1
            {"step": "查订单 67890", "agent": "knowledge_agent", "status": "done"},  # 步2
        ],  # 结束
        "focus_stack": {"groups": [{"group_id": "g1", "items": [{"entity_id": "12345"}]}]},  # 焦点
    }  # 结束


def test_draft_skips_incomplete() -> None:  # 未完成不沉淀
    assert draft_from_run({"completion": {"all_done": False}, "last_plan": [{"step": "x"}]}) is None  # None
    assert draft_from_run({"completion": {"all_done": True}, "last_plan": []}) is None  # 无计划 None


def test_draft_from_done_run() -> None:  # 成功运行 → 草案
    draft = draft_from_run(_done_state())  # 草案
    assert draft is not None  # 有
    assert draft.id.startswith("auto_")  # 稳定 id
    assert draft.enabled is False  # 默认关
    assert draft.example_queries  # 有示例
    assert draft.meta.get("playbook")  # 回放台账
    assert "物流" in draft.trigger  # 触发词


def test_propose_records_candidate(rag_db) -> None:  # 提炼并落候选（pending）
    cid = propose_from_run(_done_state(), source="run", run_ids=["r1"])  # 落候选
    assert cid  # 有 id
    rows = candidates.list_candidates("pending")  # 列
    assert any(r["id"] == cid and r["status"] == "pending" for r in rows)  # 在
    row = candidates.get(cid)  # 取
    assert row and row["draft"].get("id", "").startswith("auto_")  # 草案解析


def test_evaluate_gate(rag_db) -> None:  # 校验 Gate：好草案过、坏草案不过
    draft = draft_from_run(_done_state()).model_dump(mode="json")  # 好
    assert candidates.evaluate(draft)["ok"] is True  # 过（无工具引用不报错）
    bad = dict(draft)  # 坏
    bad["tools"] = [{"name": "no_such_tool", "role": "support", "approval": "none"}]  # 不存在工具
    assert candidates.evaluate(bad)["ok"] is False  # 不过


def test_decide_and_materialize(rag_db, tmp_path) -> None:  # 决策 + 入库（写 skill.yaml 可被发现）
    cid = propose_from_run(_done_state())  # 落候选
    candidates.decide(cid, True, note="人工确认")  # 批准
    row = candidates.get(cid)  # 取
    assert row["status"] == "approved"  # 已批准
    path = candidates.materialize(row["draft"], str(tmp_path))  # 入库
    assert Path(path).is_file()  # 文件在
    found = discover(extra=[str(tmp_path)])  # 发现
    assert any(m.id == row["draft"]["id"] for m in found)  # 能被发现


def test_decide_only_pending(rag_db) -> None:  # 只 pending 可决（重复决不改）
    cid = propose_from_run(_done_state())  # 落候选
    candidates.decide(cid, True)  # 第一次
    candidates.decide(cid, False)  # 第二次（应无效）
    row = candidates.get(cid)  # 取
    assert row["status"] == "approved"  # 仍是批准
