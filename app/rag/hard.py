"""硬要素抽取：条款号 / 金额 / 日期（**纯规则，不调用任何模型**）。

为什么单独一层：门限需要"可解释、可单测"的强信号。用户问"第 3.2 条"，
检索结果里连 `3.2` 都没有，那不是"分数低一点"，而是**根本没找对**——应当直接拒，
而不是靠一个学出来的分数去猜。

两类要素的强度不同（见 :func:`missing_elements`）：
- **条款号 / 日期**：必须字面命中，缺了就是硬失败；
- **金额**：也可能写成中文数字（三十万），抽取拿不到，所以缺了只降级、不直接拒。
"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 正则

#: 条款号：`第 3.2 条` / `3.2 款`。
_CLAUSE = re.compile(r"第\s*(\d+(?:\.\d+)*)\s*[条款项章节]")  # 带"第…条"
_CLAUSE_BARE = re.compile(r"(?<![\d.])(\d+(?:\.\d+){1,2})\s*[条款项]")  # 裸的 3.2 条

#: 金额：`3 万` / `300000 元` / `1.5 亿`。**单位按最长优先**（万亿→亿元→万元→亿→万→元），
#: 否则 `3万亿元` 会先贪到「万」、剩下「亿元」被丢，量级错 8 个数量级。
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(万亿元|亿元|万元|万亿|亿|万|元)")  # 数字+单位（最长在前）

#: 日期：`2024-03` / `2024年3月15日` / `2024/3/15`。
_DATE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*(?:[-/月]\s*(\d{1,2}))?")  # 年月日

#: 金额单位换算（**元**）。
_UNIT = {"万亿元": 1000000000000, "亿元": 100000000, "万亿": 1000000000000, "亿": 100000000, "万元": 10000, "万": 10000, "元": 1}  # 倍率


def extract(text: str) -> dict[str, list[str]]:  # 抽取
    """抽出三类要素（去重保序）：`{"clause": [...], "amount": [...], "date": [...]}`。

    统一归一在前（全半角/中文数字/日期/空白），让「五万 / 5万 / 50000」与「2024年3月」对齐。
    """
    from app.rag.normalize import normalize  # 统一归一（入库同源）

    body = normalize(text)  # 归一后再抽（与入库侧同一套）
    clauses: list[str] = []  # 条款号
    for pattern in (_CLAUSE, _CLAUSE_BARE):  # 两种写法
        for match in pattern.finditer(body):  # 逐个
            value = match.group(1).strip(".")  # 去尾点
            if value and value not in clauses:  # 不重复
                clauses.append(value)  # 记录
    amounts: list[str] = []  # 金额
    for match in _AMOUNT.finditer(body):  # 逐个
        number = float(match.group(1))  # 数字
        unit = match.group(2)  # 单位
        value = str(round(number * _UNIT[unit]))  # 归一成"元"（round 已返回 int）
        if value not in amounts:  # 不重复
            amounts.append(value)  # 记录
    dates: list[str] = []  # 日期
    for match in _DATE.finditer(body):  # 逐个
        year, month, day = match.group(1), match.group(2), match.group(3)  # 三段
        value = f"{year}-{int(month):02d}"  # 至少到月
        if day:  # 有日
            value = f"{value}-{int(day):02d}"  # 补到日
        if value not in dates:  # 不重复
            dates.append(value)  # 记录
    return {"clause": clauses, "amount": amounts, "date": dates}  # 返回


def missing_elements(question: str, texts: list[str]) -> tuple[list[str], list[str]]:  # 缺什么
    """返回 `(硬缺, 软缺)`：硬缺 = 条款号/日期没命中；软缺 = 金额没命中。"""
    wanted = extract(question)  # 问题里的要素
    found = extract("\n".join(str(item or "") for item in texts))  # 证据里的要素
    hard = [value for value in [*wanted["clause"], *wanted["date"]] if value not in found["clause"] and value not in found["date"]]  # 条款/日期缺失
    soft = [value for value in wanted["amount"] if value not in found["amount"]]  # 金额缺失
    return hard[:5], soft[:5]  # 只报前几个，够界面显示
