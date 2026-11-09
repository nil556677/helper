"""审批模块：落表 + 幂等 + 乐观决策 + 超时。（REST/UI 接入后置）"""

from __future__ import annotations  # 延迟值注解

from app.agent.approval import repository  # 仓储

__all__ = ["repository"]  # 导出