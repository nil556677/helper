"""S2：答题智能体的任务规则（迁移自旧 `nodes/answer/prompt.py`）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.contexts.prompts._types import define  # 模板定义器

TEMPLATE = define(  # 答题规则
    version=1,  # 版本
    template=(  # 正文
        "规则：\n"
        "1. 每条结论后用 [n] 标注来源，n 必须是资料里出现的编号。\n"
        "2. 资料不足就直说「资料里没有足够信息」，不要编。\n"
        "3. 若资料答案与你的通用知识冲突：仍按资料回答，并在最后另起一行写「[[资料冲突]] 简要说明」。\n"
        "4. 资料里若有「正确答案」字段，选择题以该字段为准，不要用外部知识改判。"
    ),
)  # 模板结束
