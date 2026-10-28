"""过程组的展示规则（照 DSH 的 work-details「标准」档）。

**固定标准档**：完成的一轮默认折起（标题是步骤名 + 耗时），点开才看到
步骤明细与推理预览；不再提供档位切换。规则本身与 DSH 的
``presentation-policy.js`` 的 ``standard`` 一项一致：

- ``fold_completed = True`` —— 完成轮折起；
- ``step_grouping = collapsed`` —— 步骤归在折起的组里；
- ``reasoning = True`` —— 组里显示推理预览。

本文件只放**纯数据与规则**（不 import 界面库），聊天页拿它拼标题，
测试也可以直接断言。
"""

from __future__ import annotations

from app.ui.state.chat_model import Process


def process_title(process: Process) -> str:
    """过程组标题：步骤名最多列 3 个（多了加省略号）+ 总耗时。

    失败的一轮直接写失败文案，不摆步骤名——和"成功"必须一眼分得开。
    """
    duration = f" · {process.duration_ms:.0f} ms" if process.duration_ms is not None else ""
    if any(step.status == "failed" for step in process.steps):
        return f"{process.done}{duration}"
    titles = [step.title for step in process.steps]
    if not titles:
        return f"{process.done}{duration}"
    joined = "、".join(titles[:3])
    if len(titles) > 3:
        joined += "…"
    return f"{joined}{duration}"
