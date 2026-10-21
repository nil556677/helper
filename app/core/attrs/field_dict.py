"""字段字典（**唯一真源**）：一行一字段，声明类型 / 落哪个 attrs 槽 / 能做什么算子。

设计见 ``docs/动态字段与存储边界改造方案.md`` §4.2。与旧 ``rag/ingest/attrs.py`` 的
``(type, slot, enabled)`` 三元组相比，这里补了：
- ``ops``：查询侧允许的算子（防"对 keyword 做 >"这种非法过滤）；
- ``aliases``：自然语言 → 字段名的桥梁（写进 LLM 的 tool schema）；
- ``scope``：``chunk`` / ``document``（文档级属性下沉预留）；
- ``normalize``：归一化规则名（``money`` / ``date``）。

**改这里必须同步 +1** ``version.ATTRS_VERSION``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttrSpec:
    """一个动态字段的声明。"""

    type: str
    """值类型：``float`` / ``date`` / ``keyword`` / ``string``。"""

    slot: str
    """落哪个 payload 数组：``attrs`` / ``attrs_num`` / ``attrs_flat`` / ``attrs_date``。"""

    enabled: bool = True
    """是否启用（``False`` 的字段不提取、不索引，留作模型字段占位）。"""

    ops: tuple[str, ...] = ("eq",)
    """查询侧允许的算子（``eq/gt/gte/lt/lte/between``）。"""

    aliases: tuple[str, ...] = ()
    """自然语言别名（生成 tool schema / UI 提示用）。"""

    scope: str = "chunk"
    """粒度：``chunk``（块级）或 ``document``（文档级，预留下沉）。"""

    normalize: str = ""
    """归一化规则：``money`` / ``date`` / 空（不归一化）。"""


#: 字段字典（唯一真源）。加字段只在这里加一行 + 在 extractors 里加一个提取器。
FIELD_DICT: dict[str, AttrSpec] = {
    "amount": AttrSpec(
        type="float",
        slot="attrs_num",
        ops=("eq", "gt", "gte", "lt", "lte", "between"),
        aliases=("金额", "价款", "总额", "合同金额"),
        normalize="money",
    ),
    "date": AttrSpec(
        type="date",
        slot="attrs_date",
        ops=("eq", "gt", "gte", "lt", "lte", "between"),
        aliases=("日期", "签订日期", "签发日", "生效日"),
        normalize="date",
    ),
    "clause_no": AttrSpec(
        type="keyword",
        slot="attrs_flat",
        ops=("eq",),
        aliases=("条款号", "条", "条款"),
    ),
    "party": AttrSpec(
        type="string",
        slot="attrs",
        ops=("eq",),
        aliases=("甲方", "乙方", "当事人"),
    ),
    "code": AttrSpec(
        type="keyword",
        slot="attrs_flat",
        ops=("eq",),
        aliases=("编号", "标准号", "合同号"),
    ),
    # 数量：只收"确定数字 + 量词"（三个→3、十人→10）；模糊数（几个/若干）不归一化。
    # 用 attrs_num（数值可 range），type 记 float 以通过 attrs_num 的槽位校验。
    "quantity": AttrSpec(
        type="float",
        slot="attrs_num",
        ops=("eq", "gt", "gte", "lt", "lte", "between"),
        aliases=("数量", "个数", "几个", "三", "十"),
    ),
}

#: 每个槽位只收一种类型 —— 类型靠槽位区分、字段名不带类型（用户定的口径 A）。
#: 这同时保证"同一个 key 不会以两种类型落库"：一个字段一个 slot，slot 类型固定。
_SLOT_TYPE: dict[str, tuple[str, ...]] = {
    "attrs": ("string",),
    "attrs_num": ("float",),
    "attrs_date": ("date",),
    "attrs_flat": ("keyword",),
}


def field_dict_err() -> str | None:
    """校验字段字典的一致性（槽位↔类型、key 唯一）。有错返回原因，没错返回 ``None``。

    不变量：
    - ``slot → 类型`` 关系正确（``attrs_flat`` 只收 keyword 等）；
    - 每个 ``key`` 恰好一个 slot（``FIELD_DICT`` 是 dict，键天然唯一）。
    """
    for key, spec in FIELD_DICT.items():
        allowed = _SLOT_TYPE.get(spec.slot)
        if allowed is None:
            return f"{key}: 未知槽位 {spec.slot!r}"
        if spec.type not in allowed:
            return f"{key}: 槽位 {spec.slot} 期望类型 {allowed}，实际 {spec.type!r}"
    return None
