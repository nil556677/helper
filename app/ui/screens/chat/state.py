"""聊天页的共享状态与常量（一页一份，不能做成模块级全局）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.contracts import GROUP_ORDER, StageGroup


@dataclass
class _LiveState:  # 一页一份的实时状态
    """挂在每次 build() 上，**不能**做成模块级全局：两个窗口会互相覆盖。"""

    views: list[dict[str, Any]] = field(default_factory=list)  # 正在跑的实时视图
    active: dict[str, dict[str, Any]] = field(default_factory=dict)  # 会话 id -> 在跑的任务
    sync_composer: Callable[[], None] | None = None  # 切会话时同步发送/停止按钮


@dataclass
class _ActiveStream:  # 当前正在跑的流（单 worker，全局只有一个）
    """句柄 + 它属于哪个会话；两个字段一起设、一起清，避免漂移。"""

    handle: Any = None  # 运行会话（RunSession / StreamHandle）
    conversation_id: str = ""  # 所属会话

    def is_running(self) -> bool:  # 有没有在跑
        """只看句柄在不在。"""
        return self.handle is not None  # 判断

    def belongs_to(self, conversation_id: str) -> bool:  # 是不是这个会话在跑
        """在跑且会话匹配。"""
        return self.handle is not None and self.conversation_id == conversation_id  # 判断

    def start(self, handle: Any, conversation_id: str) -> None:  # 开始
        """句柄与会话一起记。"""
        self.handle = handle  # 记句柄
        self.conversation_id = conversation_id  # 记会话

    def clear(self) -> None:  # 结束
        """句柄与会话一起清（不会只剩一个）。"""
        self.handle = None  # 清句柄
        self.conversation_id = ""  # 清会话


_TIER_LABELS: dict[str, str] = {
    "fixed": "固定",  # S1/S2/S3
    "fixed_change": "固定变化",  # F1/F2/F3
    "dynamic": "动态",  # V1/V2/V3
}


_RENDER_INTERVAL_S = 0.10


_HISTORY_PREVIEW_CHARS = 600


_FIRST_EVENT_S = 8


_RUN_CAP_S = 10 * 60


_PAGE_TURNS = 20


def _sync_composer(live_state: _LiveState) -> None:  # 同步发送按钮
    """切会话后按“当前会话有没有在跑”刷新按钮。"""
    if live_state.sync_composer is not None:  # 已经建好 composer
        live_state.sync_composer()  # 调用


def _detach_live_views(live_state: _LiveState) -> None:  # 摘掉实时视图
    """把 box 置空，后续事件只收集数据、不碰 DOM。"""
    for view in list(live_state.views):  # 遍历
        view["box"] = None  # 标记为无 DOM
    live_state.views.clear()  # 清空注册


_GROUP_KEYS = frozenset(group.value for group in GROUP_ORDER)  # 分组白名单


def _alive(view: dict[str, Any]) -> bool:  # 视图还在
    """容器未被删才更新 DOM。"""
    return view["box"] is not None and not view["box"].is_deleted  # 判断


_SCROLL_TASKS: set[asyncio.Task] = set()  # 任务集合


_STEP_ICONS: dict[str, str] = {
    StageGroup.PREPARE.value: 'psychology',  # 理解问题
    StageGroup.SEARCH.value: 'search',  # 查找资料
    StageGroup.FILTER.value: 'sort',  # 筛选重点
    StageGroup.TOOL.value: 'build',  # 使用工具
    StageGroup.GENERATE.value: 'forum',  # 生成回答
    'plan': 'psychology',  # 旧数据
    'read': 'menu_book',  # 旧数据
    'rerank': 'sort',  # 旧数据
    'embed': 'hub',  # 旧数据
    'llm': 'forum',  # 旧数据
}
