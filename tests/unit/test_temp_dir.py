"""临时目录必须落在数据目录里 —— 不然它会退回**当前工作目录**（本机就是仓库根）。

这条不是"为了覆盖率"：本机实测 `tempfile.gettempdir()` 返回的就是仓库根（TEMP/TMPDIR 都不可用），
于是所有用 tempfile 的库都往仓库根里丢东西：

- NiceGUI 测试装置的 `nicegui-test-storage-*` 空目录（出现过 9 个，而且因为工作区根上有系统的
  删除保护，事后连删都删不掉）；
- jieba 的前缀词典缓存 `jieba.cache`（8.8 MB）。

`app/bootstrap.py` 的 `pin_temp_dir()` 把它们统一钉进 `data/tmp/`；这条用例守住它。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.bootstrap import ensure_ready, pin_temp_dir
from app.core import paths


def test_temp_dir_is_pinned_inside_data_dir(fake_root: Path) -> None:
    """`ensure_ready()` 之后，进程的临时目录必须是数据目录下的 `tmp/`。"""
    ensure_ready()
    pinned = pin_temp_dir()

    assert Path(tempfile.gettempdir()) == pinned, "临时目录没被钉住"
    assert pinned.parent == paths.data_dir(), "临时目录不在数据目录下"
    assert pinned.name == "tmp"


def test_tempfile_creates_things_inside_the_workspace(fake_root: Path) -> None:
    """真建一个临时目录，确认它落在数据目录里（而不是仓库根）。"""
    ensure_ready()
    made = Path(tempfile.mkdtemp(prefix="probe-"))
    try:
        assert made.parent == pin_temp_dir()
        assert paths.project_root() in made.parents, "临时目录跑到工作区外面了"
    finally:
        # mkdtemp 建的是 0o700 目录：受限令牌下列不出也删不掉（conftest 记的同款
        # 环境事实）。删除是清理不是断言——失败不该让用例红（内容断言已经过了）。
        import contextlib

        with contextlib.suppress(OSError):
            made.rmdir()
