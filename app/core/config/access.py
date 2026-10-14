"""引导配置的读写（唯一入口）。

三条要求决定这里的形状：

1. **一次交互只读一次盘**：按"代"缓存（代 = ``mtime_ns + size + ctime_ns``）。只认前两项时，
   "同尺寸改写 + mtime 未变"会误命中——用户改了配置却不生效，而且怎么重启都查不出来。
2. **坏配置不能拖垮程序**：解析/校验失败就把它隔离成 ``config.bad-<时间戳>.json``，
   回落默认值继续跑。用户手改坏一个字符不该打不开软件。
3. **写入原子**：临时文件 + ``os.replace`` + ``fsync``。写一半掉电不能把配置清零。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from app.core import paths
from app.core.config.schema import BootstrapConfig

#: 缓存内容：(代判据, 解析结果)。
_cache: tuple[tuple[int, int, int], BootstrapConfig] | None = None

#: ``_`` 开头的键当注释用（配置文件里写 ``_note`` 说明字段含义）。
_NOTE_PREFIX = "_"


def config_file() -> Path:
    """当前引导配置文件路径。"""
    return paths.config_file()


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """取文件"代"判据；文件不存在返回 None。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)


def _strip_notes(payload: Any) -> Any:
    """递归剥掉 ``_`` 开头的注释键。

    为什么不直接在 schema 里允许未知键：那等于允许所有拼错的键名被静默忽略。
    剥注释键能同时保住"配置文件可读"和"拼错必报错"。
    """
    if isinstance(payload, dict):
        return {
            key: _strip_notes(value)
            for key, value in payload.items()
            if not (isinstance(key, str) and key.startswith(_NOTE_PREFIX))
        }
    if isinstance(payload, list):
        return [_strip_notes(item) for item in payload]
    return payload


def isolate_bad_file(path: Path, reason: str) -> str | None:
    """把坏配置挪到一边，返回新文件名（挪不动时返回 None）。

    为什么挪而不是删：用户手改的配置里可能有他自己要留的东西，直接删是最不可接受的处理。
    """
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"config.bad-{stamp}.json")
    # 同一秒里第二次隔离（用户连点保存又手改坏）不能覆盖上一份：那些坏文件是排查线索，
    # 覆盖掉等于毁证据。
    counter = 2
    while target.exists():
        target = path.with_name(f"config.bad-{stamp}-{counter}.json")
        counter += 1
    try:
        path.replace(target)
    except OSError as exc:  # noqa: BLE001 - 允许失败：隔离失败也要能继续启动
        logger.error("坏配置隔离失败（{}），继续用默认配置启动：{}", reason, exc)
        return None
    logger.error("配置不可用（{}），已隔离为 {}，本次用默认配置启动", reason, target.name)
    return target.name


def load_fresh() -> BootstrapConfig:
    """强制重读配置文件（不走缓存）。

    返回：解析成功的配置；文件不存在或不可用时返回**默认配置**（并尽力隔离坏文件）。
    """
    global _cache

    path = config_file()
    stamp = _stamp(path)
    if stamp is None:
        _cache = None
        return BootstrapConfig()

    try:
        payload = _strip_notes(json.loads(path.read_text(encoding="utf-8")))
        config = BootstrapConfig.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, UnicodeDecodeError) as exc:
        # 读不动 / 不是 JSON / 不符合 schema：三种都当"配置不可用"，隔离后回落默认值。
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        isolate_bad_file(path, first_line)
        _cache = None
        return BootstrapConfig()

    _cache = (stamp, config)
    return config


def load() -> BootstrapConfig:
    """取当前引导配置（带"代"缓存）。

    返回：配置对象。**调用方不要自己缓存返回值**——文件随时可能被改，
    这里已经按代缓存，外面再存一份就永远看不到新值。
    """
    path = config_file()
    stamp = _stamp(path)
    if stamp is None:
        return BootstrapConfig()
    if _cache is not None and _cache[0] == stamp:
        return _cache[1]
    return load_fresh()


def invalidate() -> None:
    """让下一次 :func:`load` 重读文件（保存配置后、测试里替换安装目录后都要调）。"""
    global _cache
    _cache = None


def save(config: BootstrapConfig) -> None:
    """原子写入配置文件，并作废缓存。

    参数：
        config: 要落盘的配置对象。
    异常：
        OSError: 写盘失败（调用方负责提示"配置没保存成功"）。
    """
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2)

    # 同目录的临时文件才能保证 os.replace 是原子替换（跨盘会退化成复制）。
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    invalidate()


def ensure_default() -> Path:
    """保证配置文件存在（不存在就从 ``config.example.json`` 复制，再退化为内置默认值）。

    返回：配置文件路径。首次启动调用一次，之后这里只是一次 stat。
    """
    path = config_file()
    if path.exists():
        return path

    example = paths.config_example_file()
    if example.exists():
        # 模板里带注释键与默认值，直接复制比"写出默认对象"对用户更友好。
        shutil.copyfile(example, path)
        logger.info("已从模板生成配置文件：{}", path)
    else:
        save(BootstrapConfig())
        logger.info("模板缺失，已写出内置默认配置：{}", path)
    invalidate()
    return path


def data_dir_override() -> str | None:
    """引导配置里的数据目录覆盖项（**给 app.core.paths 用**）。

    为什么单独一个函数：``paths.data_dir()`` 要读它，而本模块又用 ``paths``；
    从 paths 侧**惰性**调用这里，就不会在 import 期形成循环。
    """
    return load().data_dir
