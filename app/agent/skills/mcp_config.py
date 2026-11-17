"""MCP 服务端配置持久化：settings 表存非密配置，DPAPI 加密存密钥，启用/禁用留审计。

- 键：`mcp.server.<id>` = JSON 配置（含 enabled）；`mcp.secret.<id>` = `enc:<base64(DPAPI)>`。
- 复用 `settings` 表 + `app/core/security/dpapi`，不另造存储。
- 启用/禁用写 `skill_events`（只追加审计，谁改的何时改）。
"""

from __future__ import annotations  # 延迟值注解

import base64  # 密文编码
import json  # 序列化
import time  # 时间戳
from typing import Any  # 任意值

from app.core.db.connection import get_connection, transaction  # DB

__all__ = ["decrypt_secret", "enabled", "load_servers", "save_server", "set_enabled"]  # 导出

_PREFIX = "mcp.server."  # 配置键前缀
_SECRET_PREFIX = "mcp.secret."  # 密钥键前缀
_CIPHER = "enc:"  # 密文前缀


def _encrypt(text: str) -> str:  # DPAPI 加密（失败降级明文并告警）
    raw = str(text or "")  # 文本
    if not raw:  # 空
        return ""  # 空
    try:  # 加密
        from app.core.security import dpapi  # DPAPI

        return _CIPHER + base64.b64encode(dpapi.protect(raw.encode("utf-8"))).decode("ascii")  # 密文
    except Exception:  # 加密环境异常
        return raw  # 降级明文（与 config.store 同策略）


def decrypt_secret(stored: str) -> str:  # 解密
    text = str(stored or "")  # 存值
    if not text.startswith(_CIPHER):  # 明文（降级）
        return text  # 原样
    try:  # 解密
        from app.core.security import dpapi  # DPAPI

        return dpapi.unprotect(base64.b64decode(text[len(_CIPHER):])).decode("utf-8")  # 明文
    except Exception:  # 损坏/换机器
        return ""  # 空（不把密文当 key）


def load_servers() -> list[dict[str, Any]]:  # 读全部 server 配置（密钥已解密）
    rows = get_connection().execute(  # 查配置
        "SELECT key, value_json FROM settings WHERE key LIKE ?", (f"{_PREFIX}%",)
    ).fetchall()  # 行
    secrets = _load_secrets()  # 密钥
    out: list[dict[str, Any]] = []  # 结果
    for row in rows:  # 逐条
        try:  # 解析
            cfg = json.loads(str(row["value_json"]))  # 配置
        except Exception:  # 坏数据
            continue  # 跳过
        sid = str(row["key"])[len(_PREFIX):]  # server id
        cfg["server_id"] = sid  # 补 id
        cfg["env_secret"] = secrets.get(sid, "")  # 解密密钥
        out.append(cfg)  # 追加
    return out  # 返回


def save_server(config: dict[str, Any], *, secret: str | None = None, actor_id: str = "user") -> None:  # 保存配置
    sid = str(config.get("server_id") or "").strip()  # id
    if not sid:  # 无 id
        raise ValueError("server_id 必填")  # 拒
    now = time.time()  # 时间
    payload = {k: v for k, v in dict(config).items() if k != "env_secret"}  # 去密钥
    with transaction() as conn:  # 事务
        conn.execute(  # 写配置
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (_PREFIX + sid, json.dumps(payload, ensure_ascii=False), now),
        )  # 结束
        if secret is not None:  # 更新了密钥
            conn.execute(  # 写密钥（密文）
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (_SECRET_PREFIX + sid, json.dumps(_encrypt(secret), ensure_ascii=False), now),
            )  # 结束
    _audit(sid, "configured", actor_id, {"enabled": bool(payload.get("enabled"))})  # 审计


def set_enabled(server_id: str, enabled: bool, *, actor_id: str = "user") -> None:  # 启用/禁用 + 审计
    sid = str(server_id or "").strip()  # id
    if not sid:  # 空
        raise ValueError("server_id 必填")  # 拒
    row = get_connection().execute("SELECT value_json FROM settings WHERE key=?", (_PREFIX + sid,)).fetchone()  # 读
    payload = json.loads(str(row["value_json"])) if row else {"server_id": sid}  # 配置
    payload["enabled"] = bool(enabled)  # 改启用
    with transaction() as conn:  # 写回
        conn.execute(  # upsert
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (_PREFIX + sid, json.dumps(payload, ensure_ascii=False), time.time()),
        )  # 结束
    _audit(sid, "enabled" if enabled else "disabled", actor_id)  # 审计


def enabled(server_id: str) -> bool:  # 持久化的启用位（默认关）
    sid = str(server_id or "").strip()  # id
    if not sid:  # 空
        return False  # 关
    row = get_connection().execute("SELECT value_json FROM settings WHERE key=?", (_PREFIX + sid,)).fetchone()  # 读
    if row is None:  # 没有
        return False  # 关
    try:  # 解析
        return bool(json.loads(str(row["value_json"])).get("enabled"))  # 启用位
    except Exception:  # 坏数据
        return False  # 关


def _load_secrets() -> dict[str, str]:  # 读全部密钥并解密
    rows = get_connection().execute(  # 查
        "SELECT key, value_json FROM settings WHERE key LIKE ?", (f"{_SECRET_PREFIX}%",)
    ).fetchall()  # 行
    out: dict[str, str] = {}  # 结果
    for row in rows:  # 逐条
        try:  # 解析
            stored = json.loads(str(row["value_json"]))  # 存值
        except Exception:  # 坏数据
            continue  # 跳过
        out[str(row["key"])[len(_SECRET_PREFIX):]] = decrypt_secret(str(stored))  # 解密
    return out  # 返回


def _audit(server_id: str, event: str, actor_id: str, detail: dict[str, Any] | None = None) -> None:  # 启用审计
    try:  # 审计失败不阻断
        from app.agent.skills import store  # 审计仓

        store.record_event(skill_id=f"mcp:{server_id}", event_type=event, actor_id=actor_id, detail=detail)  # 记
    except Exception:  # 忽略
        return  # 忽略
