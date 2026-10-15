"""T2.1 单实例守卫。

真取互斥体会跨进程 / 跨测试搞出鬼，所以这里测三件**纯逻辑 / 可替换**的：
1. `instance_key` 的派生态（同目录同 key，大小写不敏感）；
2. `acquire` 在 API 不可用（没有 ctypes.windll）时返回 True 并放行——守卫生效的前提；
3. 二次 acquire 幂等（本进程取过就不再开）。
真机验收（两个 `python -m app`）放在文档验收命令里，不在单测里模拟。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import single_instance
from app.core.single_instance import MUTEX_PREFIX, acquire, instance_key


def test_instance_key_derives_from_root() -> None:
    a = instance_key(Path("D:/Helper"))
    b = instance_key(Path("D:\\helper"))  # 大小写/分隔符不同，key 应一致
    assert a == b
    assert a.startswith(MUTEX_PREFIX)


def test_instance_key_differs_across_checkouts() -> None:
    assert instance_key(Path("C:/a/helper")) != instance_key(Path("C:/b/helper"))


def test_acquire_returns_true_and_skips_when_api_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 不可用（非 Windows / 缺 ctypes.windll）→ 返回 True，绝不挡启动。"""
    monkeypatch.delattr(ctypes_module(), "windll", raising=False)
    monkeypatch.setattr(single_instance, "_mutex_handle", None)
    assert acquire(Path("D:/x")) is True


def test_acquire_is_idempotent_within_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """本进程已经取过 → 再 acquire 直接 True（不重复开句柄）。"""
    monkeypatch.setattr(single_instance, "_mutex_handle", object())
    assert acquire(Path("D:/y")) is True


def ctypes_module():
    import ctypes

    return ctypes
