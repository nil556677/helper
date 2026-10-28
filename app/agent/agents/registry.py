"""多 Agent 注册表：内存 `AgentCard`，不建表。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contracts import AgentCard  # 能力卡

__all__ = ["AgentRegistry", "DuplicateAgentError"]  # 对外导出


class DuplicateAgentError(ValueError):  # 重复注册
    """同一 agent_id 重复注册。"""


class AgentRegistry:  # 内存注册表
    """唯一 agent_id 来源；按能力/工具查询。"""

    def __init__(self) -> None:  # 构造
        self._cards: dict[str, AgentCard] = {}  # agent_id -> 卡

    def register(self, card: AgentCard) -> None:  # 注册
        """重复 agent_id 直接失败（构图期定位）。"""
        if card.agent_id in self._cards:  # 已存在
            raise DuplicateAgentError(f"agent 重复：{card.agent_id}")  # 拒绝
        self._cards[card.agent_id] = card  # 登记

    def get(self, agent_id: str) -> AgentCard:  # 取卡
        """不存在直接 KeyError。"""
        return self._cards[agent_id]  # 取

    def has(self, agent_id: str) -> bool:  # 是否存在
        return agent_id in self._cards  # 判断

    def cards(self) -> list[AgentCard]:  # 全部卡
        """按注册顺序。"""
        return list(self._cards.values())  # 列表

    def by_capability(self, capability: str) -> list[AgentCard]:  # 按能力找
        """返回声明了该能力的 agent。"""
        return [card for card in self._cards.values() if capability in card.capabilities]  # 过滤
