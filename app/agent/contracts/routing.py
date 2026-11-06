"""意图路由契约：语义召回 / 澄清 / 专家描述符 / HITL 负载。

单一来源：
- `RouteDecision` / `Candidate`（澄清候选）复用 `contracts/output.py`，本文件**不重复定义**；
- `AGENT_KINDS` 是专家种类唯一枚举源，`EXPERTS` 与 `PlanStep.agent` 都围绕它校验；
- 本文件只放**可序列化契约**，不放执行逻辑。
"""

from __future__ import annotations  # 延迟求值注解

from dataclasses import dataclass  # 描述符
from typing import Literal  # 类型

from pydantic import BaseModel, Field  # 校验

from app.agent.contracts.output import Candidate  # 澄清候选（复用单一来源）

#: 专家种类：唯一枚举源（EXPERTS 与 PlanStep.agent 都围绕它校验）。
AGENT_KINDS: tuple[str, ...] = (  # 专家种类（按功能点，单一枚举源）
    "knowledge_agent",  # 多功能 RAG（检索/资料问答）
    "general_agent",  # 纯 LLM 兜底（能力外引导）
    "unsupported",  # 无专家可做（显式出口，不静默兜底）
)

#: 副作用等级。
SIDE_EFFECTS: tuple[str, ...] = ("none", "reversible", "irreversible")  # 副作用等级


class RouteCandidate(BaseModel):  # 语义候选（route + 分数）
    """与澄清候选 `Candidate`(id/text) 是不同概念，故单独命名。"""

    route: str = Field(default="", description="固定路径 id")  # 路径
    score: float = Field(default=0.0, description="余弦相似度")  # 分数


class SemanticRecall(BaseModel):  # 语义层输出
    """只召回 + 置信，不拍板；decision 由语义层内部阈值判定。"""

    candidates: list[RouteCandidate] = Field(default_factory=list, description="候选（按分数降序）")  # 候选
    top1: float = Field(default=0.0, description="最高相似度")  # 最高分
    margin: float = Field(default=0.0, description="top1 − top2")  # 差距
    decision: Literal["direct", "ambiguous", "miss"] = Field(default="miss", description="三态判定")  # 三态


class ClarifyOptions(BaseModel):  # 澄清选项
    """semantic_miss 的三档出口。"""

    kind: Literal["candidates", "open", "reject"] = Field(default="reject", description="三档：候选/开放澄清/拒绝")  # 档位
    options: tuple[Candidate, ...] = Field(default_factory=tuple, description="① 展示的候选（② 用业务预设）")  # 候选
    clarify_attempt: int = Field(default=0, description="本会话第几次澄清")  # 次数


class InterruptPayload(BaseModel):  # HITL 待办负载（后端→前端）
    """前端只渲染，不得修改。"""

    run_id: str = Field(default="", description="运行 id")  # run
    thread_id: str = Field(default="", description="恢复时必须原样带回")  # 线程
    node: str = Field(default="", description="哪个节点暂停")  # 节点
    kind: Literal["approve", "choose", "input"] = Field(default="approve", description="需要用户做什么")  # 类型
    title: str = Field(default="", description="标题")  # 标题
    description: str = Field(default="", description="一句话说明")  # 说明
    options: list[dict] = Field(default_factory=list, description="[{value,label}]")  # 选项
    risk: Literal["low", "medium", "high"] = Field(default="low", description="风险分级")  # 风险
    expires_at: float | None = Field(default=None, description="过期时间戳")  # 过期


class ResumeCommand(BaseModel):  # HITL 恢复命令（前端→后端）
    """同 thread_id 恢复；只允许批准/拒绝/选择/输入。"""

    thread_id: str = Field(default="", description="与中断时同一 thread id")  # 线程
    decision: Literal["approve", "reject", "choose", "input"] = Field(default="approve", description="用户决策")  # 决策
    option_value: str | None = Field(default=None, description="choose 时的取值")  # 取值
    comment: str = Field(default="", description="备注")  # 备注


@dataclass(frozen=True)
class AgentDescriptor:  # 专家代理描述符（自动发现用）
    """每个专家模块必须暴露 `DESCRIPTOR`（本对象）+ `build(model, tools)`。"""

    name: str  # 必须 ∈ AGENT_KINDS 且唯一
    description: str = ""  # 能力说明
    tools: tuple[str, ...] = ()  # 工具名（≤10，且必须已注册）
    side_effect: str = "none"  # none / reversible / irreversible
    prompt: str = ""  # 系统提示
    max_tools: int = 10  # 工具上限


@dataclass(frozen=True)
class RuleHit:  # 规则命中结果
    """规则层唯一出口：命中即给出决策，否则 None。"""

    rule_id: str  # 规则 id
    route: str  # 固定路径名
    tools: tuple[str, ...] = ()  # 工具
    exact: bool = True  # 是否明确无歧义
    reasons: tuple[str, ...] = ()  # 命中依据


@dataclass(frozen=True)
class RouteSpec:  # 固定路径定义（routes/*.yaml）
    """语义层 embedding 的源文本 + 分流目标。"""

    route_id: str  # 路径 id
    label: str = ""  # 人话标签（澄清卡展示用；空则回退 id）
    description: str = ""  # 主描述
    examples: tuple[str, ...] = ()  # 示例
    tools: tuple[str, ...] = ()  # 工具
    node: str = "dispatch_fixed"  # 分流节点
    required_params: tuple[dict, ...] = ()  # 必需参数（name/extract_regex/question/options）


__all__ = [  # 对外导出
    "AGENT_KINDS",  # 专家种类
    "SIDE_EFFECTS",  # 副作用等级
    "AgentDescriptor",  # 专家描述符
    "ClarifyOptions",  # 澄清
    "InterruptPayload",  # HITL 负载
    "ResumeCommand",  # HITL 恢复
    "RouteCandidate",  # 语义候选
    "RouteSpec",  # 固定路径
    "RuleHit",  # 规则命中
    "SemanticRecall",  # 语义召回
]
