"""路径模块：优先级、目录自愈。

这些不是"为了覆盖率"的用例——路径搞错会让数据写到别处（甚至写进只读位置），
而那类问题在界面上只表现为"我的资料不见了"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core import paths


def test_project_root_is_repo_root() -> None:
    """开发期：安装目录就是仓库根。

    **判据要挑不会随目录调整而失效的东西**：以前这里断言"根下有 `core/`"，
    把代码收进 `app/` 之后这条就假失败了（路径逻辑没问题，是断言过时）。
    现在断言的是仓库根的稳定标志：`pyproject.toml` + `app/` + `tests/`。
    """
    root = paths.project_root()
    assert (root / "pyproject.toml").exists()
    assert (root / "app").is_dir()
    assert (root / "tests").is_dir()


def test_env_var_wins(fake_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先级最高（测试隔离与多实例就靠它）。"""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(elsewhere))
    assert paths.data_dir() == elsewhere.resolve()


def test_config_override_used_when_env_absent(fake_root: Path) -> None:
    """环境变量没设时，用引导配置里的 data_dir。"""
    target = fake_root / "mydata"
    (fake_root / "config.json").write_text(
        '{"log_level": "DEBUG", "data_dir": ' + f'"{target.as_posix()}"' + "}",
        encoding="utf-8",
    )
    from app.core.config import access

    access.invalidate()
    assert paths.data_dir() == target.resolve()


def test_default_data_dir_is_under_root(fake_root: Path) -> None:
    """两处都没设时，落在安装目录下的 data/。"""
    assert paths.data_dir() == fake_root / "data"


def test_ensure_dirs_recreates_missing(fake_root: Path) -> None:
    """目录被删掉后要能补回来（缓存不能退化成"只建一次"）。"""
    paths.ensure_dirs()
    data = paths.data_dir()
    assert data.is_dir() and paths.logs_dir().is_dir()

    shutil.rmtree(data)
    assert not data.is_dir()

    paths.ensure_dirs()
    assert data.is_dir(), "运行期被删的目录必须能自愈"


def test_ensure_dirs_does_not_pre_create_reserved_dirs(fake_root: Path) -> None:
    """预留目录**不预建**（方案 §6.2）：空目录进不了版本控制，还会被误读成"已经有这个能力"。

    资料库（`data/files/`）与本地模型（`models/`）都还没落地，所以启动时不该创建它们。
    """
    paths.ensure_dirs()

    assert not paths.files_dir().is_dir(), "资料副本目录等资料库落地时再建"
    assert not paths.models_dir().is_dir(), "模型目录等 P2 落地时再建"
    # 但它们的**路径**必须能报出来（启动日志与排查要用）
    assert "资料副本" in paths.describe()
