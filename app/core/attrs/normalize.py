"""归一化：金额 → 数值 + 币种；日期 → ISO8601 + epoch。

**为什么必须归一化**（见 ``docs/动态字段与存储边界改造方案.md`` §4.3）：
- 原样存 ``"人民币壹佰万元"`` / ``"2024年1月1日"`` 无法做范围比较；
- 查询侧要的是可比较的值（``amount > 500000``、``date between …``）。

**双存**：归一化值进「可过滤字段」，原文进 ``raw`` 兄弟键（展示/审计用）。
日期用 ``epoch``（UTC 零点秒）做范围、``value`` 存 ISO 串做精确/展示。

依赖：``regex``（标准库 re）+ ``cn2an``（中文数字）。
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re

import cn2an

#: 中文金额：人民币 + 中文数字（含大写壹贰叁…）+ **必带单位**。
#: 单位必带是为了**不把量词当钱**：``人民币三个`` 的 "个" 不是单位 → 不提取；
#: ``人民币三元`` 的 "元" 是单位 → 提 3。``万亿`` 必须在 ``万`` 之前匹配。
_CN_MONEY = re.compile(
    r"人民币\s*([零一二三四五六七八九十百千万亿点〇两"
    r"壹贰叁肆伍陆柒捌玖拾佰仟]+)\s*(万亿|亿|万|元|圆)"
)

#: 符号金额：¥ / ￥ / RMB + 阿拉伯数字（允许千分位与小数）。
_SYMBOL_MONEY = re.compile(r"(?:¥|￥|RMB)\s*([\d][\d,]*\.?\d*)")

#: 中文日期：``2024年3月15日``（日可省）。
_CN_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")

#: ISO 日期：``2024-03-15``。
_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

#: 中文数字单位 → 倍数。``万亿`` 要单独有，否则会先被 ``万`` 命中（少四个数量级）。
_UNIT_MULT = {"万亿": 10**12, "亿": 100000000, "万": 10000, "元": 1, "圆": 1}


def _cn_number(text: str) -> int | float | None:
    """``cn2an`` 的容错壳：畸形组合抛异常时返回 ``None`` 跳过，不拖垮整批。"""
    try:
        return cn2an.cn2an(text, "smart")
    except Exception:
        return None


def money_values(text: str) -> list[tuple[float, str]]:
    """从正文抽金额，返回 ``[(归一化数值, 原文)]``（去重由上层按 key+value 做）。"""
    out: list[tuple[float, str]] = []
    for match in _CN_MONEY.finditer(text):
        number = _cn_number(match.group(1))
        if number is None:
            continue
        multiplier = _UNIT_MULT.get(match.group(2) or "", 1)
        out.append((float(number) * multiplier, match.group(0)))
    for match in _SYMBOL_MONEY.finditer(text):
        out.append((float(match.group(1).replace(",", "")), match.group(0)))
    return out


def date_values(text: str) -> list[tuple[str, int, str]]:
    """从正文抽日期，返回 ``[(ISO 日期, epoch 秒, 原文)]``。非法日期跳过。"""
    out: list[tuple[str, int, str]] = []
    for match in _CN_DATE.finditer(text):
        _append_date(out, match.group(1), match.group(2), match.group(3), match.group(0))
    for match in _ISO_DATE.finditer(text):
        _append_date(out, match.group(1), match.group(2), match.group(3), match.group(0))
    return out


def _append_date(
    out: list[tuple[str, int, str]], year: str, month: str, day: str, raw: str
) -> None:
    """校验并追加一条日期；非法值（2024-02-30 / 2024-13-40）直接跳过。

    **用 ``datetime.date`` 校验而非 ``calendar.timegm``**：后者会把越界日**静默滚到
    下一个月**（2024-02-30 → 2024-03-01），把非法日期存成错误的可检索值，比失败更糟。
    """
    try:
        y, m, d = int(year), int(month), int(day)
        _dt.date(y, m, d)  # 非法日期抛 ValueError
        epoch = int(calendar.timegm((y, m, d, 0, 0, 0, 0, 0, 0)))
    except (ValueError, OverflowError):
        return
    out.append((f"{y:04d}-{m:02d}-{d:02d}", epoch, raw))
