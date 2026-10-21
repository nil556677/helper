"""P1.10：**零 LLM** 的精确字段抽取（编号 / 日期 / 金额 / 条款号）。

为什么要有它：向量与词法都只能回答"差不多相关"，但用户经常问的是**精确值**——
"HT-2026-0043 那份合同怎么约定的"、"2026-10-16 之后到期的有几份"。
这类问题用 FTS 的 `MATCH` 命中率很低（编号被切碎、日期格式不一），用向量更是碰运气。
所以：**导入时用正则把精确值抽出来存表**，查询时等值/子串直接走索引。

三条设计：

1. **零 LLM**：纯正则 + 规范化。可解释、可测、毫秒级；
2. **规范化后仍保留 `raw`**：引用要展示原文（"壹拾贰万捌仟元"不能被换成 128000.00 就没人认得了）；
3. **抽取失败静默跳过**：一个怪编号不该让整份资料导入失败——少抽一条，比导不进来强。

**trigram 的坑**（实测）：`codes_fts` 用 `tokenize='trigram'` 才能做**任意子串**匹配，
但 `MATCH` 的查询串**必须加引号**（`"2026-00"`）——`-` 在 MATCH 语法里会被当运算符，
不加引号会报 `no such column: 00`。另外 trigram 最少 3 个字符，更短的查询退回 `LIKE`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.core.parse.clean import compact_ws

#: 条款号：第 3 条 / 第十二条 / 第 2.3 款
CLAUSE = re.compile(r"第\s*[一二三四五六七八九十百零〇\d]{1,4}(?:\.\d{1,2})?\s*[条款项章节]")
#: 编号：HT-2026-0043 / GB/T 1234-2026 / A-1
NUMBER = re.compile(r"[A-Z]{1,6}(?:[-/][A-Z0-9]{1,12}){1,4}|\d{8,}")
#: 日期：2026-10-16 / 2026/10/16 / 2026年10月16日 / 2026.10.16
DATE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?")
#: 金额：¥1,234.50 / 128000 元 / 12.5 万元 / 3 亿元
MONEY = re.compile(r"[¥￥$]\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|元)")

#: 金额单位 → 倍数
_UNITS = {"元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000)}

#: 分类常量（写进 ``codes.kind``）
KIND_CLAUSE = "clause"
KIND_NUMBER = "number"
KIND_DATE = "date"
KIND_MONEY = "money"


@dataclass(frozen=True)
class CodeHit:
    """抽到的一条精确字段。

    参数：
        kind: ``clause`` / ``number`` / ``date`` / ``money``。
        value: **规范化后**的值（查询按它做等值/子串）。
        raw: 原文片段（引用展示用）。
        start / end: 在正文里的字符区间（定位用）。
    """

    kind: str
    value: str
    raw: str
    start: int
    end: int


def normalize_date(year: str, month: str, day: str) -> str:
    """把日期规范成 ``YYYY-MM-DD``（非法日期原样返回 ``Y-M-D``，不编造）。"""
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return f"{year}-{month}-{day}"


def normalize_money(amount: str, unit: str) -> str:
    """把金额规范成"元"为单位的两位小数字符串（用 ``Decimal``，不用 float）。"""
    try:
        value = Decimal(amount.replace(",", "")) * _UNITS.get(unit, Decimal(1))
    except (InvalidOperation, ValueError):
        return amount
    return f"{value.quantize(Decimal('0.01'))}"



def extract(text: str) -> list[CodeHit]:
    """从一段正文里抽精确字段（**同一 (kind, value) 只留第一条**）。

    参数：
        text: 片段正文（通常是 ``chunks.text``）。
    返回：按出现顺序排列的 :class:`CodeHit` 列表；抽不到返回空列表。
    """
    if not text:
        return []
    hits: list[CodeHit] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, raw: str, start: int, end: int) -> None:
        key = (kind, value)
        if not value or key in seen:
            return
        seen.add(key)
        hits.append(CodeHit(kind=kind, value=value, raw=raw, start=start, end=end))

    for match in CLAUSE.finditer(text):
        add(KIND_CLAUSE, compact_ws(match.group(0)), match.group(0), match.start(), match.end())
    for match in NUMBER.finditer(text):
        add(KIND_NUMBER, compact_ws(match.group(0)), match.group(0), match.start(), match.end())
    for match in DATE.finditer(text):
        year, month, day = match.groups()
        add(
            KIND_DATE,
            normalize_date(year, month, day),
            match.group(0),
            match.start(),
            match.end(),
        )
    for match in MONEY.finditer(text):
        prefixed, suffixed, unit = match.groups()
        if prefixed:
            add(KIND_MONEY, normalize_money(prefixed, "元"), match.group(0), match.start(), match.end())
        else:
            add(
                KIND_MONEY,
                normalize_money(suffixed, unit or "元"),
                match.group(0),
                match.start(),
                match.end(),
            )
    hits.sort(key=lambda hit: hit.start)
    return hits
