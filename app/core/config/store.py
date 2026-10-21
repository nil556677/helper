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

import base64
import json
import time
from pathlib import Path

from loguru import logger

from app.core.config import fingerprint
from app.core.config.params import (
    BY_KEY,
    KEY_ATTRS_VERSION,
    KEY_LEXICAL_FINGERPRINT,
    KEY_VECTOR_FINGERPRINT,
    PER_ITEM_PREFIX,
    Aspect,
    Config,
    Param,
)
from app.core.db.connection import get_connection, transaction

#: 动态字段提取版本的进程级"已打标"缓存：避免每批写入都 upsert 一次 index_meta。
_attrs_marked = False

#: 密文在 ``settings.value_json`` 里的前缀（JSON 字符串形如 ``"enc:<base64>"``）。
#: 有前缀 ⇒ 密文；没有前缀 ⇒ 明文（正常只有 DPAPI 加密环境异常降级时才会这样）。
_CIPHER_PREFIX = "enc:"


def _encrypt(value: object) -> str:
    """把凭证参数加密成 ``enc:<base64(DPAPI 密文)>``。

    DPAPI 不可用时**降级为明文并告警**：宁可让凭证还能用（同时留下明确日志），
    也不要因为加密环境异常让用户"设置里的 key 凭空消失"。项目本身是 Windows 专用，
    正常路径不会走到降级分支。
    """
    text = str(value or "")
    if not text:
        return ""
    try:
        from app.core.security import dpapi

        cipher = dpapi.protect(text.encode("utf-8"))
    except Exception as exc:
        logger.warning("凭证参数加密失败（将按明文存储，请检查运行环境）：{}", exc)
        return text
    return _CIPHER_PREFIX + base64.b64encode(cipher).decode("ascii")


def _decrypt(stored: object) -> str:
    """把 ``enc:<...>`` 解回明文；没有前缀说明是明文（DPAPI 降级时存的），原样返回。"""
    text = str(stored or "")
    if not text.startswith(_CIPHER_PREFIX):
        return text
    try:
        from app.core.security import dpapi

        raw = base64.b64decode(text[len(_CIPHER_PREFIX):])
        return dpapi.unprotect(raw).decode("utf-8")
    except Exception as exc:
        # 跨用户/换机器/密文损坏都会走到这里：按空值处理（连接会明确报鉴权失败），
        # 而不是把一段 base64 当成 key 发给服务端、给出更难懂的 401。
        logger.warning("凭证参数解密失败（按空值处理，请重新填写）：{}", exc)
        return ""


def load() -> Config:
    """读当前参数（settings 里的覆盖 + 清单里的默认值）。

    返回：:class:`Config`。缺项用默认值补齐，越界值夹回范围，坏值当没设过。
    ``secret=True`` 的参数会在这里**解密**——调用方拿到的始终是明文。
    """
    rows = get_connection().execute("SELECT key, value_json FROM settings").fetchall()
    values: dict[str, object] = {}
    for row in rows:
        key = str(row["key"])
        if not key.startswith(("chunk.", "embed.", "retrieval.", "rerank.", "tokenize.", "vector.")):
            continue  # 别把界面偏好（窗口几何之类）当参数读进来
        try:
            value = json.loads(str(row["value_json"]))
        except ValueError as exc:
            logger.warning("参数 {} 的值不是合法 JSON，按默认值处理：{}", key, exc)
            continue
        param = BY_KEY.get(key)
        if param is not None and param.secret:
            value = _decrypt(value)
        values[key] = value
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
            param = BY_KEY.get(key)
            stored: object = value
            if param is not None and param.secret:
                # 凭证类参数**加密后落库**（与 llm_slots.api_key_cipher 同一策略）：
                # 明文凭证不该出现在 settings / 备份 / 误发的数据库文件里。
                stored = _encrypt(value)
            conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                (key, json.dumps(stored, ensure_ascii=False), now),
            )


def aspect_extra(aspect: Aspect) -> dict[str, str]:
    """算某个指纹时要额外加进来的项（参数清单之外的东西）。

    **全局领域词只进词法指纹**：词条改变的是"怎么切词"，与向量无关——
    如果它也进向量指纹，用户改一个词就会被提示"全量重建 8 小时"，
    而实际只需重算词法（秒级）。这正是 :func:`describe_staleness` 要分开算的原因。

    词条内容的稳定哈希由 :func:`app.core.retrieval.terms.terms_hash` 提供
    （空表是 ``"empty"``、库不可用是 ``"unavailable"``，都是确定值，不会漏判）。

    **``embed.pad_to`` 两个指纹都要含**：它不是用户参数（是模型常量
    ``app/core/embed/model.py:PAD_TO``），但既决定向量值、又决定子块尺寸（块边界）——
    代码里改了它，老库必须提示重建，否则"向量空间/块尺寸变了却没重建"。
    """
    from app.core.embed.model import PAD_TO  # 局部导入：避免 config 模块加载时拉起嵌入栈

    extra: dict[str, str] = {"embed.pad_to": str(int(PAD_TO))}
    if aspect is Aspect.LEXICAL:
        from app.core.retrieval import terms  # 局部导入：避免 config 加载时拉起分词栈

        extra["tokenize.terms_hash"] = terms.terms_hash()
    return extra


def compute_fingerprints(config: Config) -> dict[Aspect, str]:
    """按当前参数算两个指纹。

    参数：
        config: 当前参数取值。
    返回：``{Aspect.VECTOR: str, Aspect.LEXICAL: str}``。
    """
    return {
        aspect: fingerprint.compute(
            config.fingerprint_items(aspect, extra=aspect_extra(aspect))
        )
        for aspect in (Aspect.VECTOR, Aspect.LEXICAL)
    }


