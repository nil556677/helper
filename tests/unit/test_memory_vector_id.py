"""记忆向量点 id 的确定性回归测试（替换进程内 hash() 的修复）。

背景：早期 ``app/memory/vector.py`` 用内置 ``hash()`` 造 Qdrant 点 id —— Python 对 str
的哈希按进程加盐（``PYTHONHASHSEED``），重启后同键落到不同点 id，upsert 变成新增重复点；
``& 0x7FFFFFFF`` 只留 31 位，碰撞还会互相覆盖。现在改用固定命名空间的 ``uuid5``，
跨进程/重启稳定、不碰撞。这里锁死这个约定，防止以后再引入进程内随机。
"""

from __future__ import annotations  # 延迟值注解

import uuid  # 类型断言

from app.memory import vector  # 被测


def test_point_id_deterministic_and_namespaced() -> None:  # 确定性
    """同一 (namespace, memory_key) 永远得到同一个点 id；不同则不同。"""
    a = vector._point_id("n", "k")  # 首次
    assert a == vector._point_id("n", "k")  # 同键同点
    assert isinstance(a, uuid.UUID)  # Qdrant 接受 UUID 点 id
    assert a == uuid.uuid5(vector._POINT_ID_NS, "n:k")  # 命中固定命名空间（跨进程/重启稳定）
    assert a != vector._point_id("n", "k2")  # 不同键 → 不同点
    assert a != vector._point_id("m", "k")  # 不同作用域同键也不串
