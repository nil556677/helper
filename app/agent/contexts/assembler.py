"""装配器：唯一按固定顺序拼上下文的地方，产出 system / messages / 占用统计。

**顺序（定稿）**：

```
[固定]     S1 总规则 → S2 智能体规则 → S3 静态环境
[固定变化] F1 事实档案 → F2 压缩摘要（都进 system 尾部）
           F3 最近 N 轮（messages，追加）
[动态]     V1 工具结果 → V2 用户上传资料 → V3 当前问题（合并成最后一条 user）
```
"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Iterable, Sequence  # 输入类型
from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

from app.agent.contexts import blocks, layers, prompts, tokens  # 层/块/token/模板
from app.agent.contexts.budget import Budget  # 预算
from app.agent.contexts.spec import AgentContextSpec  # 声明

__all__ = ["BuiltContext", "ContextAssembler", "ContextUsage", "build"]  # 对外导出


@dataclass(frozen=True)
class ContextUsage:  # 上下文占用（给事件/UI/落库）
    """字段名与 `ContextPayload` 扩展保持一致。"""

    window: int  # 本轮窗口
    total: int  # 总 token
    tiers: dict[str, int] = field(default_factory=dict)  # 大板块
    blocks: dict[str, int] = field(default_factory=dict)  # 小板块
    subblocks: dict[str, dict[str, int]] = field(default_factory=dict)  # 最细一层
    compacted: int = 0  # 压缩条数
    compacted_saved: int = 0  # 压缩释放
    trimmed: int = 0  # 裁剪条数
    trimmed_saved: int = 0  # 裁剪释放
    prompt_versions: dict[str, int] = field(default_factory=dict)  # 模板版本

    def to_dict(self) -> dict[str, Any]:  # 序列化
        """给事件/turn_json/落库用（纯 dict）。"""
        from dataclasses import asdict  # 局部导入

        return asdict(self)  # 深拷贝成 dict


@dataclass(frozen=True)
class BuiltContext:  # 装配结果
    """直接喂给 `ModelRequest(system=..., messages=...)`。"""

    system: str  # system 文本
    messages: list[dict[str, Any]]  # 消息列表
    usage: ContextUsage  # 占用统计


def _budget_for(spec: AgentContextSpec) -> Budget:  # 取预算
    """spec 给了就覆盖，否则用默认。"""
    if spec.history_budget is None:  # 没覆盖
        return Budget()  # 默认
    return Budget(_history_budget=int(spec.history_budget))  # 覆盖（显式）


def _versions(spec: AgentContextSpec) -> dict[str, int]:  # 模板版本
    """实际用到的模板版本（进 trace/调试）。"""
    result: dict[str, int] = {}  # 结果
    for name in (spec.system_prompt, spec.rules_prompt):  # 用到的两个
        result[name] = prompts.get(name).version  # 取版本
    return result  # 返回


def build(  # 装配一次
    spec: AgentContextSpec,  # agent 声明
    *,
    facts: Iterable[str] = (),  # F1 事实档案
    summary_text: str = "",  # F2 压缩摘要
    history: Sequence[dict[str, Any]] = (),  # F3 最近 N 轮
    tool_results: Iterable[dict[str, Any]] = (),  # V1 工具结果（含检索）
    attachments: Iterable[dict[str, Any]] = (),  # V2 上传资料
    question: str = "",  # V3 当前问题
    budget: Budget | None = None,  # 预算覆盖
) -> BuiltContext:  # 结果
    """只拼装、不裁剪、不压缩（裁剪在 history，压缩在 compact）。"""
    used = budget or _budget_for(spec)  # 生效预算
    s1 = layers.system_base()  # S1
    s2 = layers.agent_rules(spec.rules_prompt)  # S2
    s3 = layers.static_env(spec.static_env)  # S3
    f1 = layers.facts(facts)  # F1
    f2 = layers.summary(summary_text)  # F2
    system = blocks.join_blocks(s1, s2, s3, f1, f2)  # system 尾部放背景类
    f3 = layers.recent(history)  # F3
    v1 = layers.tool_results(tool_results)  # V1
    v2 = layers.attachments(attachments)  # V2
    v3 = layers.question(question)  # V3
    tail = blocks.join_blocks(v1, v2, v3)  # 动态区合并成一条
    messages: list[dict[str, Any]] = list(f3)  # 先历史
    if tail:  # 有动态内容
        messages.append({"role": "user", "content": tail})  # 最后一条 user
    sizes = tokens.count_blocks(  # 文本块统计
        {"S1": s1, "S2": s2, "S3": s3, "F1": f1, "F2": f2, "V1": v1, "V2": v2, "V3": v3}
    )
    sizes["F3"] = tokens.count_messages(f3)  # 历史按消息算
    tiers = {  # 大板块
        "fixed": sizes.get("S1", 0) + sizes.get("S2", 0) + sizes.get("S3", 0),  # 固定
        "fixed_change": sizes.get("F1", 0) + sizes.get("F2", 0) + sizes.get("F3", 0),  # 固定变化
        "dynamic": sizes.get("V1", 0) + sizes.get("V2", 0) + sizes.get("V3", 0),  # 动态
    }
    usage = ContextUsage(  # 占用统计
        window=used.window,  # 窗口
        total=sum(tiers.values()),  # 总量
        tiers=tiers,  # 大板块
        blocks={name: size for name, size in sizes.items() if size},  # 小板块（空块不出现）
        subblocks=_subblocks(f3, tool_results),  # 最细一层
        prompt_versions=_versions(spec),  # 版本
    )
    return BuiltContext(system=system, messages=messages, usage=usage)  # 返回


def _subblocks(history: Sequence[dict[str, Any]], tools: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:  # 最细一层
    """F3 按轮次、V1 按工具名聚合（同工具多次调用合并）。"""
    result: dict[str, dict[str, int]] = {}  # 结果
    if history:  # 有历史
        result["F3"] = {  # 逐条
            f"第{index}条": tokens.count_text(str(item.get("content") or ""))  # 单条
            for index, item in enumerate(history, 1)  # 编号
        }
    by_tool: dict[str, int] = {}  # 工具名 → token
    for item in tools:  # 逐条工具结果
        name = str(item.get("name") or "unknown")  # 工具名
        by_tool[name] = by_tool.get(name, 0) + tokens.count_text(str(item.get("body") or ""))  # 累加
    if by_tool:  # 有工具结果
        result["V1"] = by_tool  # 记
    return result  # 返回


class ContextAssembler:  # 面向调用方的门面
    """目前只是 `build()` 的薄封装；C2 会把它接进节点。"""

    def __init__(self, spec: AgentContextSpec) -> None:  # 构造
        self._spec = spec  # 保存声明

    def build(self, **kwargs: Any) -> BuiltContext:  # 装配
        """参数透传给 `build()`。"""
        return build(self._spec, **kwargs)  # 委托
