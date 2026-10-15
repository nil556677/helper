"""设置相关的编排：汇总数据概览、读版本、打开目录。

界面只认这里返回的普通数据（dict / 字符串），不碰 core 内部细节（技术方案 §5.1 红线 2）。

**这里没有日志级别的读写**：那个旋钮只由 `config.json` 给，界面不提供（§10.4 第 12 条），
所以 2026-10 把 `log_level()` / `save_log_level()` 一并删了（不留死代码）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

from app.core import paths
from app.core.db import schema
from app.core.db.connection import get_connection
from app.core.logging import daily_log_file


def data_overview() -> dict[str, Any]:
    """数据概览：路径 + 数据结构版本（设置页「通用」分区用）。

    返回：
        ``{"paths": {名称: 路径}, "schema_version": int}``。

    注意这里**不再统计资料份数与片段数**：那属于「知识库」分区，等它落地时自己查
    （现在没有资料库，摆一个恒为 0 的计数只会让人以为"这功能已经有了"）。
    """
    return {
        "paths": {
            "安装目录": str(paths.project_root()),
            "数据目录": str(paths.data_dir()),
            "数据库": str(paths.db_file()),
            "日志文件": str(daily_log_file()),
            "资料副本": str(paths.files_dir()),
        },
        "schema_version": schema.schema_version(get_connection()),
    }


def app_version() -> str:
    """从 pyproject.toml 读版本号（不在代码里再写一份，避免两处漂移）。"""
    try:
        import tomllib

        with (paths.project_root() / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, ValueError) as exc:
        logger.debug("读版本号失败：{}", exc)
        return "未知"


def open_folder(target: str) -> bool:
    """在文件资源管理器里打开目录（传文件路径就打开它所在目录）。

    参数：
        target: 目录或文件路径。
    返回：打开成功为 True。**失败必须让调用方知道**——界面据此提示用户，
    否则就成了"点了按钮什么都没发生"。
    """
    folder = target if Path(target).is_dir() else str(Path(target).parent)
    try:
        os.startfile(folder)  # type: ignore[attr-defined]  # Windows 专有
    except OSError as exc:
        logger.warning("打开目录失败（{}）：{}", folder, exc)
        return False
    return True
