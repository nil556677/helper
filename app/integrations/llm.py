"""ChatOpenAI 工厂：按 ``llm_slots`` 槽位构造模型实例。

- key 只经 DPAPI 解密后传给 ``ChatOpenAI(api_key=SecretStr(...))``，绝不落日志；
- 按 (槽位, base_url, model, key 摘要, temperature, max_tokens, streaming) 缓存实例，
  换 key/模型后自然生成新实例（旧实例留在缓存里也无害，数量极少）；
- 没配全 → 返回 ``None``，调用方回退（重写/生成/路由都允许无模型）。
"""

from __future__ import annotations

import hashlib
import threading

from pydantic import SecretStr

from app.core.llm import store as slot_store
from app.core.security import SecurityError, unprotect

_LOCK = threading.Lock()
_CACHE: dict[tuple, object] = {}


def _key_digest(api_key: bytes) -> str:
    return hashlib.sha256(api_key).hexdigest()[:12]


def normalize_base_url(url: str) -> str:  # 规范化 OpenAI 兼容端点
    """只保留根地址：误填的 ``/chat/completions`` 会被 SDK 再拼一次，导致 404。"""
    value = str(url or "").strip().rstrip("/")  # 去空白与尾部斜杠
    for suffix in ("/chat/completions", "/completions"):  # 常见误填后缀
        if value.endswith(suffix):  # 命中后缀
            value = value[: -len(suffix)].rstrip("/")  # 去掉后缀
            break  # 只去一次
    return value  # 返回根地址


def chat_model(
    *,
    slot: str = "llm",
    temperature: float | None = None,
    streaming: bool = False,
    max_tokens: int | None = None,
    timeout_s: float | None = None,
    max_retries: int = 2,
    **options: object,
):
    """按槽位构造 ``ChatOpenAI``；未配置/解密失败返回 ``None``。

    ``options`` 是**调用处显式给的**采样参数（top_p / frequency_penalty / stop / extra_body…），
    原样转给 ChatOpenAI，并进缓存键——所以「同槽位不同参数」不会复用同一个实例。
    """
    row = slot_store.get(slot)
    if row is None:
        return None
    base_url = normalize_base_url(str(row["base_url"] or "").strip())  # 只留根地址
    model = str(row["model"] or "").strip()
    cipher = row["api_key_cipher"]
    if not base_url or not model or not cipher:
        return None
    try:
        api_key = unprotect(bytes(cipher))
    except (SecurityError, OSError, ValueError):
        return None

    from langchain_openai import ChatOpenAI

    extra_key = repr(sorted(options.items()))  # 采样参数进缓存键
    key = (
        slot,
        base_url,
        model,
        _key_digest(api_key),
        temperature,
        streaming,
        max_tokens,
        timeout_s,
        max_retries,
        extra_key,
    )
    with _LOCK:
        instance = _CACHE.get(key)
        if instance is None:
            instance = ChatOpenAI(
                model=model,
                base_url=base_url,
                api_key=SecretStr(api_key.decode("utf-8", "replace")),
                temperature=0.2 if temperature is None else temperature,
                max_tokens=max_tokens,
                timeout=float(timeout_s) if timeout_s is not None else 60,
                max_retries=max_retries,
                streaming=streaming,
                stream_usage=streaming,  # 让兼容网关在流末尾带 usage
                **options,  # 调用处显式给的其余参数
            )
            _CACHE[key] = instance
        return instance


def clear_model_cache() -> None:
    """设置页保存模型后调用，清掉旧实例。"""
    with _LOCK:
        _CACHE.clear()
