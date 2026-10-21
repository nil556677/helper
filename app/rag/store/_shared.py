"""跨子模块共享的私有实现：哨兵与序列化（移除 SQLite chunks 后大幅瘦身）。

放这里是因为 ``documents`` 依赖它；单独拎出来避免子模块循环 import。
"""

from __future__ import annotations

#: 哨兵：表示"这个列别动"（区分 ``None``＝清空）
_KEEP = object()
