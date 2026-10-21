"""主进程侧的子进程生命周期辅助（P7）。

把"要守护哪些子进程"集中在这里：Qdrant sidecar（若本地安装）与独立 worker。
主进程启动时调 :func:`start_sidecars`，退出时调 :func:`stop_sidecars`。
真正的界面/窗口启动不在本模块（避免 core 依赖界面）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core import paths
from app.core.proc.supervisor import ChildSpec, Supervisor


def qdrant_binary() -> Path:
    """随包分发的 qdrant.exe 路径（``bin/qdrant/qdrant.exe``）。"""
    return paths.project_root() / "bin" / "qdrant" / "qdrant.exe"


def build_specs() -> list[ChildSpec]:
    """组装要守护的子进程声明。

    - Qdrant：仅当随包二进制存在时拉起（否则假设用户连外部服务 / 本地嵌入式）。
    - worker：始终拉起（任务执行独立进程）。
    """
    specs: list[ChildSpec] = []
    qdrant = qdrant_binary()
    if qdrant.is_file():
        specs.append(
            ChildSpec(
                name="qdrant",
                argv=[
                    str(qdrant),
                    "--storage-path",
                    str(paths.data_dir() / "qdrant"),
                ],
                cwd=str(qdrant.parent),
            )
        )
    specs.append(
        ChildSpec(
            name="worker",
            argv=[sys.executable, "-m", "app.worker"],
            cwd=str(paths.project_root()),
        )
    )
    return specs


def start_sidecars() -> Supervisor:
    """拉起并守护子进程（幂等）。"""
    sup = Supervisor(build_specs(), log_dir=paths.data_dir() / "logs")
    sup.start()
    return sup


def stop_sidecars(sup: Supervisor | None, *, grace_s: float = 5.0) -> None:
    """优雅停止（worker 先收尾，qdrant 后备）。"""
    if sup is None:
        return
    sup.stop(grace_s=grace_s)
