"""路径模块：优先级、目录自愈。

这些不是"为了覆盖率"的用例——路径搞错会让数据写到别处（甚至写进只读位置），
而那类问题在界面上只表现为"我的资料不见了"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.core import paths
from app.core.config.schema import BootstrapConfig


def test_project_root_is_repo_root() -> None:
    """开发期：安装目录就是仓库根（从 `app/core/paths.py` 往上三层）。

    **判据要挑不会随目录调整而失效的东西**：以前断言"根下有 `core/`"，把代码收进 `app/` 之后
    就假失败了（路径逻辑没问题，是断言过时）。现在断言仓库根的稳定标志：`pyproject.toml` +
    `app/` + `tests/`。

    这里**用模块自身的位置算**而不是调 `paths.project_root()`：界面测试的会话装置会把那个函数
    临时换到 `.tmp/ui-test-root`（pytest 先跑 `tests/ui`），直接调会拿到被换过的值。
    """
    root = Path(paths.__file__).resolve().parents[2]
    assert (root / "pyproject.toml").exists()
    assert (root / "app").is_dir()
    assert (root / "tests").is_dir()


def test_data_dir_is_fixed_under_the_install_dir(fake_root: Path) -> None:
    """数据目录**写死**：恒为安装目录下的 data/（配置与环境变量都不能再改道）。"""
    assert paths.data_dir() == fake_root / "data"
    assert not hasattr(paths, "ENV_DATA_DIR"), "环境变量覆盖已经去掉"
    assert "data_dir" not in BootstrapConfig.model_fields, "配置里不该再有 data_dir"


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
