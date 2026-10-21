"""把字段字典**编译**成三份消费物（单一真源，三个消费者）。

设计见 ``docs/动态字段与存储边界改造方案.md`` §4.2 与 §6：
- :func:`filter_spec` —— 查询侧（P2 检索器）校验"能不能过滤、用什么算子"；
- :func:`json_schema` —— LLM function-calling 的受约束输出 schema；
- :func:`facet_spec` —— UI/facet 的可用字段清单。

**本模块只生成声明，不消费**：检索器与 UI 的实现在后续阶段按这些契约落地。
"""

from __future__ import annotations

from app.core.attrs.field_dict import FIELD_DICT

#: 字段类型 → JSON Schema 类型。
_JSON_TYPES = {"float": "number", "date": "string", "keyword": "string", "string": "string"}


def filter_spec() -> dict[str, dict]:
    """查询侧能力清单：``{field: {type, slot, ops, aliases}}``。"""
    return {
        key: {
            "type": spec.type,
            "slot": spec.slot,
            "ops": list(spec.ops),
            "aliases": list(spec.aliases),
        }
        for key, spec in FIELD_DICT.items()
        if spec.enabled
    }


def json_schema() -> dict:
    """LLM tool-calling 用的 JSON Schema（只含 enabled 字段）。"""
    properties = {
        key: {"type": _JSON_TYPES.get(spec.type, "string"), "description": "、".join(spec.aliases)}
        for key, spec in FIELD_DICT.items()
        if spec.enabled
    }
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


def facet_spec() -> list[dict]:
    """UI/facet 可用字段清单：``[{field, slot, aliases}]``。"""
    return [
        {"field": key, "slot": spec.slot, "aliases": list(spec.aliases)}
        for key, spec in FIELD_DICT.items()
        if spec.enabled
    ]
