"""上下文占用统计容器（ContextUsage）。

历史：本文件曾是"唯一按固定顺序拼上下文"的装配器（`assembler.build` / `ContextAssembler` /
`layers` 包），它们没有生产调用方，已在 T0.4 删除；装配统一走
`app.agent.contexts.builder.build_context`。本文件只保留被 `fallback/agent.py`、
`persist.py` 与事件层引用的 `ContextUsage` 数据类。
"""

from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

__all__ = ["ContextUsage"]  # 对外导出


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
    pruned: dict[str, int] = field(default_factory=dict)  # 各层剪枝计数
    dropped: dict[str, int] = field(default_factory=dict)  # 各层预算丢弃计数
    cache_prefix_tokens: int = 0  # 稳定前缀(S) token（Prompt Cache 观测）
    constraints_kept: float = 0.0  # 关键约束保留率（key_facts+hints 落在终上下文比例，P5 指标）

    def to_dict(self) -> dict[str, Any]:  # 序列化
        """给事件/turn_json/落库用（纯 dict）。"""
        from dataclasses import asdict  # 局部导入

        return asdict(self)  # 深拷贝成 dict
