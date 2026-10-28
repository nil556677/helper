"""查询归一化（**纯规则，不调用任何模型**）：只改写法，不改词。

做四件事：全半角、空白、日期、中文数字。
不做：同义替换（那是词表的事）、句式改写（那是模型的事）。
"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 读词表
import re  # 正则
import unicodedata  # 全半角
from pathlib import Path  # 词表路径

_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")  # 2024年3月15日
_SPACE = re.compile(r"\s+")  # 连续空白

#: 同义词表路径（可选；不存在就当没有）。
SYNONYMS_FILE = Path("data") / "synonyms.json"  # 词表

#: 词表缓存：mtime 变了才重读。
_SYNONYMS: dict[str, list[str]] = {}  # 词 -> 同义
_SYNONYMS_MTIME: float = -1.0  # 上次读取时间


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


def synonyms() -> dict[str, list[str]]:  # 读词表
    """按 mtime 缓存；没有文件返回空表。"""
    global _SYNONYMS, _SYNONYMS_MTIME  # 改模块级
    try:  # 没有文件/坏文件都不该炸
        mtime = SYNONYMS_FILE.stat().st_mtime  # 修改时间
    except OSError:  # 文件不存在
        return {}  # 空表
    if mtime == _SYNONYMS_MTIME:  # 没变
        return _SYNONYMS  # 用缓存
    try:  # 解析
        raw = json.loads(SYNONYMS_FILE.read_text(encoding="utf-8"))  # JSON
    except (OSError, ValueError):  # 坏文件
        return {}  # 空表
    table: dict[str, list[str]] = {}  # 结果
    if isinstance(raw, dict):  # 期望 {词: [同义...]}
        for key, value in raw.items():  # 逐条
            words = [str(item) for item in value] if isinstance(value, list) else []  # 列表
            if key and words:  # 有效
                table[str(key)] = words  # 双向补全
                for word in words:  # 反向
                    table.setdefault(word, []).append(str(key))  # 互相同义
    _SYNONYMS, _SYNONYMS_MTIME = table, mtime  # 更新缓存
    return table  # 返回


def variant_terms(text: str, *, limit: int = 2) -> list[str]:  # 词表扩展
    """按词表把查询里的术语换成同义说法，最多返回 `limit` 条变体。"""
    table = synonyms()  # 词表
    if not table:  # 没词表
        return []  # 无变体
    variants: list[str] = []  # 结果
    for word, alternatives in table.items():  # 逐词
        if word not in text:  # 没出现
            continue  # 下一个
        for alternative in alternatives:  # 逐个同义
            candidate = text.replace(word, alternative)  # 替换
            if candidate != text and candidate not in variants:  # 新的
                variants.append(candidate)  # 记
            if len(variants) >= limit:  # 够了
                return variants  # 返回
    return variants  # 返回