def store_fingerprints(fingerprints: dict[Aspect, str], config: Config) -> None:
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
            items = config.fingerprint_items(aspect, extra=aspect_extra(aspect))
            for name, item in items.items():
                conn.execute(
                    "INSERT INTO index_meta(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (fingerprint.item_key(f"{aspect.value}.{name}"), str(item), now),
                )
        # 动态字段版本一起记（没有独立 Aspect，用一个普通键，见 params.KEY_ATTRS_VERSION）
        conn.execute(
            "INSERT INTO index_meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (KEY_ATTRS_VERSION, str(_attrs_version()), now),
        )
    global _attrs_marked
    _attrs_marked = True


def _attrs_version() -> int:
    """动态字段当前版本（进 index_meta 用；本地导入避免循环）。"""
    from app.core.attrs.version import ATTRS_VERSION

    return ATTRS_VERSION


def stored_fingerprints() -> dict[str, str]:
    """读回已存的指纹（键名 → 值）。没有就是空 dict（＝还没建过索引）。"""
    rows = get_connection().execute(
        "SELECT key, value FROM index_meta WHERE key IN (?, ?, ?)",
        (KEY_VECTOR_FINGERPRINT, KEY_LEXICAL_FINGERPRINT, KEY_ATTRS_VERSION),
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
    global _attrs_marked
    with transaction() as conn:
        conn.execute(
            "DELETE FROM index_meta WHERE key = ? OR key = ? OR key = ? OR key LIKE ?",
            (KEY_VECTOR_FINGERPRINT, KEY_LEXICAL_FINGERPRINT, KEY_ATTRS_VERSION, f"{PER_ITEM_PREFIX}%"),
        )
    _attrs_marked = False


def _upsert_meta(key: str, value: str, now: float = 0.0) -> None:
    """upsert 一条 ``index_meta``（进程内幂等由调用方决定）。"""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO index_meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, value, now or time.time()),
        )


def mark_attrs_version(*, force: bool = False) -> None:
    """把当前动态字段版本记进 ``index_meta``（幂等；进程序内缓存避免每批写库）。

    写入侧（``writer.store_records`` / ``backfill_attrs``）在写过带属性的数据后调用它——
    这样界面才知道"索引里的动态字段是按哪版建的"，版本一变就提示回填。
    """
    from app.core.attrs.version import ATTRS_VERSION

    global _attrs_marked
    if _attrs_marked and not force:
        return
    _upsert_meta(KEY_ATTRS_VERSION, str(ATTRS_VERSION))
    _attrs_marked = True


def stored_attrs_version() -> str:
    """读回已记录的动态字段版本；没记过返回空串（＝可能是老数据或没导入过）。"""
    row = get_connection().execute(
        "SELECT value FROM index_meta WHERE key = ?", (KEY_ATTRS_VERSION,)
    ).fetchone()
    return str(row["value"]) if row is not None else ""


def describe_staleness(
    config: Config,
    *,
    userdict_path: Path | None = None,  # noqa: ARG001
) -> dict[str, object]:
    """把"索引是否过期"整理成界面直接能用的一份数据。

    返回：
        ``{"stored": {...}, "current": {...}, "stale": {分项: [变了的项]},
        "action": "none" | "lexical" | "full", "detail": 给人看的一句话}``。

    ``action`` 的判定（**只要向量指纹对不上就全量**，因为向量指纹包含切片参数）：
        - 两个都对得上 → ``none``（不提示）；
        - 只有词法对不上 → ``lexical``（秒级）；
        - 向量对不上 → ``full``（小时级）。
    """
    current = compute_fingerprints(config)
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
        items = config.fingerprint_items(aspect, extra=aspect_extra(aspect))
        changed = fingerprint.diff(items, stored_items(aspect))
        stale[aspect.value] = changed or ["（指纹算法变了）"]

    # 动态字段版本独立判定：它只影响 attrs*（回填即可，不重嵌入）。
    attrs_stale = stored.get(KEY_ATTRS_VERSION, "") != str(_attrs_version())
    if attrs_stale:
        stale["attrs"] = ["attrs.version"]

    if Aspect.VECTOR.value in stale:
        action = "full"
        detail = "索引已过期：影响向量的参数变了，需要全量重建。"
    elif Aspect.LEXICAL.value in stale:
        action = "lexical"
        detail = "索引已过期：只有词法相关的东西变了，重算词法即可（秒级）。"
    elif not stale:
        action = "none"
        detail = "索引与当前参数一致，不需要重建。"
    else:
        # 只剩 attrs 过期：不重建、不重嵌，回填动态字段即可
        action = "none"
        detail = "动态字段规则已更新：建议回填动态字段（不重嵌，秒级）。"

    return {
        "stored": stored,
        "current": {aspect.value: value for aspect, value in current.items()},
        "stale": stale,
        "action": action,
        "attrs_stale": attrs_stale,
        "detail": detail,
    }


def param_labels(keys: list[str]) -> list[str]:
    """把参数键翻成界面上的中文名（"到底哪个变了"要说人话）。"""
    labels: list[str] = []
    for key in keys:
        if key == "attrs.version":
            labels.append("动态字段规则")
            continue
        param: Param | None = BY_KEY.get(key)
        labels.append(param.label if param is not None else key)
    return labels
