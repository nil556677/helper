"""Skill 沉淀 · 提炼：从一次"成功且完整"的运行（plan_execute 的 last_plan/completion）生成候选草案。

原则（方案 §7）：**自动只产候选**，且要求"多轮成功/聚合"再入库；单条运行仅作草案，
工具角色留给评审补；从失败/缺失步导出 `avoid`。
"""

from __future__ import annotations  # 延迟值注解

import hashlib  # 稳定 id
import re  # 关键词
from typing import Any  # 任意值

from app.agent.skills.candidates import record_candidate  # 候选区
from app.agent.skills.models import CostBudget, SkillManifest  # 契约

__all__ = ["draft_from_run", "propose_from_run"]  # 导出

#: 关键词最小长度。
_MIN_WORD = 2  # 字/词
#: 关键词上限。
_MAX_WORDS = 5  # 个


def draft_from_run(state: dict[str, Any]) -> SkillManifest | None:  # 从运行状态提炼草案（不落库）
    """要求 completion.all_done 且 last_plan 非空；否则返回 None（不沉淀未完成的）。"""
    completion = state.get("completion") if isinstance(state.get("completion"), dict) else {}  # 完成块
    if not completion.get("all_done"):  # 未真正完成
        return None  # 不沉淀
    plan = list(state.get("last_plan") or [])  # 计划步骤
    if not plan:  # 无步骤
        return None  # 不沉淀
    question = str(state.get("resolved_query") or state.get("question") or "")  # 问题
    slug = "auto_" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:8]  # 稳定 id
    avoid = [str(s.get("step") or "") for s in plan if str(s.get("status") or "") != "done"]  # 失败步 → 反例
    playbook = [  # 步骤台账（入 meta，供评审/回放）
        {"step": str(s.get("step") or ""), "agent": str(s.get("agent") or ""), "status": str(s.get("status") or "")}  # 一步
        for s in plan  # 逐步骤
    ]  # 结束
    return SkillManifest(  # 草案
        id=slug,  # id
        namespace="auto",  # 命名空间
        version=1,  # 版本
        enabled=False,  # 默认关
        description=f"从成功运行沉淀：{question[:40]}",  # 描述
        tools=[],  # 工具角色留给评审补（单条运行不臆造工具）
        side_effect="none",  # 副作用（保守）
        trigger=_keywords(question),  # 触发词
        avoid=avoid,  # 反例
        example_queries=[question] if question else [],  # 示例
        cost_budget=CostBudget(),  # 预算
        meta={"source": "run", "playbook": playbook, "changelog": "auto candidate"},  # 附加
    )  # 结束


def propose_from_run(state: dict[str, Any], *, source: str = "run", run_ids: list[str] | None = None) -> int | None:  # 提炼并落候选
    """提炼草案并写进 skill_candidates（pending），返回候选 id；不满足沉淀条件返回 None。"""
    draft = draft_from_run(state)  # 草案
    if draft is None:  # 不满足
        return None  # 空
    return record_candidate(draft.model_dump(mode="json"), source=source, run_ids=run_ids)  # 落候选


def _keywords(text: str) -> list[str]:  # 粗关键词（够用即可，非 jieba）
    cleaned = re.sub(r"[的了吗呢和与及、，。,. ]+", " ", str(text or ""))  # 去连接词/标点
    words = re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}", cleaned)  # 中/英词
    seen: list[str] = []  # 去重
    for w in words:  # 逐个
        if w not in seen:  # 新词
            seen.append(w)  # 加
        if len(seen) >= _MAX_WORDS:  # 上限
            break  # 停
    return seen  # 返回
