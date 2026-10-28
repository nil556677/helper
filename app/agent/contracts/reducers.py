"""状态 channel 的合并规则：一种语义一个 reducer，图里不再手写合并。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型


def _merge_one(  # 合并一条
    item: Any,  # 待合并项
    merged: dict[Any, dict[str, Any]],  # key -> 合并结果
    order: list[Any],  # 保序的 key 列表
    tail: list[Any],  # 无 key 项
    key: str,  # 主键字段
) -> None:  # 无返回
    """有 key 按 last-write-wins，无 key 原样接尾。"""
    if not isinstance(item, dict) or key not in item:  # 非字典或没有主键
        tail.append(item)  # 原样接尾
        return  # 结束
    value = item[key]  # 主键值
    if value not in merged:  # 第一次见
        order.append(value)  # 记录顺序
        merged[value] = dict(item)  # 拷贝保存
    else:  # 已存在
        merged[value].update(item)  # 覆盖字段（last-write-wins）


def merge_by_id(  # 按 id 合并
    current: list[Any],  # 旧值
    incoming: list[Any],  # 新值
    key: str = "id",  # 主键字段
) -> list[Any]:  # 合并结果
    """后到覆盖先到，保序，无 key 项接尾。"""
    merged: dict[Any, dict[str, Any]] = {}  # key -> 合并结果
    order: list[Any] = []  # 保序 key
    tail: list[Any] = []  # 无 key 项
    for item in current:  # 先合并旧值
        _merge_one(item, merged, order, tail, key)  # 并入
    for item in incoming:  # 再合并新值
        _merge_one(item, merged, order, tail, key)  # 并入
    result = [merged[value] for value in order]  # 按序取有 key 项
    result.extend(tail)  # 接无 key 项
    return result  # 返回


def merge_usage(  # 合并用量
    current: dict[str, Any] | None,  # 旧值
    incoming: dict[str, Any] | None,  # 新值
) -> dict[str, Any]:  # 合并结果
    """数值累加，其余字段覆盖。"""
    result = dict(current or {})  # 从旧值起步
    for key, value in (incoming or {}).items():  # 逐字段
        if isinstance(value, (int, float)) and isinstance(result.get(key), (int, float)):  # 两边都是数
            result[key] = result[key] + value  # 累加
        else:  # 非数值或首见
            result[key] = value  # 覆盖
    return result  # 返回
