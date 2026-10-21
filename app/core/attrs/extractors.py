"""规则提取器：每个字段一个函数，返回归一化后的属性条目列表。

**契约**：每个提取器返回 ``list[dict]``，每条至少含 ``{"key", "value"}``；
``value`` 是**归一化值**（金额 float、日期 ISO 串），可带 ``unit/raw/epoch`` 兄弟键。
提取器只负责"从文本找出来"，**分槽/去重/校验**在 :mod:`app.core.attrs.api` 统一做。

依赖：``regex``（标准库 re）+ ``cn2an``（中文数字）。
"""

from __future__ import annotations

import re

from app.core.attrs.normalize import date_values, money_values


def extract_amounts(text: str) -> list[dict]:
    """金额：中文大写 / 符号金额，归一化为数值 + 币种。"""
    return [
        {"key": "amount", "value": value, "unit": "CNY", "raw": raw}
        for value, raw in money_values(text)
    ]


def extract_dates(text: str) -> list[dict]:
    """日期：中文日期 / ISO 日期，归一化为 ISO 串 + epoch 秒。"""
    return [
        {"key": "date", "value": iso, "epoch": epoch, "raw": raw}
        for iso, epoch, raw in date_values(text)
    ]


def extract_clause_nos(text: str) -> list[dict]:
    """条款号：``第X条`` → 阿拉伯数字字符串（支持小写/大写中文数字）。"""
    out: list[dict] = []
    pattern = r"第\s*([一二三四五六七八九十百零\d壹贰叁肆伍陆柒捌玖拾佰仟]+)\s*条"
    for match in re.finditer(pattern, text):
        number = _cn_positive_int(match.group(1))
        if number is None:
            continue
        out.append({"key": "clause_no", "value": str(number)})
    return out


def extract_parties(text: str) -> list[dict]:
    """当事人：``甲方：xxx公司`` → ``"甲方=xxx公司"``。"""
    pattern = (
        r"(甲方|乙方)[：:]\s*([\u4e00-\u9fa5A-Za-z0-9（）()]+?"
        r"(?:有限(?:责任)?公司|控股|集团|事务所|单位))"
    )
    return [
        {"key": "party", "value": match.group(1) + "=" + match.group(2)}
        for match in re.finditer(pattern, text)
    ]


def extract_codes(text: str) -> list[dict]:
    """编号/标准号：``GB/T 1234`` 这类，去空白。"""
    out: list[dict] = []
    for match in re.finditer(r"\b([A-Z]{2,4}\s*/\s*[A-Za-z0-9\-]+(?:\s+\d+)?)", text):
        out.append({"key": "code", "value": re.sub(r"\s+", "", match.group(1))})
    return out


#: 中文数字元字符集（与 normalize 里的金额共用同一批汉字）。
_QUANT_NUMERAL = r"零一二三四五六七八九十百千万亿点〇两壹贰叁肆伍陆柒捌玖拾佰仟"
#: 常用量词（跟在数字后构成"数量"）；顺序无所谓，匹配任一即可。
_QUANT_UNIT = r"个台件套条部名人人次层年月日周天小时辆把双张页篇段落批家千克克吨公斤立方米平方米倍"
#: 数量匹配：中文数字 + 量词；**负向断言排除"第X条"**（那是条款号，不是数量）。
_QUANT_RE = re.compile(
    rf"(?<!第)([{_QUANT_NUMERAL}]+)([{_QUANT_UNIT}])"
)


def extract_quantities(text: str) -> list[dict]:
    """数量/个数：``三个``→3、``十个人``→10、``五台``→5、``两个``→2。

    只收**确定数字 + 量词**：``几个``/``若干``/``几十`` 这类模糊数没有确定值，
    ``cn2an`` 也抛错，直接丢弃。数字本身不带量词时（孤立"三"）不算数量，避免噪声。
    """
    out: list[dict] = []
    for match in _QUANT_RE.finditer(text):
        number = _cn_positive_int(match.group(1))
        if number is None:
            continue
        out.append({"key": "quantity", "value": int(number), "raw": match.group(0)})
    return out


def _cn_positive_int(text: str) -> int | None:
    """中文/阿拉伯数字 → 正整数；畸形组合返回 ``None``。"""
    import cn2an

    try:
        return int(cn2an.cn2an(text, "smart"))
    except Exception:
        return None


#: 注册表（字段 → 提取函数）。加字段只在这里加一行 + 字段字典。
EXTRACTORS = {
    "amount": extract_amounts,
    "date": extract_dates,
    "clause_no": extract_clause_nos,
    "party": extract_parties,
    "code": extract_codes,
    "quantity": extract_quantities,
}
