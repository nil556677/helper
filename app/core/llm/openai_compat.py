"""OpenAI 兼容协议的**最小**客户端——现在只做一件事：把"配好没有"验出来。

**为什么自己写而不上 SDK**：这里只需要"发一次极小的请求，把失败原因分好类"。
SDK 会带进来一堆现在用不到的依赖与行为（重试、工具调用、各种封装…），等真要用了再说。
用**标准库 urllib**：不新增依赖——本地应用一次只发一个请求，用不着连接池。

**为什么失败必须分类**：模型配置最常见的毛病是"填了但通不了"，而"失败了"三个字对用户
毫无帮助。要能立刻分辨：key 不对 / 地址错 / 模型名错 / 被限流 / 超时 / 连不上——
每种给一句能照着改的话（见 :data:`KIND_LABELS`）。

**流式对话还不在这里**：等聊天接真模型时再补（那时才需要 SSE 与超时策略，现在写就是猜）。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

#: 单次探测的超时（秒）。故意比真实对话短：这只是"握个手"。
DEFAULT_TIMEOUT_S = 20.0

#: 失败分类 → 给用户看的一句话（**每种都要能照着改**）
KIND_LABELS: dict[str, str] = {
    "config": "还没填全",
    "auth": "密钥不对，或这个 key 没有该模型的权限",
    "not_found": "接口地址或模型名不对（404）",
    "bad_request": "请求被拒绝，多半是模型名不对（400）",
    "rate_limit": "被限流了，稍后再试（429）",
    "server": "对方服务出问题了（5xx）",
    "timeout": "超时：地址可能不对，或网络太慢",
    "network": "连不上：地址写错、网络不通或证书有问题",
    "unknown": "出了没预料到的问题",
}


#: 1×1 的透明 PNG（OCR 探测用：发一张真的能被解码的图，又小到不占流量）
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class ProbeResult:
    """一次连接探测的结果。"""

    ok: bool
    kind: str
    """``ok`` / ``config`` / ``auth`` / ``not_found`` / ``bad_request`` / ``rate_limit`` /
    ``server`` / ``timeout`` / ``network``。"""

    detail: str
    """给人看的一句话（成功时是"连接正常…"，失败时是可以照着改的原因）。"""

    elapsed_ms: int


def _endpoint(base_url: str) -> str:
    """拼出 chat 端点。

    用户可能只填到 ``/v1``，也可能整条 ``…/chat/completions`` 都粘进来——
    两种都要能用（后者原样返回，避免拼成 ``…/chat/completions/chat/completions``）。
    """
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


def _short(body: str) -> str:
    """从错误响应体里抠出一句能看的说明（各家格式不同，尽力而为）。"""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:160]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if message:
            return str(message)[:160]
    if isinstance(error, str):
        return error[:160]
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message)[:160] if message else text[:160]


def _classify_status(status: int, body: str) -> tuple[str, str]:
    """把 HTTP 状态码翻译成分类 + 一句人话。"""
    hint = _short(body)
    if status in (401, 403):
        kind = "auth"
    elif status == 404:
        kind = "not_found"
    elif status in (400, 422):
        kind = "bad_request"
    elif status == 429:
        kind = "rate_limit"
    else:
        kind = "server"
    detail = KIND_LABELS[kind] if status < 500 else f"{KIND_LABELS[kind]}（HTTP {status}）"
    if status in (400, 404, 422) and hint:
        detail = f"{detail}；对方说：{hint}"
    return kind, detail


def _post(url: str, payload: dict, api_key: str, timeout: float) -> tuple[int, str]:
    """发一个 JSON POST，返回 ``(状态码, 响应体)``；网络层的失败往上抛。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 对方给了状态码：这不算"网络失败"，要按状态码分类
        return int(exc.code), exc.read(4096).decode("utf-8", "replace")


def _missing(base_url: str, model: str, api_key: str) -> str:
    """检查三项必填；返回缺什么（都齐了返回空串）。"""
    if not base_url.strip():
        return "还没填接口地址"
    if not model.strip():
        return "还没填模型名"
    if not api_key.strip():
        return "还没填 api key"
    return ""


def _run(url: str, payload: dict, api_key: str, timeout: float, started: float, what: str) -> ProbeResult:
    """发请求并把结果翻译成 :class:`ProbeResult`。"""
    try:
        status, body = _post(url, payload, api_key, timeout)
    except TimeoutError:
        kind, detail = "timeout", KIND_LABELS["timeout"]
    except urllib.error.URLError as exc:
        # urlopen 的超时也走 URLError；reason 是 timeout 就归类成超时
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            kind, detail = "timeout", KIND_LABELS["timeout"]
        else:
            kind, detail = "network", f"{KIND_LABELS['network']}（{getattr(exc, 'reason', exc)}）"
    except OSError as exc:
        kind, detail = "network", f"{KIND_LABELS['network']}（{exc}）"
    else:
        if 200 <= status < 300:
            return ProbeResult(True, "ok", f"{what}连接正常", int((time.perf_counter() - started) * 1000))
        kind, detail = _classify_status(status, body)
    return ProbeResult(False, kind, detail, int((time.perf_counter() - started) * 1000))


def probe_chat(base_url: str, model: str, api_key: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> ProbeResult:
    """探测推理模型：发一条极小的对话请求（``max_tokens=1``）。

    参数：
        base_url: 接口地址（可以只到 ``/v1``）。
        model: 模型名。
        api_key: 明文密钥（只在这一次请求里存在）。
        timeout: 超时（秒）。
    返回：:class:`ProbeResult`。
    """
    problem = _missing(base_url, model, api_key)
    if problem:
        return ProbeResult(False, "config", problem, 0)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    started = time.perf_counter()
    return _run(_endpoint(base_url), payload, api_key, timeout, started, "推理模型")


def probe_ocr(base_url: str, model: str, api_key: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> ProbeResult:
    """探测 OCR 模型：发一张 1×1 的真实图片。

    **为什么不发纯文本**：OCR 模型只吃图片，发文本可能"成功"却根本没碰到图像通道——
    那就等于没验。发一张能被解码的小图，才算真的走通了这条路。
    """
    problem = _missing(base_url, model, api_key)
    if problem:
        return ProbeResult(False, "config", problem, 0)
    image = base64.b64encode(_PNG_1X1).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "只回一个字：ok"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                ],
            }
        ],
        "max_tokens": 8,
    }
    started = time.perf_counter()
    return _run(_endpoint(base_url), payload, api_key, timeout, started, "OCR 模型")
