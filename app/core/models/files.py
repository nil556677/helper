"""模型文件的完整性与哈希缓存。

计划 T3.1：三份模型文件（嵌入 / 精排 / tokenizer）的**大小 + sha256** 清单，
以及按情况分级的校验（缺 = ERROR、大小不符 = ERROR、哈希不符 = WARNING）。

**为什么哈希不符要降级成 WARNING**：文档说明用户可能用别的 onnxruntime 重新量化过
——那是完全可用的另一份文件，只是和我们清单里的不是同一份。把它当成"必须重下"会误伤
自量化的用户。所以默认 WARNING，并提供 :func:`update_manifest_for` 让用户一次性"接受现状"。

**哈希缓存**：模型共 1.1 GB，每次启动都重算 sha256 是浪费。算过一次就在模型旁边写
``<name>.sha256``（含 mtime + 大小），下次比对大小与缓存值即可，不用重读全文件。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.core import paths

#: 缓存文件后缀（与模型同名，加 .sha256）。
CACHE_SUFFIX = ".sha256"


@dataclass(frozen=True)
class ModelFile:
    """清单里的一条模型文件记录。"""

    name: str
    size: int
    sha256: str


#: 三份模型的真值。改动这份时要先 `--update-manifest`（或 scripts/update_model_manifest.py）
#: 重算哈希，别手敲。
MANIFEST: dict[str, ModelFile] = {
    "bge-m3.int8.onnx": ModelFile(
        name="bge-m3.int8.onnx",
        size=568479395,
        sha256="2237f770aad5c71bbc1fc2d361a57f9a37400574cc9eff32626f0cdb49234730",
    ),
    "bge-reranker-v2-m3.int8.onnx": ModelFile(
        name="bge-reranker-v2-m3.int8.onnx",
        size=570727094,
        sha256="912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d",
    ),
    "tokenizer.json": ModelFile(
        name="tokenizer.json",
        size=17082821,
        sha256="6710678b12670bc442b99edc952c4d996ae309a7020c1fa0096dd245c2faf790",
    ),
}


class ModelFileError(RuntimeError):
    """模型文件有问题（缺失 / 大小不符 / 哈希不符）。"""


def list_names() -> tuple[str, ...]:
    """清单里的全部文件名。"""
    return tuple(MANIFEST)


def path_of(name: str) -> Path:
    """模型文件的路径（``models/<name>``）。"""
    return paths.models_dir() / name


def _str_hash(path: Path) -> str:
    """算一个文件的 sha256（1 MB 一块流式读，不吃内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_path(name: str) -> Path:
    return path_of(name).with_name(name + CACHE_SUFFIX)


def _cache_valid(name: str, size: int) -> str | None:
    """缓存可用就返回缓存里的哈希；否则 None。

    判据：缓存文件存在，且里面记录的 ``mtime_ns`` 与**当前文件的 mtime_ns + size** 都对得上。
    """
    cached = _cache_path(name)
    if not cached.is_file():
        return None
    try:
        payload = json.loads(cached.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        current = path_of(name).stat()
    except OSError:
        return None
    if payload.get("mtime_ns") == current.st_mtime_ns and payload.get("size") == size:
        return str(payload["sha256"])
    return None


def _write_cache(name: str, size: int, sha256: str) -> None:
    """把哈希结果写进缓存（原子写）。"""
    try:
        stat = path_of(name).stat()
    except OSError:
        return
    payload = {
        "mtime_ns": stat.st_mtime_ns,
        "size": size,
        "sha256": sha256,
    }
    target = _cache_path(name)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        tmp.replace(target)
    except OSError as exc:
        logger.warning("模型哈希缓存写入失败（{}）：{}", target, exc)


def _sha256_matching(name: str, size: int) -> str | None:
    """能复用缓存就复用；否则重算并写缓存。返回哈希（None 表示算不出来）。"""
    cached = _cache_valid(name, size)
    if cached is not None:
        return cached
    try:
        digest = _str_hash(path_of(name))
    except OSError as exc:
        logger.warning("模型文件读不了（哈希阶段）：{}：{}", name, exc)
        return None
    _write_cache(name, size, digest)
    return digest


def problems(name: str, *, deep: bool = True) -> list[str]:
    """校验一份模型文件，返回问题列表（空列表 = 没问题）。

    参数：
        name: 清单里的文件名。
        deep: 是否连哈希一起查（默认查；``--no-hash`` 或 ``deep=False`` 只查大小）。
    返回：
        每项一句人话（缺哪种、期望多大、实际多大）。**默认把哈希不符当 WARNING**——
        见模块说明。
    """
    if name not in MANIFEST:
        return [f"清单里没有这个模型：{name}"]
    expected = MANIFEST[name]
    path = path_of(name)
    if not path.is_file():
        return [
            f"模型缺失：{path.name}（期望 {expected.size} 字节）",
            f"去哪下：见 {paths.models_dir() / 'README.md'}",
        ]

    actual = path.stat().st_size
    if actual != expected.size:
        return [
            f"大小不符：期望 {expected.size}（{expected.size / 1048576:.1f} MiB），"
            f"实际 {actual}（{actual / 1048576:.1f} MiB）——多半是截断或下错了文件"
        ]

    if not deep:
        return []

    digest = _sha256_matching(name, actual)
    if digest is None:
        return ["哈希算不出来（文件读不了）"]
    if digest != expected.sha256:
        return [
            f"sha256 不符：期望 {expected.sha256}，实际 {digest}",
            "可能是你自己用别的 onnxruntime 重新量化过——这份文件也能用，只是不是清单里的同一份。",
        ]
    return []


def require(name: str, *, deep: bool = True) -> Path:
    """校验并返回模型的路径；有问题就抛 :class:`ModelFileError`。

    接线点：``--selfcheck`` 第 4 项，以及 P2 时 embed/rerank 首次加载前。
    """
    issues = problems(name, deep=deep)
    if issues:
        raise ModelFileError("；".join(issues))
    return path_of(name)


def update_manifest_for(name: str) -> ModelFile:
    """把**当前文件**的 (大小, sha256) 写回清单，返回更新后的记录，供自己量化的用户"接受现状"。

    **就地修改模块级 MANIFEST**（内存态）；要持久化得把改后的清单写回代码/或存到
    ``models/manifest.local.json``。本函数只更新内存清单 + 刷新缓存，供 ``--update-manifest`` 用。
    """
    path = path_of(name)
    if not path.is_file():
        raise ModelFileError(f"模型不存在，无法更新清单：{path}")
    size = path.stat().st_size
    digest = _str_hash(path)
    _write_cache(name, size, digest)
    record = ModelFile(name=name, size=size, sha256=digest)
    MANIFEST[name] = record
    logger.info("模型清单已更新：{} → {} 字节 {}", name, size, digest)
    return record