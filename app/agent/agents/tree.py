"""过程树：把带 agent 维度的 stage 事件聚成父子树（UI/presentation 共用，零业务依赖）。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Sequence  # 输入类型
from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

__all__ = ["AgentNode", "build_agent_tree"]  # 对外导出


@dataclass
class AgentNode:  # 一个 Agent 运行节点
    """key 是子 run 的 branch（并行多路用），parent 指向父 branch。"""

    key: str  # 子 run 标识（branch）
    agent_id: str  # 哪个 agent
    parent_key: str  # 父 branch（空 = 顶层）
    steps: list[dict[str, Any]] = field(default_factory=list)  # 该 agent 的步骤
    children: list[AgentNode] = field(default_factory=list)  # 子 agent


def build_agent_tree(stages: Sequence[dict[str, Any]]) -> list[AgentNode]:  # 建树
    """按 `branch` 分组、按 `parent_run_id` 挂父子；无 agent 维度的事件不参与。"""
    nodes: dict[str, AgentNode] = {}  # key -> 节点
    for stage in stages:  # 逐条步骤
        key = str(stage.get("branch") or "")  # 子 run 标识
        agent_id = str(stage.get("agent_id") or "")  # agent
        if not key or not agent_id:  # 主链路步骤不进树
            continue  # 跳过
        node = nodes.get(key)  # 取节点
        if node is None:  # 首次出现
            node = AgentNode(  # 建节点
                key=key,  # key
                agent_id=agent_id,  # agent
                parent_key=str(stage.get("parent_run_id") or ""),  # 父
            )  # 节点结束
            nodes[key] = node  # 登记
        node.steps.append(dict(stage))  # 收步骤
    roots: list[AgentNode] = []  # 顶层节点
    for node in nodes.values():  # 挂父子
        if node.parent_key and node.parent_key in nodes:  # 有父且父存在
            nodes[node.parent_key].children.append(node)  # 挂到父
        else:  # 顶层
            roots.append(node)  # 收根
    return roots  # 返回
