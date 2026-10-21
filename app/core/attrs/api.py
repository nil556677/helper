"""动态字段的对外 API：**提取 → 校验 → 合并进 payload**（唯一实现）。

设计见 ``docs/动态字段与存储边界改造方案.md`` §4。调用方只认这里的
:func:`build_attrs` / :func:`enrich`，不要各自再写一份提取。

**不变式**：
- 纯函数：只看 ``text``，不看库、不看时间，重复调用结果一致（幂等）；
- 类型校验：值类型与字段字典不符的条目**丢弃**（不阻断入库）；
- 去重：同一 ``key=value`` 只记一次。
"""

from __future__ import annotations

from app.core.attrs.extractors import EXTRACTORS
from app.core.attrs.field_dict import FIELD_DICT, AttrSpec
from app.core.attrs.version import ATTRS_VERSION


def _is_valid(item: object, spec: AttrSpec) -> bool:
    """按字段字典校验一条属性；类型不符返回 ``False``（调用方丢弃）。"""
    if not isinstance(item, dict) or "key" not in item or "value" not in item:
        return False
    value = item["value"]
    if spec.type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec.type == "date":
        return isinstance(value, str) and isinstance(item.get("epoch"), int)
    return isinstance(value, str)


def build_attrs(text: str) -> dict[str, list]:
    """从正文提取动态属性，按字段字典分进 ``attrs*`` 数组。

    参数：
        text: 正文（**原文**，不是拼了标题的 ``embed_text``）。
    返回：
        ``{"attrs": [...], "attrs_num": [...], "attrs_flat": [...], "attrs_date": [...]}``；
        空槽不出现；条目已去重、已通过类型校验。
    """
    out: dict[str, list] = {}
    for field, extractor in EXTRACTORS.items():
        spec = FIELD_DICT.get(field)
        if spec is None or not spec.enabled:
            continue
        seen: set[str] = set()
        for item in extractor(text):
            if not _is_valid(item, spec):
                continue
            marker = f"{item['key']}={item['value']}"
            if marker in seen:
                continue
            seen.add(marker)
            if spec.slot == "attrs_flat":
                out.setdefault(spec.slot, []).append(f"{item['key']}={item['value']}")
            else:
                out.setdefault(spec.slot, []).append(item)
    return out


def extract(text: str) -> dict[str, list]:
    """``build_attrs`` 的别名（查询侧/其他模块语义更直观）。"""
    return build_attrs(text)


#: 动态字段落进的四个 payload 槽。
_SLOTS = ("attrs", "attrs_num", "attrs_flat", "attrs_date")


def apply_attrs(payload: dict, attrs: dict[str, list]) -> dict:
    """把**已提取好**的属性合并进 payload（清空旧槽 + 打版本）。

    供批量写入复用同一份提取结果：同文本的多条记录只 `build_attrs` 一次，
    再各自 `apply_attrs`，不会漏掉后续记录。
    """
    for slot in _SLOTS:
        if slot in payload:
            payload[slot] = []
    payload.update(attrs)
    payload["attrs_version"] = ATTRS_VERSION
    return payload


def enrich(payload: dict, text: str) -> dict:
    """把动态字段合并进 payload，并打上 ``attrs_version``。

    **在真正存储之前调用**（由 ``app.core.vector.writer.store_records`` 统一做）。
    原地修改并返回 ``payload``；``text`` 为空时只打版本、不提取。
    """
    return apply_attrs(payload, build_attrs(text) if text else {})
