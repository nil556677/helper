"""每计划独立预算：一次 plan+execute 运行各自新建，不共享全局/会话计数。

设计见《执行分层方案》§12：每次 `plan_execute` 开头 `PlanBudget()` 新建（可带复杂度档位参数），
跑完即弃；Execute 边界扣减、任一耗尽即 `exceeded()` 返回终止 kind，不静默继续。
"""

from __future__ import annotations  # 延迟求值注解

from dataclasses import dataclass, field  # 数据类
from typing import Any  # 任意值

__all__ = ["PlanBudget"]  # 导出


@dataclass
class PlanBudget:  # 独立预算（可变）
    """每个计划实例的限量 + 已用量；`exceeded()` 给出超出种类或 None。"""

    max_steps: int = 7  # 可执行步数上限
    max_replans: int = 2  # 重规划次数上限
    max_tokens: int = 4000  # 整个 plan 模型 token 上限
    max_tool_calls: int = 12  # 全部工具调用次数上限
    step_timeout_s: float = 60.0  # 单步墙钟上限（秒）
    total_timeout_s: float = 300.0  # 整个 plan 墙钟上限（秒）
    max_cost: float | None = None  # 可选代价上限（换算单位）

    # 运行期计数（每次实例独立，非共享）
    used_steps: int = 0  # 已执行步数
    used_replans: int = 0  # 已重规划次数
    used_tokens: int = 0  # 已用 token
    used_tool_calls: int = 0  # 已用工具调用
    started_at: float = field(default_factory=lambda: _now())  # 开始墙钟
    used_cost: float = 0.0  # 已用代价

    def consume_tokens(self, n: int) -> None:  # 记 token 用量
        self.used_tokens += max(0, int(n))  # 累加

    def consume_tool(self, count: int = 1) -> None:  # 记工具调用
        self.used_tool_calls += max(0, int(count))  # 累加

    def consume_cost(self, c: float) -> None:  # 记代价
        self.used_cost += max(0.0, float(c))  # 累加

    def mark_step(self) -> None:  # 完成一步
        self.used_steps += 1  # 累加

    def mark_replan(self) -> None:  # 完成一轮重规划
        self.used_replans += 1  # 累加

    def exceeded(self) -> str | None:  # 返回首个超限种类（None=未超）
        """顺序优先级：步数/重规划/token/工具/墙钟/代价。"""
        if self.used_steps >= self.max_steps:  # 步数超
            return "budget_steps"  # 终止
        if self.used_replans >= self.max_replans:  # 重规划超
            return "budget_replans"  # 终止
        if self.used_tokens >= self.max_tokens:  # token 超
            return "budget_tokens"  # 终止
        if self.used_tool_calls >= self.max_tool_calls:  # 工具超
            return "budget_tools"  # 终止
        if _now() - self.started_at >= self.total_timeout_s:  # 总墙钟超
            return "budget_time"  # 终止
        if self.max_cost is not None and self.used_cost >= self.max_cost:  # 代价超
            return "budget_cost"  # 终止
        return None  # 未超

    def step_timed_out(self) -> bool:  # 单步是否超时（看门狗）
        """由外部传入最近一步起始时间判断；这里保留占位便于单测可注入时间。"""
        return False  # 外部驱动

    def snapshot(self) -> dict[str, Any]:  # 序列化快照（trace/观测）
        return {  # 快照
            "used_steps": self.used_steps,  # 步
            "used_replans": self.used_replans,  # 重规划
            "used_tokens": self.used_tokens,  # token
            "used_tool_calls": self.used_tool_calls,  # 工具
            "used_cost": self.used_cost,  # 代价
            "limits": {  # 上限
                "max_steps": self.max_steps,  # 步
                "max_replans": self.max_replans,  # 重规划
                "max_tokens": self.max_tokens,  # token
                "max_tool_calls": self.max_tool_calls,  # 工具
                "total_timeout_s": self.total_timeout_s,  # 墙钟
            },  # 结束
        }  # 返回


def _now() -> float:  # 当前墙钟
    import time  # 时间

    return time.monotonic()  # 单调时钟
