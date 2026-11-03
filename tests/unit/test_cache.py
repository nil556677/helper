"""TtlCache 单元测试：逐条 ttl、容量上限、命中深拷贝、缺省窗口。"""

from __future__ import annotations  # 延迟求值注解

import time  # 睡眠等待过期

from app.agent.tools.cache import TtlCache  # 被测缓存


def test_default_ttl_hit_and_copy() -> None:  # 缺省窗口命中 + 深拷贝
    c = TtlCache(ttl=1e9)  # 极长窗口
    c.set(("k",), {"a": 1})  # 存
    v = c.get(("k",))  # 命中
    assert v == {"a": 1}  # 值对
    v["a"] = 999  # 改外层
    assert c.get(("k",))["a"] == 1  # 内部不受影响


def test_per_entry_ttl_overrides() -> None:  # 逐条 ttl 覆盖默认
    c = TtlCache(ttl=1e9)  # 默认极长
    c.set(("a",), 1, ttl=0.02)  # 这条 20ms 过期
    c.set(("b",), 2)  # 这条走默认（长）
    assert c.get(("a",)) == 1  # 未过期
    time.sleep(0.03)  # 等 a 过期
    assert c.get(("a",)) is None  # a 已过期
    assert c.get(("b",)) == 2  # b 仍在


def test_max_entries_evicts() -> None:  # 容量上限淘汰
    c = TtlCache(ttl=1e9, max_entries=3)  # 只留 3
    for i in range(5):  # 塞 5 条
        c.set((i,), i)  # 存
    assert len(c._store) <= 3  # 未超上限


def test_clear() -> None:  # 清空
    c = TtlCache(ttl=1e9)  # 长窗口
    c.set(("x",), 1)  # 存
    c.clear()  # 清
    assert c.get(("x",)) is None  # 不再命中