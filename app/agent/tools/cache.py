"""工具结果 TTL 缓存：读型工具在窗口内返回同一份结果（窗口内视为幂等）。

- 天气：同 (city, date) 5 分钟内结果一致；
- RAG ：同 (query, top_k) 10 分钟内结果一致。
只缓存**成功返回**：抛异常（业务/网络失败）不落缓存，便于上层重试；命中返回深拷贝，
防外部调用方改坏内部状态。并发做“错过即各跑一次”，不保证只跑一次（结果一致即可）。
"""

from __future__ import annotations  # 延迟求值注解

import copy  # 深拷贝
import threading  # 线程安全
import time  # 单调钟

_DEFAULT_TTL = 300.0  # 默认窗口（秒）


class TtlCache:  # 单调时间 TTL 缓存
    """线程安全、访问即过期清理；命中返回深拷贝。"""

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:  # 构造
        self._ttl = float(ttl)  # 生存时间（秒）
        self._lock = threading.Lock()  # 写锁（get/set/清理互斥）
        self._store: dict[tuple, tuple[float, object]] = {}  # key -> (放入时钟, 值)

    def get(self, key: tuple) -> object | None:  # 取（命中深拷贝）
        """命中且未过期返回深拷贝；未命中/过期返回 None。"""
        now = time.monotonic()  # 当前单调钟
        with self._lock:  # 加锁
            item = self._store.get(key)  # 查表
            if item is None:  # 从未放
                return None  # 未命中
            if now - item[0] > self._ttl:  # 已过期
                self._store.pop(key, None)  # 顺手清掉
                return None  # 未命中
            value = item[1]  # 命中值
        return copy.deepcopy(value)  # 深拷贝防外部改动

    def set(self, key: tuple, value: object) -> None:  # 存
        """记录 (放入时钟, 值)；调用方只应在成功时存。"""
        with self._lock:  # 加锁
            self._store[key] = (time.monotonic(), value)  # 记录

    def clear(self) -> None:  # 清空
        """测试或主动失效用。"""
        with self._lock:  # 加锁
            self._store.clear()  # 清空