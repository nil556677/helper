"""L5 校验节点：对最终答案做引用校验 + PII 脱敏，写 `verify`。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.routing.verify import mask_pii, strip_invalid, verify  # 校验

__all__ = ["verify_node"]  # 导出


def verify_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """校验答案；越界角标删除；覆盖不足给一句说明（不静默）。"""
    del config  # 不用图配置
    answer = str(state.get("answer") or "")  # 答案
    evidence = [item for item in (state.get("fragments") or []) if isinstance(item, dict)]  # 引用片段
    result = verify(answer, evidence)  # 校验
    out: dict[str, Any] = {"verify": result.model_dump(mode="json"), "answer": mask_pii(answer)}  # 写回
    if result.invalid:  # 越界角标
        out["answer"] = strip_invalid(mask_pii(answer), len(evidence))  # 删掉
    if not result.passed and evidence:  # 未通过但有证据
        out["note"] = "部分内容缺乏证据支撑，请以引用资料为准。"  # 说明
    return out  # 返回