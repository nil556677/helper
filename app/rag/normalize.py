"""查询归一化（**纯规则，不调用任何模型**）：只改写法，不改词。

做四件事：全半角、空白、日期、中文数字。
不做：同义替换（那是词表的事）、句式改写（那是模型的事）。
"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 正则
import unicodedata  # 全半角

_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")  # 2024年3月15日
_SPACE = re.compile(r"\s+")  # 连续空白


def normalize(text: str) -> str:  # 归一化
    """全半角 + 空白 + 日期 + 中文数字。"""
    body = unicodedata.normalize("NFKC", str(text or ""))  # 全角转半角
    body = _fold_dates(body)  # 日期统一成 2024-03-15
    body = _fold_numbers(body)  # 中文数字转阿拉伯
    return _SPACE.sub(" ", body).strip()  # 折叠空白


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
