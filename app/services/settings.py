"""设置相关的编排：汇总数据概览、读版本、打开目录、**索引期参数与指纹**。

界面只认这里返回的普通数据（dict / 字符串），不碰 core 内部细节（技术方案 §5.1 红线 2）。

**这里没有日志级别的读写**：那个旋钮只由 `config.json` 给，界面不提供（§10.4 第 12 条），
所以 2026-10 把 `log_level()` / `save_log_level()` 一并删了（不留死代码）。

**索引期参数**（P0.6）：默认值只在 `app/core/config/params.py` 写一份，这里只做
"读出来给界面画 / 写回去 / 算过不过期"三件事。界面不该自己拼参数名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from app.core import paths
from app.core.config import params as params_mod
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


# ---------------- 索引期参数与指纹（P0.6） ----------------


def userdict_path() -> Path:
    """自定义词库文件的实际路径（``tokenize.userdict`` 是相对安装目录的相对路径）。

    相对路径存进配置、在这里拼成绝对路径：安装目录换了（用户挪了文件夹）配置仍然有效。
    """
    configured = str(params_store.load()["tokenize.userdict"])
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else paths.project_root() / candidate


def parameter_groups() -> list[dict[str, Any]]:
    """给界面画参数表用的分组数据（含当前值与元信息）。

    返回：``[{"title": 组名, "items": [{key,label,value,default,help,minimum,maximum,cast}]}]``。
    界面据此渲染，**不自己拼参数名**（拼错就是"改了没生效"）。
    """
    config = params_store.load()
    groups: list[dict[str, Any]] = []
    for title, items in params_mod.GROUPS:
        groups.append(
            {
                "title": title,
                "items": [
                    {
                        "key": param.key,
                        "label": param.label,
                        "value": config[param.key],
                        "default": param.default,
                        "help": param.help_text,
                        "minimum": param.minimum,
                        "maximum": param.maximum,
                        "kind": "bool" if param.cast is bool else ("text" if param.cast is str else "number"),
                        "rebuild": _rebuild_hint(param),
                    }
                    for param in items
                ],
            }
        )
    return groups


def _rebuild_hint(param: params_mod.Param) -> str:
    """这个参数改了要不要重建、重建多大范围（界面上一行提示）。"""
    if not param.into:
        return "改了不用重建"
    if params_mod.Aspect.LEXICAL in param.into and params_mod.Aspect.VECTOR not in param.into:
        return "改了只需重算词法（秒级）"
    return "改了要全量重建（小时级）"


def save_parameters(changes: dict[str, Any]) -> dict[str, Any]:
    """保存参数并返回**保存后的索引状态**（界面据此弹提示条）。

    参数：
        changes: ``{参数键: 新值}``。
    返回：:func:`index_status` 的结果（含 ``action`` / ``detail`` / 变了哪几项）。
    异常：
        KeyError: 参数名不在清单里。
    """
    params_store.save_values(changes)
    return index_status()


def index_status() -> dict[str, Any]:
    """索引是否过期、过期了要做什么。

    返回：
        ``{"action": "none"|"lexical"|"full", "detail": 一句话,
        "changed_labels": [中文参数名], "stored": {...}, "current": {...}}``。
        界面用它决定"要不要显示提示条、显示哪两个按钮"（技术方案 §12 第 4 条）。
    """
    report = params_store.describe_staleness(
        params_store.load(), userdict_path=userdict_path()
    )
    labels: list[str] = []
    for keys in report["stale"].values():
        labels.extend(params_store.param_labels(list(keys)))
    return {
        "action": report["action"],
        "detail": report["detail"],
        "changed_labels": labels,
        "stored": report["stored"],
        "current": report["current"],
        "stale": report["stale"],
    }


def mark_index_built() -> dict[str, Any]:
    """把**当前参数**记为"索引就是按这套建的"（建索引 / 重建完成后调用）。

    返回：写入后的 :func:`index_status`（正常应当立刻变成 ``action="none"``）。
    """
    config = params_store.load()
    path = userdict_path()
    params_store.store_fingerprints(
        params_store.compute_fingerprints(config, userdict_path=path),
        config,
        userdict_path=path,
    )
    logger.info("已记录索引指纹（{}）", path.name)
    return index_status()
