"""L5 校验节点：对最终答案做引用校验 + PII 脱敏，写 `verify`。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.context import bind_node_run, current_run  # 当前 run（发事件）
from app.agent.events import verify_report  # 校验结论事件
from app.agent.routing.verify import mask_pii, strip_invalid, verify  # 校验

__all__ = ["verify_node"]  # 导出


@bind_node_run
def verify_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """校验答案；越界角标删除；覆盖不足给一句说明（不静默）。"""
    del config  # 不用图配置
    answer = str(state.get("answer") or "")  # 答案
    evidence = [item for item in (state.get("fragments") or []) if isinstance(item, dict)]  # 引用片段
    result = verify(answer, evidence)  # 校验
    run = current_run()  # 事件出口
    if run is not None:  # 发给前端（角标标灰/失效）
        run.emit(  # 事件
            verify_report(  # 结论
                valid=result.valid,  # 合法
                invalid=result.invalid,  # 越界
                missing=result.missing,  # 未引
                coverage=result.coverage,  # 覆盖率
                reasons=result.reasons,  # 原因
                unsupported=result.unsupported,  # 无支撑
            )  # 结束
        )
    out: dict[str, Any] = {"verify": result.model_dump(mode="json"), "answer": mask_pii(answer)}  # 写回
    if result.invalid:  # 越界角标
        out["answer"] = strip_invalid(mask_pii(answer), len(evidence))  # 删掉
    if not result.passed and evidence:  # 未通过但有证据
        out["note"] = "部分内容缺乏证据支撑，请以引用资料为准。"  # 说明
    return out  # 返回