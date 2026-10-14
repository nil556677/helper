"""模型槽位的读写与连接测试（**UI-free**，可以直接被 pytest 调用）。

**配置全在数据库**：每个槽位一行写进 ``llm_slots``；api key 只存 **DPAPI 密文 + 尾号**，
明文既不落盘也不进日志。``config.json`` 只留"打开数据库之前就必须知道"的两项
（日志级别、数据目录）——见方案 §12。

**为什么只有两个槽位**：推理模型负责回答问题，OCR 模型负责把扫描件 / 图片转成文字。
以后加槽位 = 这里加一行（表里 ``slot`` 是主键、没写死取值约束，**不用迁移库**）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.db.connection import get_connection, transaction
from app.core.llm.openai_compat import ProbeResult, probe_chat, probe_ocr
from app.core.security import SecurityError, protect, unprotect

#: 尾号显示几个字符（只够辨认"是不是我那一把"，不足以还原）
KEY_TAIL_CHARS = 4

#: 明文 key 最短长度（各家都远长于这个；太短几乎肯定是粘错了）
KEY_MIN_CHARS = 8


@dataclass(frozen=True)
class Slot:
    """一个模型槽位。"""

    key: str
    """写进数据库的槽位名（``llm_slots.slot``）。"""

    title: str
    purpose: str
    example: str
    probe: str
    """用哪个探测函数：``chat`` 或 ``ocr``。"""


#: 现在的两个槽位。加槽位就在这后面加一行。
SLOTS: tuple[Slot, ...] = (
    Slot(
        key="reasoning",
        title="推理模型",
        purpose="回答问题时用的主模型，走 OpenAI 兼容协议",
        example="火山方舟 / doubao-seed-1.6",
        probe="chat",
    ),
    Slot(
        key="ocr",
        title="OCR 模型",
        purpose="扫描件 / 图片转文字；没有它，扫描版资料进不来",
        example="火山方舟 / doubao-vision-ocr",
        probe="ocr",
    ),
)

_PROBES = {"chat": probe_chat, "ocr": probe_ocr}


def slot_keys() -> tuple[str, ...]:
    """全部槽位名。"""
    return tuple(slot.key for slot in SLOTS)


def _slot(slot_key: str) -> Slot:
    for slot in SLOTS:
        if slot.key == slot_key:
            return slot
    raise ValueError(f"不认识的模型槽位：{slot_key}（可选：{'、'.join(slot_keys())}）")


def _row(slot_key: str) -> Any:
    return get_connection().execute(
        "SELECT * FROM llm_slots WHERE slot = ?", (slot_key,)
    ).fetchone()


def load_all() -> list[dict[str, Any]]:
    """读出全部槽位（槽位元信息 + 已存配置）。

    **绝不返回明文 key**：只给尾号（``key_tail``）——界面就靠它告诉用户"存着的是哪一把"。

    返回：每项含 ``key / title / purpose / example / provider / base_url / model /
    key_tail / has_key / configured``。
    """
    rows = {
        str(row["slot"]): row
        for row in get_connection().execute("SELECT * FROM llm_slots").fetchall()
    }
    items: list[dict[str, Any]] = []
    for slot in SLOTS:
        row = rows.get(slot.key)
        base_url = str(row["base_url"]) if row else ""
        model = str(row["model"]) if row else ""
        items.append(
            {
                "key": slot.key,
                "title": slot.title,
                "purpose": slot.purpose,
                "example": slot.example,
                "provider": str(row["provider"]) if row else "",
                "base_url": base_url,
                "model": model,
                "key_tail": str(row["key_tail"] or "") if row else "",
                "has_key": bool(row and row["api_key_cipher"]),
                "configured": bool(base_url.strip() and model.strip()),
            }
        )
    return items


def _validate(provider: str, base_url: str, model: str) -> tuple[str, str, str]:
    """清洗并校验三个手填字段。"""
    provider, base_url, model = provider.strip(), base_url.strip(), model.strip()
    if not base_url:
        raise ValueError("接口地址不能为空")
    if not model:
        raise ValueError("模型名不能为空")
    if not base_url.lower().startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    return provider, base_url, model


def save(
    slot_key: str,
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str | None = None,
) -> None:
    """保存一个槽位（每个槽位各自保存，互不影响）。

    参数：
        slot_key: 槽位名（见 :data:`SLOTS`）。
        provider: 服务商（手填，仅用于展示与排查）。
        base_url: 接口地址。
        model: 模型名。
        api_key: ``None`` = **不动**已存的 key；空串 = 清掉；其他 = 换成它
            （加密后只存密文与尾号）。
    异常：
        ValueError: 槽位名不认识，或必填项为空 / 明显不合法。
        SecurityError: 加密失败（比如不是 Windows）。
    """
    slot = _slot(slot_key)
    provider, base_url, model = _validate(provider, base_url, model)

    if api_key is not None and api_key.strip() and len(api_key.strip()) < KEY_MIN_CHARS:
        raise ValueError(f"api key 太短（少于 {KEY_MIN_CHARS} 个字符），像是没粘全")

    import time

    now = time.time()
    with transaction() as conn:
        if api_key is None:
            # 不动密钥列：只更新手填的三项
            conn.execute(
                "INSERT INTO llm_slots(slot, provider, base_url, model, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(slot) DO UPDATE SET provider = excluded.provider,"
                " base_url = excluded.base_url, model = excluded.model,"
                " updated_at = excluded.updated_at",
                (slot.key, provider, base_url, model, now),
            )
            return

        key = api_key.strip()
        if not key:
            conn.execute(
                "INSERT INTO llm_slots(slot, provider, base_url, model, api_key_cipher, key_tail,"
                " updated_at) VALUES (?, ?, ?, ?, NULL, NULL, ?)"
                " ON CONFLICT(slot) DO UPDATE SET provider = excluded.provider,"
                " base_url = excluded.base_url, model = excluded.model,"
                " api_key_cipher = NULL, key_tail = NULL, updated_at = excluded.updated_at",
                (slot.key, provider, base_url, model, now),
            )
            return

        cipher = protect(key.encode("utf-8"))
        conn.execute(
            "INSERT INTO llm_slots(slot, provider, base_url, model, api_key_cipher, key_tail,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(slot) DO UPDATE SET provider = excluded.provider,"
            " base_url = excluded.base_url, model = excluded.model,"
            " api_key_cipher = excluded.api_key_cipher, key_tail = excluded.key_tail,"
            " updated_at = excluded.updated_at",
            (slot.key, provider, base_url, model, cipher, key[-KEY_TAIL_CHARS:], now),
        )


def test_connection(slot_key: str, *, timeout: float | None = None) -> dict[str, Any]:
    """拿库里存着的配置**真发一次极小的请求**，把"通不通"验出来。

    参数：
        slot_key: 槽位名。
        timeout: 覆盖探测超时（测试用）。
    返回：``{"ok": bool, "kind": str, "detail": str, "elapsed_ms": int}``。
        ``kind`` 见 :data:`app.core.llm.openai_compat.KIND_LABELS`——**失败会分类**，
        用户照着 detail 就能改。
    """
    slot = _slot(slot_key)
    row = _row(slot_key)
    if row is None or not str(row["base_url"]).strip() or not str(row["model"]).strip():
        return {"ok": False, "kind": "config", "detail": "先填好接口地址与模型名并保存", "elapsed_ms": 0}
    if not row["api_key_cipher"]:
        return {"ok": False, "kind": "config", "detail": "还没填 api key", "elapsed_ms": 0}

    try:
        api_key = unprotect(bytes(row["api_key_cipher"])).decode("utf-8")
    except SecurityError as exc:  # 换用户/换机器/密文损坏都会走到这里
        return {
            "ok": False,
            "kind": "auth",
            "detail": f"存着的密钥解不开：{exc}。请重新粘贴一次 key",
            "elapsed_ms": 0,
        }

    probe = _PROBES[slot.probe]
    kwargs = {} if timeout is None else {"timeout": timeout}
    result: ProbeResult = probe(str(row["base_url"]), str(row["model"]), api_key, **kwargs)
    return {
        "ok": result.ok,
        "kind": result.kind,
        "detail": result.detail,
        "elapsed_ms": result.elapsed_ms,
    }
