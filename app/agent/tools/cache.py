"""TTL 缓存（共享原语）：读型/幂等场景在同一窗口内返回同一份结果。

- 工具：天气（5 分钟）、RAG 检索（10 分钟）；
- 改写：成功 30 天 / 失败 60 秒（**按条目各自 ttl**）。

只缓存**成功/需要缓存的结果**；命中返回深拷贝防外部改动；线程安全、访问即过期清理。
并发"错过即各跑一次"，不保证只跑一次（读型一致性够用）。
"""

from __future__ import annotations  # 延迟求值注解

import copy  # 深拷贝
import threading  # 线程安全
import time  # 单调钟

_DEFAULT_TTL = 300.0  # 默认窗口（秒）


class TtlCache:  # 单调时间 TTL 缓存
    """线程安全、访问即过期清理；命中返回深拷贝；每条目可覆盖 ttl。"""

    def __init__(  # 构造
        self,  # 实例
        ttl: float = _DEFAULT_TTL,  # 默认窗口（秒）
        max_entries: int | None = None,  # 可选容量上限（防内存无限增长）
    ) -> None:
        self._ttl = float(ttl)  # 默认生存时间（秒）
        self._max = int(max_entries) if max_entries is not None else None  # 容量上限
        self._lock = threading.Lock()  # 写锁
        self._store: dict[tuple, tuple[float, object]] = {}  # key -> (过期单调钟, 值)

    def get(self, key: tuple) -> object | None:  # 取（命中深拷贝）
        """命中且未过期返回深拷贝；未命中/过期返回 None。"""
        now = time.monotonic()  # 当前单调钟
        with self._lock:  # 加锁
            item = self._store.get(key)  # 查表
            if item is None:  # 从未放
                return None  # 未命中
            if now > item[0]:  # 已过期
                self._store.pop(key, None)  # 顺手清掉
                return None  # 未命中
            value = item[1]  # 命中值
        return copy.deepcopy(value)  # 深拷贝防外部改动

    def set(  # 存
        self,  # 实例
        key: tuple,  # 键
        value: object,  # 值
        *,
        ttl: float | None = None,  # 本次窗口（缺省用实例默认）
    ) -> None:
        """记录 (过期单调钟, 值)；支持逐条 ttl 与容量上限（超限先清过期再淘汰）。"""
        window = float(ttl) if ttl is not None else self._ttl  # 本次窗口
        with self._lock:  # 加锁
            self._store[key] = (time.monotonic() + window, value)  # 记录过期时刻
            if self._max is not None and len(self._store) > self._max:  # 超容量
                now = time.monotonic()  # 当前
                stale = [k for k, (exp, _v) in self._store.items() if now > exp]  # 已过期
                for k in stale:  # 先清过期
                    self._store.pop(k, None)  # 删
                while len(self._store) > self._max:  # 仍超
                    self._store.pop(next(iter(self._store)), None)  # 淘汰最旧一个

    def clear(self) -> None:  # 清空
        """测试或主动失效用。"""
        with self._lock:  # 加锁
            self._store.clear()  # 清空