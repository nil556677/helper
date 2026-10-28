"""`app.core.paths.unique_path`：导出不覆盖同名文件。"""

from __future__ import annotations

from pathlib import Path

from app.core.paths import unique_path


def test_unique_path_keeps_free_name(tmp_path: Path) -> None:
    """目标不存在 → 原样返回。"""
    target = tmp_path / "导出.zip"  # 目标
    assert unique_path(target) == target  # 原样


def test_unique_path_adds_suffix(tmp_path: Path) -> None:
    """同名已存在 → 加序号，不覆盖。"""
    target = tmp_path / "导出.zip"  # 目标
    target.write_bytes(b"old")  # 已有文件
    assert unique_path(target) == tmp_path / "导出-2.zip"  # 序号 2
    (tmp_path / "导出-2.zip").write_bytes(b"old2")  # 再占一个
    assert unique_path(target) == tmp_path / "导出-3.zip"  # 序号 3
