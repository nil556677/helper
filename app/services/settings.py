"""设置相关的编排：汇总数据概览、读版本、打开目录、**索引期参数与指纹**。

界面只认这里返回的普通数据（dict / 字符串），不碰 core 内部细节（技术方案 §5.1 红线 2）。

**日志级别不提供界面旋钮**（§10.4 第 12 条），但**调试模式**例外（2026-10 用户要求）：
它只做"开/关"一件事，打开时把文件与控制台都降到 DEBUG、并保留真实控制台输出，
方便错误直接打到控制台看。

**索引期参数**（P0.6）：默认值只在 `app/core/config/params.py` 写一份，这里只做
"读出来给界面画 / 写回去 / 算过不过期"三件事。界面不该自己拼参数名。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from app.core import paths
from app.core.config import store as params_store
from app.core.db import schema
from app.core.db.connection import get_connection
from app.core.logging import daily_log_file


@dataclass(frozen=True)
class PathSpot:
    """折叠区里的一行：中文名 + 路径。"""

    label: str
    path: str


@dataclass(frozen=True)
class DataOverview:
    """数据落在哪 + 数据结构版本（设置页「通用」分区用）。

    **为什么不是一个 ``{"paths": {中文名: 路径}}`` 字典**：那样界面得自己拼中文键
    （``overview["paths"]["日志文件"]``），改个名字不会有任何地方报错。现在中文标签只在这里
    出现一次（``spots``），界面照着画就行。
    """

    data_dir: str
    log_file: str
    schema_version: int
    spots: tuple[PathSpot, ...]


def data_overview() -> DataOverview:
    """数据概览：常用路径 + 结构版本 + 折叠区要显示的路径清单。"""
    return DataOverview(
        data_dir=str(paths.data_dir()),
        log_file=str(daily_log_file()),
        schema_version=schema.schema_version(get_connection()),
        spots=(
            PathSpot("安装目录", str(paths.project_root())),
            PathSpot("数据库", str(paths.db_file())),
            PathSpot("日志文件", str(daily_log_file())),
            PathSpot("资料副本", str(paths.files_dir())),
        ),
    )


def app_version() -> str:
    """从 pyproject.toml 读版本号（不在代码里再写一份，避免两处漂移）。"""
    try:
        import tomllib

        with (paths.project_root() / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, ValueError) as exc:
        logger.debug("读版本号失败：{}", exc)
        return "未知"


def get_log_debug() -> bool:
    """当前是否处于调试模式（读数据库 ``bootstrap.log_debug``）。"""
    from app.core.config import access

    return bool(access.load().log_debug)


def set_log_debug(on: bool) -> None:
    """打开/关闭调试模式：写数据库并**立即重建日志 sink**（不用重启生效）。

    打开后文件与控制台都降到 DEBUG；关闭回到 INFO（不保留之前的 DEBUG 级别，
    否则"关了调试还在刷 DEBUG"）。
    """
    from app.core.config import access
    from app.core.logging import init_logging

    config = access.load()
    config.log_debug = bool(on)
    config.log_level = "DEBUG" if on else "INFO"
    access.save(config)
    init_logging(config.log_level, debug=bool(on))


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


def copy_path(target: str) -> bool:
    """把目录路径复制到剪贴板（打不开资源管理器时的降级：用户粘贴到地址栏）。

    为什么需要：受限/沙箱环境里 ``os.startfile`` 会抛 WinError 5，资源管理器打不开；
    但路径仍可复制。调用方根据返回值给出"已复制，请粘贴"的提示。
    """
    folder = target if Path(target).is_dir() else str(Path(target).parent)
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            ["clip"],
            input=folder,
            text=True,
            capture_output=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception as exc:
        logger.warning("复制目录路径失败（{}）：{}", folder, exc)
        return False


# ---------------- 索引期参数与指纹（P0.6） ----------------


def rebuild_lexical() -> dict[str, Any]:
    """**只重算词法**（P1.8 快路径：改了词库/分词参数之后用）。

    返回：``{"updated": 更新行数, "status": 之后的状态}``。
    为什么在这里记指纹：重算完就是"索引已按这套参数建过"，否则提示条永远不会消失。
    """
    from app.rag.ingest import rebuilder

    updated = rebuilder.rebuild_lexical()
    return {"updated": updated, "status": mark_index_built()}


def rebuild_all(doc_ids: list[int] | None = None) -> int:
    """**全量重建**（P1.8 的重活）：排队一条 ``reindex`` 任务，返回任务 id。

    为什么走队列：全量重建是小时级的活，放界面里跑会卡死窗口，也没法取消。
    """
    from app.services.knowledge import enqueue_reindex

    return enqueue_reindex(doc_ids)


#: 过期判定的缓存：``(参数快照, 词条哈希) → index_status()``。
#: 为什么需要：知识库页每次自动刷新都会问一次"过期了吗"，而算一次要读两份指纹 +
#: 查一次词条表。界面每 2 秒问一次就是白白重复（D3）。
_STATUS_CACHE: dict[str, Any] = {}


def _status_key() -> tuple[Any, str, str]:
    """缓存键：**参数快照 + 词条内容哈希 + 已记录的动态字段版本**。"""
    from app.core.retrieval import terms

    config = params_store.load()
    values = getattr(config, "values", {}) or {}
    snapshot = tuple(sorted((str(key), str(value)) for key, value in dict(values).items()))
    return snapshot, terms.terms_hash(), params_store.stored_attrs_version()


def invalidate_index_status() -> None:
    """丢掉过期判定缓存（保存参数、重算词法、重建索引之后必须要调）。"""
    _STATUS_CACHE.clear()


def index_status() -> dict[str, Any]:
    """索引是否过期、过期了要做什么。

    返回：
        ``{"action": "none"|"lexical"|"full", "detail": 一句话,
        "changed_labels": [中文参数名], "stored": {...}, "current": {...}}``。
        界面用它决定"要不要显示提示条、显示哪两个按钮"（技术方案 §12 第 4 条）。

    结果按 ``(参数快照, 词库文件状态)`` 缓存：界面自动刷新会反复问，而算一次不便宜。
    """
    key = _status_key()
    cached = _STATUS_CACHE.get("entry")
    if cached is not None and cached[0] == key:
        return cached[1]  # type: ignore[no-any-return]
    report = params_store.describe_staleness(params_store.load())
    labels: list[str] = []
    for keys in report["stale"].values():
        labels.extend(params_store.param_labels(list(keys)))
    result = {
        "action": report["action"],
        "attrs_stale": bool(report.get("attrs_stale")),
        "detail": report["detail"],
        "changed_labels": labels,
        "stored": report["stored"],
        "current": report["current"],
        "stale": report["stale"],
    }
    _STATUS_CACHE["entry"] = (key, result)
    return result


def mark_index_built() -> dict[str, Any]:
    """把**当前参数**记为"索引就是按这套建的"（建索引 / 重建完成后调用）。

    返回：写入后的 :func:`index_status`（正常应当立刻变成 ``action="none"``）。
    """
    config = params_store.load()
    params_store.store_fingerprints(params_store.compute_fingerprints(config), config)
    logger.info("已记录索引指纹（词条哈希与参数已落库）")
    invalidate_index_status()
    return index_status()
