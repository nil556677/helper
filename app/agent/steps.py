"""把内部 stage 收敛成用户级步骤（历史/落库共用）：只按 group 合并。

这里**不认识任何具体步骤 id**：分组在 ``stage(...)`` 调用处声明，组名与顺序来自契约层。
旧数据里没有 ``group`` 的步骤原样保留（不猜、不吞），避免历史轮次丢过程。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意

from app.agent.contracts import GROUP_LABELS, GROUP_ORDER  # 分组顺序与组名（唯一事实来源）

_ORDER = {group.value: index for index, group in enumerate(GROUP_ORDER)}  # 分组 → 展示序号
_LABELS = {group.value: label for group, label in GROUP_LABELS.items()}  # 分组 → 组名


def grouped(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:  # 聚合
    """同组合并成一行；未知分组的步骤原样接尾（保持旧行为）。"""
    merged: dict[str, dict[str, Any]] = {}  # 分组 → 合并结果
    loose: list[dict[str, Any]] = []  # 没有分组的旧数据
    for step in steps:  # 逐条
        group = str(step.get("group") or "")  # 分组
        if group not in _ORDER:  # 旧数据 / 未知分组
            loose.append(dict(step))  # 原样保留
            continue  # 下一条
        item = merged.setdefault(  # 建组
            group,  # 分组
            {"group": group, "title": _LABELS[group], "detail": "", "status": "done", "ms": 0},  # 形状
        )  # 建组结束
        if step.get("status") == "failed":  # 有一步失败
            item["status"] = "failed"  # 整组标失败
        item["ms"] = int(item.get("ms") or 0) + int(step.get("ms") or 0)  # 耗时累加
        detail = str(step.get("detail") or "").strip()  # 明细
        if detail and detail not in str(item["detail"]):  # 不重复
            item["detail"] = f"{item['detail']}；{detail}" if item["detail"] else detail  # 拼接
    ordered = sorted(merged.values(), key=lambda item: _ORDER[str(item["group"])])  # 按固定顺序
    return [*ordered, *loose]  # 合并
