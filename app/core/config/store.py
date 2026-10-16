"""索引期参数的读写 + 指纹落盘（**SQL 都在这里**，上层只认 :class:`Config`）。

两处存储、两种用途：

- ``settings``（v1 就有）：参数的**当前取值**（用户改过的就存这，没改过用默认值）；
- ``index_meta``（v1 就有，形状正好是 ``key/value/updated_at``）：**建索引时用的指纹**，
  以及逐项留痕（`fingerprint.item.*`）——后者用来回答"到底哪个参数变了"。

**为什么逐项也要存**：只存一个总指纹的话，界面只能说"索引过期了，请重建"；
逐项存下来才能说"词库变了，只需重算词法"——这是"别让用户猜"里最要紧的一句
（技术方案 §12）。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from loguru import logger

from app.core.config import fingerprint
from app.core.config.params import (
    BY_KEY,
    KEY_LEXICAL_FINGERPRINT,
    KEY_VECTOR_FINGERPRINT,
    PER_ITEM_PREFIX,
    Aspect,
    Config,
    Param,
)
from app.core.db.connection import get_connection, transaction

#: 词库文件算哈希时的读取上限（防止用户误塞一个几百 MB 的文件进来）。1 MB 足够放几万词条。
MAX_USERDICT_BYTES = 1 << 20


def load() -> Config:
    """读当前参数（settings 里的覆盖 + 清单里的默认值）。

    返回：:class:`Config`。缺项用默认值补齐，越界值夹回范围，坏值当没设过。
    """
    rows = get_connection().execute("SELECT key, value_json FROM settings").fetchall()
    values: dict[str, object] = {}
    for row in rows:
        key = str(row["key"])
        if not key.startswith(("chunk.", "embed.", "retrieval.", "rerank.", "tokenize.", "vector.")):
            continue  # 别把界面偏好（窗口几何之类）当参数读进来
        try:
            values[key] = json.loads(str(row["value_json"]))
        except ValueError as exc:
            logger.warning("参数 {} 的值不是合法 JSON，按默认值处理：{}", key, exc)
    return Config(values=values)


def save_values(changes: dict[str, object]) -> None:
    """写入若干参数（只写传进来的，其余不动）。

    参数：
        changes: ``{参数键: 新值}``。
    异常：
        KeyError: 键不在清单里（拼错必须立刻炸；静默写进去会让用户"改了没生效"）。
    """
    for key in changes:
        if key not in BY_KEY:
            raise KeyError(f"没有这个参数：{key}（清单见 app/core/config/params.py）")

    now = time.time()
    with transaction() as conn:
        for key, value in changes.items():
            conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now),
            )


def userdict_hash(path: Path) -> str:
    """词库内容的哈希（进词法指纹）。

    返回：``"sha256:<前 16 位>"``；文件不存在返回 ``"missing"``，读不了返回 ``"unreadable"``。

    为什么不存在也算一个确定值：**"从无到有"必须让指纹变**——用户第一次建了词库文件就该
    提示重算词法。返回空串或 None 都可能被当成"没这一项"而漏掉。
    """
    try:
        if not path.is_file():
            return "missing"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            digest.update(handle.read(MAX_USERDICT_BYTES))
        return "sha256:" + digest.hexdigest()[:16]
    except OSError as exc:
        logger.warning("词库文件读不了（{}）：{}", path, exc)
        return "unreadable"


def aspect_extra(aspect: Aspect, *, userdict_path: Path) -> dict[str, str]:
    """算某个指纹时要额外加进来的项（参数清单之外的东西）。

    **词库只进词法指纹**：词库改变的是"怎么切词"，与向量无关——
    如果它也进向量指纹，用户改一次词库就会被提示"全量重建 8 小时"，
    而实际只需重算词法（秒级）。这正是 :func:`describe_staleness` 要分开算的原因。

    两个项都要有：``…_hash`` 管内容变化，``…_path`` 管"换了一份文件但内容为空/不存在"
    这种指纹看不出差别的情况。
    """
    if aspect is Aspect.LEXICAL:
        return {
            "tokenize.userdict_hash": userdict_hash(userdict_path),
            "tokenize.userdict_path": str(userdict_path),
        }
    return {}


def compute_fingerprints(config: Config, *, userdict_path: Path) -> dict[Aspect, str]:
    """按当前参数算两个指纹。

    参数：
        config: 当前参数取值。
        userdict_path: 自定义词库路径（只影响词法指纹）。
    返回：``{Aspect.VECTOR: str, Aspect.LEXICAL: str}``。
    """
    return {
        aspect: fingerprint.compute(
            config.fingerprint_items(aspect, extra=aspect_extra(aspect, userdict_path=userdict_path))
        )
        for aspect in (Aspect.VECTOR, Aspect.LEXICAL)
    }


def store_fingerprints(
    fingerprints: dict[Aspect, str], config: Config, *, userdict_path: Path
) -> None:
    """把指纹与逐项取值写进 ``index_meta``（建索引/重建完成后调用）。"""
    now = time.time()
    with transaction() as conn:
        for aspect, value in fingerprints.items():
            key = KEY_VECTOR_FINGERPRINT if aspect is Aspect.VECTOR else KEY_LEXICAL_FINGERPRINT
            conn.execute(
                "INSERT INTO index_meta(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, now),
            )
            items = config.fingerprint_items(
                aspect, extra=aspect_extra(aspect, userdict_path=userdict_path)
            )
            for name, item in items.items():
                conn.execute(
                    "INSERT INTO index_meta(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (fingerprint.item_key(f"{aspect.value}.{name}"), str(item), now),
                )


def stored_fingerprints() -> dict[str, str]:
    """读回已存的指纹（键名 → 值）。没有就是空 dict（＝还没建过索引）。"""
    rows = get_connection().execute(
        "SELECT key, value FROM index_meta WHERE key IN (?, ?)",
        (KEY_VECTOR_FINGERPRINT, KEY_LEXICAL_FINGERPRINT),
    ).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def stored_items(aspect: Aspect) -> dict[str, str]:
    """读回某个分项当初的逐项取值（去掉前缀，返回参数键 → 值）。"""
    prefix = fingerprint.item_key(f"{aspect.value}.")
    rows = get_connection().execute(
        "SELECT key, value FROM index_meta WHERE key LIKE ?", (f"{prefix}%",)
    ).fetchall()
    return {str(row["key"])[len(prefix) :]: str(row["value"]) for row in rows}


def clear_fingerprints() -> None:
    """清掉指纹与逐项留痕（"清空集合 / 从头重建"时用）。"""
    with transaction() as conn:
        conn.execute(
            "DELETE FROM index_meta WHERE key = ? OR key = ? OR key LIKE ?",
            (KEY_VECTOR_FINGERPRINT, KEY_LEXICAL_FINGERPRINT, f"{PER_ITEM_PREFIX}%"),
        )


def describe_staleness(config: Config, *, userdict_path: Path) -> dict[str, object]:
    """把"索引是否过期"整理成界面直接能用的一份数据。

    返回：
        ``{"stored": {...}, "current": {...}, "stale": {分项: [变了的项]},
        "action": "none" | "lexical" | "full", "detail": 给人看的一句话}``。

    ``action`` 的判定（**只要向量指纹对不上就全量**，因为向量指纹包含切片参数）：
        - 两个都对得上 → ``none``（不提示）；
        - 只有词法对不上 → ``lexical``（秒级）；
        - 向量对不上 → ``full``（小时级）。
    """
    current = compute_fingerprints(config, userdict_path=userdict_path)
    stored = stored_fingerprints()

    stale: dict[str, list[str]] = {}
    for aspect, value in current.items():
        stored_value = stored.get(
            KEY_VECTOR_FINGERPRINT if aspect is Aspect.VECTOR else KEY_LEXICAL_FINGERPRINT
        )
        if stored_value is None:
            stale[aspect.value] = ["（还没建过索引）"]
            continue
        if stored_value == value:
            continue
        items = config.fingerprint_items(
            aspect, extra=aspect_extra(aspect, userdict_path=userdict_path)
        )
        changed = fingerprint.diff(items, stored_items(aspect))
        stale[aspect.value] = changed or ["（指纹算法变了）"]

    if not stale:
        action = "none"
        detail = "索引与当前参数一致，不需要重建。"
    elif Aspect.VECTOR.value in stale:
        action = "full"
        detail = "索引已过期：影响向量的参数变了，需要全量重建。"
    else:
        action = "lexical"
        detail = "索引已过期：只有词法相关的东西变了，重算词法即可（秒级）。"

    return {
        "stored": stored,
        "current": {aspect.value: value for aspect, value in current.items()},
        "stale": stale,
        "action": action,
        "detail": detail,
    }


def param_labels(keys: list[str]) -> list[str]:
    """把参数键翻成界面上的中文名（"到底哪个变了"要说人话）。"""
    labels: list[str] = []
    for key in keys:
        param: Param | None = BY_KEY.get(key)
        labels.append(param.label if param is not None else key)
    return labels
