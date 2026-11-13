"""预算与阈值：窗口写死 1M，纯函数，便于单测。

公式来源：DSH 的 compaction 预算（阈值为 `min(W×0.8, W−O−B)`），
`retain` 额外收口到 `0.8×history_budget`，避免“压缩完仍超软预算”导致抖动。
"""

from __future__ import annotations  # 类型注解延迟求值

from dataclasses import dataclass, field  # 冻结配置

#: 写死的上下文窗口（用户决定：不探测、不加 DB 字段；后续多模型再改）。
WINDOW = 1_000_000

#: 余量上下限。
_HEADROOM_MIN = 4096  # 最小余量
_HEADROOM_MAX = 65536  # 最大余量


@dataclass(frozen=True)
class Budget:  # 预算参数
    """window 固定（当前 1M）；各档位按比例推导，不写死历史量。"""

    window: int = WINDOW  # 上下文窗口（固定，当前决策 1M）
    output_reserve: int = 2048  # 输出预留（槽位 max_tokens）
    history_ratio: float = 0.12  # 会话历史占窗口的比例（触发预算推导用）
    keep_messages: int = 20  # 压缩后保留的最近原始消息条数
    min_turns: int = 4  # 滞回：距上次压缩至少几轮
    max_compactions: int = 20  # 单会话压缩次数上限
    _history_budget: int | None = field(default=None, repr=False)  # 显式覆盖（默认按比例推导）

    @property
    def history_budget(self) -> int:  # 会话历史预算（触发阈值）
        """显式覆盖优先；否则由 window×比例推导（不写死）。"""
        if self._history_budget is not None:  # 显式给
            return int(self._history_budget)  # 用显式
        return max(1, int(self.window * self.history_ratio))  # 按比例

    @property
    def headroom(self) -> int:  # 额外余量
        """按窗口 1/8，限制在 4096~65536。"""
        return min(_HEADROOM_MAX, max(_HEADROOM_MIN, self.window // 8))  # 夹

    @property
    def retain(self) -> int:  # 压缩后逐字保留
        """不超过软预算的 80%，也不超过窗口的 16%。"""
        by_budget = int(self.history_budget * 0.8)  # 软预算口径
        by_window = int((self.window - self.output_reserve) * 0.16)  # 窗口口径
        return min(by_budget, by_window)  # 取小

    @property
    def trim_target(self) -> int:  # 裁剪目标
        """一次裁到软预算 80%，留 20% 余量。"""
        return int(self.history_budget * 0.8)  # 比例

    def should_compact(self, history_tokens: int, *, turns_since_last: int = 0, compactions: int = 0) -> bool:  # 软触发判定
        """历史超预算 + 过滞回 + 未超次数上限。"""
        if compactions >= self.max_compactions:  # 次数用尽
            return False  # 不压
        if turns_since_last < self.min_turns:  # 还没过滞回
            return False  # 不压
        return history_tokens > self.history_budget  # 超预算才压


__all__ = ["WINDOW", "Budget"]  # 对外导出
