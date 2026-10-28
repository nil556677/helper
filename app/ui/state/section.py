"""设置页当前分区（轻量状态，给"只在可见时才干活"的逻辑用）。

**为什么需要**：设置页把所有分区都建好、用 `set_visibility` 切显示。这样切页很快，但
**隐藏分区里的定时器仍在跑**（例如「通用」每 3 秒探一次 Qdrant HTTP）。把"当前分区"
记在这里，轮询类回调先判断自己在不在当前分区，不在就直接返回。

单机单窗口，一个模块级字典够用；多窗口再换成 client 级存储。
"""

from __future__ import annotations

#: 当前分区 key（设置页 `select()` 时写入；空 = 还没进过设置页）。
_ACTIVE: dict[str, str] = {"key": ""}


def set_active(key: str) -> None:
    """切分区时写入当前 key。"""
    _ACTIVE["key"] = key  # 记录


def active() -> str:
    """读当前分区 key（空 = 还没进设置页）。"""
    return _ACTIVE["key"]  # 返回


def is_active(key: str) -> bool:
    """某个分区是否是当前可见分区。"""
    return _ACTIVE["key"] == key  # 比较
