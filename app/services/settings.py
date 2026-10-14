"""设置相关的编排：读写引导配置、汇总数据概览、打开目录。

界面只认这里返回的普通数据（dict / 字符串），不碰 core 内部细节（技术方案 §5.1 红线 2）。
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from app.core import paths
from app.core.config import access
from app.core.config.schema import BootstrapConfig
from app.core.db import schema
from app.core.db.connection import get_connection
from app.core.logging import LOG_FILENAME

#: 日志级别选项（与 app.core.config.schema 的 Literal 一致）。
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


def log_level() -> str:
    """当前生效的日志级别（来自引导配置 config.json）。"""
    return access.load().log_level


def save_log_level(level: str) -> None:
    """保存日志级别，并让它**立即**生效（不重启）。

    **"已保存"与"已生效"分开对待**：配置写盘失败才算失败（异常抛出去，界面提示"保存失败"）；
    重建日志 sink 失败只告警——配置已经落盘了，报成"保存失败"会让用户反复点。

    参数：
        level: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR`` 之一。
    异常：
        OSError: 配置文件写不进去。
        pydantic.ValidationError: 值非法（界面只从固定选项里选，正常不会发生）。
    """
    current = access.load()
    # 走一次完整校验再落盘：避免"界面传了非法值、直接写进配置文件"。
    access.save(BootstrapConfig(log_level=level, data_dir=current.data_dir))  # type: ignore[arg-type]

    try:
        from app.core.logging import init_logging

        init_logging(level)
    except OSError as exc:  # noqa: BLE001 - 允许失败：配置已落盘，不该报成"保存失败"
        logger.warning("日志级别已保存，但重建日志 sink 失败：{}", exc)
        return
    logger.info("日志级别已切换为 {}", level)


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
            "日志文件": str(paths.logs_dir() / LOG_FILENAME),
            "资料副本": str(paths.files_dir()),
        },
        "schema_version": schema.schema_version(get_connection()),
    }


def app_version() -> str:
    """从 pyproject.toml 读版本号（不在代码里再写一份，避免两处漂移）。"""
    try:
        import tomllib

        with open(paths.project_root() / "pyproject.toml", "rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, ValueError) as exc:  # noqa: BLE001 - 允许失败：读不到就显示未知
        logger.debug("读版本号失败：{}", exc)
        return "未知"


def open_folder(target: str) -> bool:
    """在文件资源管理器里打开目录（传文件路径就打开它所在目录）。

    参数：
        target: 目录或文件路径。
    返回：打开成功为 True。**失败必须让调用方知道**——界面据此提示用户，
    否则就成了"点了按钮什么都没发生"。
    """
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    try:
        os.startfile(folder)  # type: ignore[attr-defined]  # Windows 专有
    except OSError as exc:  # noqa: BLE001 - 允许失败：打不开目录不该影响应用
        logger.warning("打开目录失败（{}）：{}", folder, exc)
        return False
    return True
