"""天气工具：按统一范式编写 —— 异步纯函数返回 data；网络访问统一走 `app.core.http_client`。

错误约定：城市/日期等业务错抛 `ToolError`；网络/上游瞬时错由共享 HTTP 客户端重试，仍失败则让它
冒泡给 `@tool_spec` 转成 execution.failed(可重试)；编程 Bug 由 tool_spec 判为 internal.error。
"""

from __future__ import annotations  # 延迟求值注解

import re  # 日期格式校验
import time  # 时间戳
from datetime import datetime  # 日期合法性校验
from typing import Any, Literal  # 任意类型 / 枚举

from pydantic import BaseModel, Field  # 参数契约（会随 args_schema 发给模型）

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.tooling import ToolError, tool_spec  # 统一范式
from app.core.http_client import aget_json  # 共享 HTTP 客户端

_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"  # 城市转经纬度
_FORECAST = "https://api.open-meteo.com/v1/forecast"  # 天气

#: WMO 天气编码 -> 中文描述（Open-Meteo 用此编码）。
_WMO: dict[int, str] = {  # 编码表
    0: "晴",  # 晴
    1: "大部晴朗",  # 少云
    2: "多云",  # 多云
    3: "阴",  # 阴
    45: "雾",  # 雾
    48: "冻雾",  # 冻雾
    51: "小毛毛雨",  # 零星毛毛雨
    53: "毛毛雨",  # 毛毛雨
    55: "大毛毛雨",  # 浓密毛毛雨
    61: "小雨",  # 小雨
    63: "中雨",  # 中雨
    65: "大雨",  # 大雨
    80: "小阵雨",  # 小阵雨
    81: "中阵雨",  # 中阵雨
    82: "大阵雨",  # 强阵雨
    95: "雷暴",  # 雷暴
    96: "雷暴伴小冰雹",  # 雷暴伴轻冰雹
    99: "雷暴伴大冰雹",  # 雷暴伴重冰雹
}  # 编码表结束


async def _geocode(city: str) -> tuple[float, float] | None:  # 城市 -> (lat, lon)
    """按城市名查经纬度（走共享客户端）；查不到返回 None。"""
    data = await aget_json(  # 共享异步 GET
        _GEOCODE,  # 地理编码
        params={"name": city, "count": 1, "language": "zh", "format": "json"},  # 查询参数
    )  # 请求结束
    results = data.get("results") or []  # 命中列表
    if not results:  # 没命中
        return None  # 无经纬度
    row = results[0]  # 取第一条
    return float(row["latitude"]), float(row["longitude"])  # 经纬度


def _code_text(code: int) -> str:  # 天气编码转中文
    """认不出就回退为未知。"""
    return _WMO.get(int(code), "未知")  # 查表或回退


def _daily_fields(daily: dict[str, Any], today: str) -> dict[str, Any] | None:  # 取某日字段
    """从 daily 数组取指定日期的结构化字段；**仅表示"格式已校验但不在预报窗口"**（返回 None）。"""
    index = list(daily.get("time") or [])  # 日期列表
    if today not in index:  # 不在预报窗口
        return None  # 无（调用方据此报"不在窗口"）
    i = index.index(today)  # 当天下标
    code = _code_text(daily["weather_code"][i])  # 天气
    high = daily["temperature_2m_max"][i]  # 最高温
    low = daily["temperature_2m_min"][i]  # 最低温
    return {  # 结构化
        "date": today,  # 日期
        "weather": code,  # 天气描述
        "tmin_c": low,  # 最低温
        "tmax_c": high,  # 最高温
        "text": f"{today} {code}，最高 {high}℃，最低 {low}℃",  # 一句话
    }  # 返回


