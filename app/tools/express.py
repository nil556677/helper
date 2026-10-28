"""快递查询工具：用快递100的公开查询接口（**无需 API Key**）。

先自动识别快递公司（或使用用户给的 company），再查物流轨迹；
接口可能限流/变更，失败一律明确提示，不编造物流。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from app.core.intent import RouteDecision
from app.ui.state.chat_model import Turn

_AUTO_URL = "https://www.kuaidi100.com/autonumber/autoComNum?resultv2=1&text={number}"
_QUERY_URL = (
    "https://www.kuaidi100.com/query?type={carrier}&postid={number}&temp={temp}&phone="
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.kuaidi100.com/",
}

#: 中文名/常见写法 → 快递100 公司编码。
_CARRIERS: dict[str, str] = {
    "顺丰": "shunfeng",
    "中通": "zhongtong",
    "圆通": "yuantong",
    "韵达": "yunda",
    "申通": "shentong",
    "京东": "jd",
    "邮政": "youzhengguonei",
    "EMS": "ems",
    "极兔": "jtexpress",
}

#: 快递100 state → 中文。
_STATES: dict[str, str] = {
    "0": "在途",
    "1": "已揽收",
    "2": "疑难件",
    "3": "已签收",
    "4": "已退签",
    "5": "派件中",
    "6": "已退回",
    "7": "转投",
    "8": "清关",
    "14": "拒签",
}


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return dict(json.loads(response.read().decode("utf-8", "replace")))
    except TimeoutError as exc:
        raise RuntimeError("接口超时，请稍后重试") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise RuntimeError("接口超时，请稍后重试") from exc
        raise RuntimeError(f"网络不可用（{exc.reason}）") from exc
    except OSError as exc:
        raise RuntimeError(f"网络错误（{exc}）") from exc


def _detect_carrier(number: str) -> str:
    data = _get_json(_AUTO_URL.format(number=urllib.parse.quote(number)))
    auto = data.get("auto") or []
    if not auto:
        raise ValueError("识别不出快递公司")
    code = str(auto[0].get("comCode") or "").strip()
    if not code:
        raise ValueError("识别不出快递公司")
    return code


_JUNK = ("查无结果", "暂无", "无轨迹", "单号不存在", "运单不存在")


def _fetch(number: str, carrier: str = "") -> tuple[str, str, list[dict]]:
    """返回 ``(公司编码, 状态, 最近轨迹)``。"""
    code = _CARRIERS.get(carrier, carrier).strip()
    if not code:
        code = _detect_carrier(number)
    data = _get_json(
        _QUERY_URL.format(
            carrier=urllib.parse.quote(code),
            number=urllib.parse.quote(number),
            temp=f"{time.time():.6f}",  # 快递100 用它绕缓存；缺了会慢/超时
        )
    )
    message = str(data.get("message") or "").strip()
    if message and message != "ok":
        raise ValueError(message)
    events = [
        item
        for item in (data.get("data") or [])
        if item.get("context")
        and not any(marker in str(item["context"]) for marker in _JUNK)
    ]
    if not events:
        raise ValueError("没有查到该运单的物流轨迹")
    state = _STATES.get(str(data.get("state")), "状态未知")
    return code, state, events[:3]


def handler(text: str = "", decision: RouteDecision | None = None) -> Turn:
    del text
    slots = decision.slots if decision else {}
    number = str(slots.get("tracking_no") or "").strip()
    carrier = str(slots.get("carrier") or "").strip()
    try:
        code, state, events = _fetch(number, carrier)
    except Exception as exc:
        return Turn(note=f"快递查询失败：{exc}")
    lines = [
        f"{str(item.get('time') or '').strip()} {str(item.get('context') or '').strip()}".strip()
        for item in events
    ]
    return Turn(
        answer=f"运单 {number}（{code}）当前状态：{state}。\n" + "\n".join(lines)
    )
