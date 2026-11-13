"""确认策略：某次工具调用是否需先经用户确认（纯函数，供确认门复用）。

区别于"后台审批"——这里是**用户侧确认**：查敏感/写改前问一句、告知要做什么、留一面记账。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

from app.agent.contracts.tools import Mutation  # 变更画像

__all__ = ["should_require_approval"]  # 导出


def should_require_approval(  # 判定
    descriptor: Any,  # ToolDescriptor（或带 approval/mutation 的对象）
    mutation: str = "",  # 变更画像（兜底）
    args: dict[str, Any] | None = None,  # 调用参数
    *,  # 关键字
    tenant_id: str = "",  # 租户（预留）
    threshold: float = 0,  # 自动审批阈值（预留）
) -> tuple[bool, str]:  # (是否审批, 原因)
    """manual → 必经；auto → 按规则(当前一律放行占位)；mutation 不可逆 → 审；其余不审。"""
    approval = str(getattr(descriptor, "approval", "") or "")  # 确认策略
    if approval == "manual":  # 需用户确认
        return True, "需用户确认后执行"  # 问一句
    if approval == "auto":  # 自动化决策（占位：可加金额阈值）
        amount = float((args or {}).get("amount") or 0) if args else 0.0  # 金额
        if threshold and amount > threshold:  # 超阈值
            return True, f"金额超过自动阈值 {threshold}"  # 审
        return False, ""  # 放行
    use_mutation = str(mutation or getattr(descriptor, "mutation", Mutation.READ_ONLY) or Mutation.READ_ONLY)  # 变更画像
    if use_mutation == Mutation.NON_IDEMPOTENT_WRITE:  # 不可逆写
        return True, "不可逆操作，需用户确认后执行"  # 需确认
    return False, ""  # 不问