#: 参数契约：字段说明只写 Field(description=...)，不写行尾注释（模型只能读到 description）。
class WeatherQuery(BaseModel):  # 参数契约
    city: str = Field(  # 城市参数
        min_length=1,  # 空串直接判格式错，不浪费一次网络请求
        description="要查询天气的城市名，如 北京、上海、成都",  # 给模型的说明
    )  # 字段结束
    mode: Literal["current", "forecast"] = Field(  # 分支参数（显式化，避免空串隐式语义）
        default="current",  # 默认实时
        description="current=当前实时天气；forecast=按 date 查预报（窗口为未来 7 天）",  # 给模型的说明
    )  # 字段结束
    date: str = Field(  # 日期参数
        default="",  # 默认空
        description="forecast 时必填，格式 YYYY-MM-DD，须在未来 7 天预报窗口内；mode=current 时忽略",  # 给模型的说明
    )  # 字段结束


@tool_spec(  # 统一范式 + 5 分钟幂等缓存
    description="查询指定城市当前天气或未来 7 天内的某日预报。",  # 能力说明
    args_schema=WeatherQuery,  # 参数契约
    ttl=300.0,  # 同参 5 分钟内幂等
)  # 装饰结束
async def query_weather(city: str, mode: str = "current", date: str = ""):  # 异步纯函数（返回 data）
    """成功返回 data；业务错抛 ToolError，网络错冒泡给共享客户端/tool_spec。"""
    city = str(city or "").strip()  # 去空白
    if not city:  # 空城市
        raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), "城市不能为空")  # 业务错（不请求网络）
    if str(mode or "current") not in ("current", "forecast"):  # 非法模式
        raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), "mode 只能是 current 或 forecast")  # 业务错
    if mode == "forecast":  # 预报分支
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(date or "")):  # 格式先行校验
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), "日期格式须为 YYYY-MM-DD（例如 2026-11-10）")  # 格式错
        try:  # 校验真实日期
            datetime.strptime(str(date), "%Y-%m-%d")  # 解析
        except ValueError:  # 非法日期
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"日期无效：{date}")  # 业务错
    coord = await _geocode(city)  # 定位（共享客户端可重试网络错）
    if coord is None:  # 城市找不到
        raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"未能定位到城市「{city}」")  # 业务错
    lat, lon = coord  # 解包
    params: dict[str, Any] = {  # 查询参数
        "latitude": lat,  # 纬度
        "longitude": lon,  # 经度
        "timezone": "Asia/Shanghai",  # 时区
        "forecast_days": 7,  # 窗口 7 天
    }  # 参数结束
    if mode == "forecast":  # 指定日期 -> 每日字段
        params["daily"] = "weather_code,temperature_2m_max,temperature_2m_min"  # 每日字段
    else:  # 没指定 -> 实时
        params["current_weather"] = True  # 实时字段
    data = await aget_json(_FORECAST, params=params)  # 请求天气（共享客户端重试，仍失败冒泡）
    if mode == "forecast":  # 定日
        fields = _daily_fields(data.get("daily") or {}, str(date))  # 取字段（格式已校验）
        if fields is None:  # 只有"不在窗口"会到这里
            raise ToolError(str(ErrorCode.PROTOCOL_ARG_INVALID), f"日期 {date} 不在未来 7 天预报范围内")  # 业务错
        return {"city": city, "date": str(date), "type": "forecast", "ts": int(time.time()), "temperature_c": None, "windspeed_kmh": None, **fields}  # 统一结构（含 ts；缺失字段置空）
    current = data.get("current_weather") or {}  # 实时
    text = f"{city} 当前 {_code_text(current.get('weathercode'))}，气温 {current.get('temperature')}℃，风速 {current.get('windspeed')} km/h"  # 人话
    return {  # 实时固定结构
        "city": city,  # 城市
        "date": "",  # 无日期
        "type": "current",  # 类型
        "weather": _code_text(current.get("weathercode")),  # 天气
        "temperature_c": current.get("temperature"),  # 气温
        "windspeed_kmh": current.get("windspeed"),  # 风速
        "tmin_c": None,  # 实时无最低温
        "tmax_c": None,  # 实时无最高温
        "ts": int(time.time()),  # 查询时间戳
        "text": text,  # 人话文本
    }  # 返回