"""统一文本归一化（**纯规则，不调用模型**）：入库与查询共用同一套，保证词项/数字对齐。

做四件事：全半角(NFKC)、空白、日期、中文数量词。
不做：同义替换（那是词表的事）、句式改写（那是模型的事）。
"""

from __future__ import annotations  # 延迟值注解

import re  # 正则
import unicodedata  # 全半角

_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")  # 2024年3月15日
_SPACE = re.compile(r"\s+")  # 连续空白

#: 中文数字的"数值词"（不含单位）与"单位词"（分开，单位保留给 hard 换算）。
_NUM = "零〇一二三四五六七八九十百千点"
_UNIT = "万亿亿元万元万亿天月年日元个次%％条吨公斤千克克米厘公里小时分钟秒台套笔家角分"
_QUANTITY = re.compile(rf"([{_NUM}]+)([{_UNIT}]+)")  # 数值词+单位（两段，单位保留）


def normalize(text: str) -> str:  # 归一化
    """全半角 + 空白 + 日期 + 中文数字（数量词）。"""
    body = unicodedata.normalize("NFKC", str(text or ""))  # 全角转半角
    body = _fold_dates(body)  # 日期统一成 2024-03-15
    body = _fold_quantities(body)  # "五万元/三十天" → 数字（定向，单位保留）
    return _SPACE.sub(" ", body).strip()  # 折叠空白


def _fold_quantities(text: str) -> str:  # 定向数量词 → 阿拉伯（保留单位）
    """把「中文数值词+单位」的**数值部分**转成数字：`五万元`→`5万元`、`三十天`→`30天`。

    单位保留在串外，供 `rag.hard` 金额按倍率归一到"元"（`5万` → 50000）。
    只在"数值词+单位"命中时触发，避免把"三无产品"这类词里的单字误转。
    """
    import cn2an  # 数量词转换（依赖已在）

    def _swap(match: re.Match[str]) -> str:  # 单段转换
        number, unit = match.group(1), match.group(2)  # 数值 + 单位
        try:  # cn2an 对怪输入抛
            value = str(cn2an.transform(number, "cn2an"))  # 中文数值 → 阿拉伯数字
        except Exception:  # 转不了
            return match.group(0)  # 原样
        return (value if value != number else number) + unit  # 数值换、单位留

    return _QUANTITY.sub(_swap, text)  # 替换所有数量词


def _fold_dates(text: str) -> str:  # 日期归一
    """`2024 年 3 月 15 日` → `2024-03-15`。"""
    def swap(match: re.Match[str]) -> str:  # 单条替换
        year, month, day = match.group(1), int(match.group(2)), match.group(3)  # 三段
        value = f"{year}-{month:02d}"  # 年到月
        return f"{value}-{int(day):02d}" if day else value  # 补日
    return _DATE.sub(swap, text)  # 替换


def _fold_numbers(text: str) -> str:  # 中文数字
    """把"叁万元""三十"这类转成阿拉伯数字；失败就原样返回。"""
    try:  # cn2an 对怪输入会抛
        import cn2an  # 已在依赖里

        return str(cn2an.transform(text, "cn2an"))  # 转换
    except Exception:  # 转不了
        return text  # 原样
