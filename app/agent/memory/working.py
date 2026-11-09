"""工作记忆：Task-level 结构化白板（Checkpointer 内），摘要零触碰。

- 它是"当前运行的工作集"，不是聊天记录；`key_facts` 是任务正确性锚点，永不压缩。
- 任务结束用 `promote` 取结论候选，再清空白板。
- 字段全靠校验/构造保证形状，避免运行时把任意 dict 塞进来。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any, TypedDict  # 类型

__all__ = ["WorkingMemory", "blank", "promote", "record_step", "set_goal", "touch", "update"]  # 导出


class WorkingMemory(TypedDict, total=False):  # 白板形状
    task_goal: str  # 当前任务目标
    current_step: str  # 当前步
    past_steps: list[tuple[str, str]]  # (步名, 结果摘要) 已完成
    pending_actions: list[str]  # 待办
    key_facts: dict[str, Any]  # ID/金额/状态码/原始数值（锚点，永不压缩）


def blank() -> WorkingMemory:  # 空白板
    """新任务起点。"""
    return {  # 构造
        "task_goal": "",  # 无目标
        "current_step": "",  # 无当前步
        "past_steps": [],  # 无历史步
        "pending_actions": [],  # 无待办
        "key_facts": {},  # 无关键事实
    }  # 结束


def set_goal(white: WorkingMemory, goal: str) -> WorkingMemory:  # 设目标（不覆盖已有）
    """任务目标为空或变化时写入，保留已有锚点。"""
    out = dict(white or {})  # 拷贝
    out["task_goal"] = str(goal or "")  # 写目标
    return out  # 返回


def record_step(white: WorkingMemory, name: str, result: str = "") -> WorkingMemory:  # 记录完成一步
    """把当前步归档进 past_steps 并清 current_step。"""
    out = dict(white or {})  # 拷贝
    past = list(out.get("past_steps") or [])  # 现有
    step = str(out.get("current_step") or name or "")  # 步名
    if step:  # 非空
        past.append((step, str(result or "")))  # 归档
    out["past_steps"] = past  # 写回
    out["current_step"] = ""  # 清当前步
    return out  # 返回


def update(white: WorkingMemory, *, current_step: str | None = None, pending: list[str] | None = None) -> WorkingMemory:  # 局部更新
    """只覆盖传入的键，不整块替换。"""
    out = dict(white or {})  # 拷贝
    if current_step is not None:  # 给了当前步
        out["current_step"] = str(current_step)  # 写
    if pending is not None:  # 给了待办
        out["pending_actions"] = list(pending)  # 写
    return out  # 返回


def touch(white: WorkingMemory, key_facts: dict[str, Any]) -> WorkingMemory:  # 合并关键事实（逐键覆盖，不整体替换）
    """`key_facts` 只追加或覆盖单个键，绝不全量替换。"""
    out = dict(white or {})  # 拷贝
    facts = dict(out.get("key_facts") or {})  # 现有
    for key, value in (key_facts or {}).items():  # 逐键
        facts[str(key)] = value  # 覆盖/新增
    out["key_facts"] = facts  # 写回
    return out  # 返回


def promote(white: WorkingMemory) -> tuple[list[tuple[str, str]], dict[str, Any]]:  # 收尾取结论候选
    """返回 (过去步骤, key_facts) 供提升为 episode / 长期记忆；随后该清空白板。"""
    base = blank() if not white else dict(white)  # 兜底
    return list(base.get("past_steps") or []), dict(base.get("key_facts") or {})  # 结论候选