"""引导配置（日志级别 / 调试模式 / 监听地址）：**存数据库，不再依赖 config.json**。

## 为什么改

以前这三项在 ``config.json``：手改、备份、和 DB 里的设置分成两处，用户要记两个地方；
而且 ``config.json`` 坏一个字符就可能打不开软件。现在统一进 ``settings`` 表
（``bootstrap.*``），界面「设置 · 通用」里改，重启后从库读——只剩一个配置源。

## 启动顺序（很重要）

数据库路径是写死的（``data/helper.db``），所以可以**先建库、再读配置**：

1. 目录 + 默认日志；
2. ``schema.init_db()``；
3. :func:`ensure_default`：库里有就用；没有就写默认值；
4. :func:`load` 读库，按级别/调试模式重建日志。

配置的**唯一来源就是数据库**：不再读取安装目录的 ``config.json``（该文件已废弃，
只保留路径概念用于日志/自检提示身份，不读、不迁移、不归档）。

## 失败口径

- 读库失败（表还没建/库坏了）→ 返回默认配置，让程序先起来（真正坏库由启动体检报）；
- 配置有一项不合法 → 回落默认值并记 ERROR，不阻断启动。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from app.core import paths
from app.core.config.schema import DEFAULT_HOST, BootstrapConfig, is_loopback

#: 数据库里的键前缀（与业务参数共享 ``settings`` 表，靠前缀区分）。
_PREFIX = "bootstrap."

#: 进程内缓存（保存后调 :func:`invalidate` 作废）。
_cache: BootstrapConfig | None = None


def config_file() -> Path:
    """（已废弃的）配置文件路径：仅供日志/自检提示身份，运行时不再读取它。"""
    return paths.config_file()


def _read_db() -> dict[str, Any] | None:
    """读 ``bootstrap.*``；表不存在/库不可用返回 None（调用方回落默认值）。"""
    from app.core.db.connection import get_connection

    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT key, value_json FROM settings WHERE key LIKE ?", (_PREFIX + "%",)
        ).fetchall()
    except Exception as exc:  # 表还没建 / 库文件暂时不可读
        logger.debug("读引导配置失败（按默认值处理）：{}", exc)
        return None
    if not rows:
        return None
    data: dict[str, Any] = {}
    for key, value in rows:
        name = str(key)[len(_PREFIX) :]
        try:
            data[name] = json.loads(str(value))
        except (TypeError, ValueError):
            logger.warning("引导配置项不是合法 JSON，已忽略：{}", key)
    return data or None


def _validate(data: dict[str, Any]) -> BootstrapConfig:
    """把库里读出的值校验成配置；坏值回落默认值（**不阻断启动**）。"""
    try:
        config = BootstrapConfig.model_validate(data)
    except ValidationError as exc:
        logger.error("引导配置有一项不合法，已回落默认值：{}", exc)
        return BootstrapConfig()
    if not is_loopback(config.host):
        logger.warning(
            "数据库里的 host={} 不是回环地址，已按 {} 运行（界面无认证）",
            config.host,
            DEFAULT_HOST,
        )
        config = config.model_copy(update={"host": DEFAULT_HOST})
    return config


def load_fresh() -> BootstrapConfig:
    """强制重读数据库（不走进程内缓存）。"""
    global _cache
    data = _read_db()
    _cache = _validate(data) if data is not None else BootstrapConfig()
    return _cache


def load() -> BootstrapConfig:
    """取当前引导配置（进程内缓存；保存后自动作废）。"""
    global _cache
    if _cache is None:
        data = _read_db()
        _cache = _validate(data) if data is not None else BootstrapConfig()
    return _cache


def invalidate() -> None:
    """作废缓存（保存后、测试替换安装目录后调用）。"""
    global _cache
    _cache = None


def save(config: BootstrapConfig) -> None:
    """把配置写进 ``settings`` 表（原子事务；调用方负责提示失败）。"""
    from app.core.db.connection import transaction

    now = time.time()
    payload = config.model_dump(mode="json")
    with transaction() as conn:
        for name, value in payload.items():
            conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                (f"{_PREFIX}{name}", json.dumps(value, ensure_ascii=False), now),
            )
    invalidate()


def ensure_default() -> Path:
    """保证库里有引导配置；没有则写默认值。返回配置文件路径（调用方仅用于日志提示）。"""
    path = config_file()
    if _read_db() is not None:
        invalidate()  # 防止启动早期按默认值缓存过一份
        return path
    save(BootstrapConfig())
    logger.info("已写入默认引导配置（日志级别 INFO / 调试关闭 / 监听 127.0.0.1）")
    return path
