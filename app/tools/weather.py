"""天气工具：用 **Open-Meteo 免费接口**（无需 API Key）。

先地理编码城市 → 再查当前天气；网络失败/城市找不到都返回明确说明，不编造。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from app.core.config import store as params_store
from app.core.intent import RouteDecision
from app.core.intent.slots import clean_city
from app.ui.state.chat_model import Turn

#: WMO weather code → 中文描述。
_WMO: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return dict(json.loads(response.read().decode("utf-8", "replace")))


def _fetch(city: str) -> tuple[str, float, str]:
    """返回 ``(城市显示名, 温度, 天气描述)``。"""
    query = urllib.parse.quote(city)
    geo = _get_json(
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={query}&count=1&language=zh&format=json"
    )
    results = geo.get("results") or []
    if not results:
        raise ValueError(f"找不到城市「{city}」")
    place = results[0]
    latitude = place["latitude"]
    longitude = place["longitude"]
    name = str(place.get("name") or city)
    forecast = _get_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,weather_code&timezone=auto"
    )
    current = forecast.get("current") or {}
    if "temperature_2m" not in current:
        raise ValueError("接口没有返回温度")
    temperature = float(current["temperature_2m"])
    description = _WMO.get(int(current.get("weather_code", -1)), "天气未知")
    return name, temperature, description


def handler(text: str = "", decision: RouteDecision | None = None) -> Turn:
    del text
    raw = str((decision.slots if decision else {}).get("city") or "").strip()
    if not raw:
        try:
            raw = str(params_store.load()["tools.weather.default_city"] or "").strip()
        except Exception:
            raw = ""
    city = clean_city(raw)
    if not city:
        return Turn(note="请提供有效的城市（例如：北京）。")
    try:
        name, temperature, description = _fetch(city)
    except Exception as exc:
        return Turn(note=f"天气查询失败：{exc}")
    return Turn(answer=f"{name}现在 {temperature:.0f}°C，{description}。")
