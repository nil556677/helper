"""L1 时间解析：相对时间词 → 绝对范围（零 LLM）。"""

from __future__ import annotations  # 延迟求值注解

from datetime import datetime, timedelta  # 时间

from app.agent.contracts.triage import TimeRange  # 契约

__all__ = ["resolve_time"]  # 导出


def _day_range(now: datetime, offset_days: int) -> TimeRange:  # 单日范围
    day = (now + timedelta(days=offset_days)).date()  # 目标日
    return TimeRange(start=day.isoformat(), end=day.isoformat(), label=f"{offset_days:+d}天")  # 范围


def resolve_time(query: str, now: datetime | None = None) -> TimeRange | None:  # 解析
    """命中相对时间词就给范围；解析不了返回 None（由分诊触发澄清）。"""
    text = str(query or "")  # 原文
    current = now or datetime.now()  # 当前
    today = current.date()  # 今天
    if "今天" in text or "今日" in text:  # 今天
        return TimeRange(start=today.isoformat(), end=today.isoformat(), label="今天")  # 今天
    if "昨天" in text:  # 昨天
        return _day_range(current, -1)  # 昨天
    if "前天" in text:  # 前天
        return _day_range(current, -2)  # 前天
    if "本周" in text or "这周" in text:  # 本周
        start = today - timedelta(days=today.weekday())  # 周一
        return TimeRange(start=start.isoformat(), end=today.isoformat(), label="本周")  # 范围
    if "上周" in text:  # 上周
        start = today - timedelta(days=today.weekday() + 7)  # 上周一
        end = start + timedelta(days=6)  # 上周日
        return TimeRange(start=start.isoformat(), end=end.isoformat(), label="上周")  # 范围
    if "本月" in text or "这个月" in text:  # 本月
        start = today.replace(day=1)  # 本月一号
        return TimeRange(start=start.isoformat(), end=today.isoformat(), label="本月")  # 范围
    if "上月" in text or "上个月" in text:  # 上月
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)  # 上月一号
        end = today.replace(day=1) - timedelta(days=1)  # 上月末
        return TimeRange(start=start.isoformat(), end=end.isoformat(), label="上月")  # 范围
    if "本季度" in text or "这季度" in text:  # 本季度
        q = (today.month - 1) // 3  # 季度号
        start = today.replace(month=q * 3 + 1, day=1)  # 季初
        return TimeRange(start=start.isoformat(), end=today.isoformat(), label="本季度")  # 范围
    if "上季度" in text:  # 上季度
        q = (today.month - 1) // 3  # 当前季度号
        start_month = (q - 1) * 3 + 1  # 上季初月
        start = today.replace(month=start_month, day=1)  # 上季初
        end = today.replace(month=q * 3, day=1) - timedelta(days=1)  # 上季末
        return TimeRange(start=start.isoformat(), end=end.isoformat(), label="上季度")  # 范围
    if "今年" in text:  # 今年
        return TimeRange(start=today.replace(month=1, day=1).isoformat(), end=today.isoformat(), label="今年")  # 范围
    if "去年" in text:  # 去年
        start = today.replace(year=today.year - 1, month=1, day=1)  # 去年初
        end = today.replace(year=today.year - 1, month=12, day=31)  # 去年末
        return TimeRange(start=start.isoformat(), end=end.isoformat(), label="去年")  # 范围
    return None  # 解析不了
