"""用户记忆域包（S9 落位）：结构化用户画像 + 长期事实的上层访问。

本包是"用户记忆"领域（docs/架构与目录设计.md 预留位）。**域纪律**：
- UI 不得直接 import 本包（须经 `app/services/memory.py` 门面）；
- 其它领域（RAG/工具）不得 import 本包；
- `app/agent/memory/manager.py` 是本域在 agent 侧的唯一收口层（它可跨入本域，依赖方向单向）。

扁平面板（docs/多源检索设计.md §12）：`model`（形状）/ `store`（持久化）/
`render`（渲染成上下文块）。向量（P3）新增 `vector` 并启用 `FINGERPRINT_KEY`。
"""

from __future__ import annotations  # 延迟值注解

from app.memory.model import ProfileEntry, default_namespace  # 数据模型
from app.memory.render import render_profile_block  # 渲染
from app.memory.store import ProfileStore  # 存储

__all__ = [  # 导出（字典序）
    "ProfileEntry",  # 画像条目
    "ProfileStore",  # 画像仓储
    "default_namespace",  # 默认作用域
    "render_profile_block",  # 渲染画像块
]  # 结束
