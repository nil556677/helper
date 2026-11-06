"""L4e 通用兜底节点（general_agent）：纯 LLM、无工具、不闲聊，只做能力引导。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.llm.provider import get_chat_model  # 统一模型入口

__all__ = ["general_agent_node"]  # 导出

_PROMPT = (  # 提示
    "你是资料问答助手的兜底引导。不要闲聊、不要伪造数据或工具能力。"
    "若用户问题超出能力范围，如实说明，并引导其提出可检索资料回答的问题。"
)  # 提示结束


def _degrade() -> dict[str, Any]:  # 无模型降级
    return {"answer": "我是资料问答助手：可以帮你在本地资料中检索并据资料回答。请描述你想查的资料问题。", "note": ""}  # 模板


async def general_agent_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """有模型才生成引导话术；无模型/失败用模板，绝不假装有数据。"""
    del config  # 不用图配置
    from app.agent.contracts import StageGroup  # 分组
    from app.agent.stages import stage  # 过程步

    model = get_chat_model()  # 统一入口
    question = str(state.get("question") or "")  # 问题
    if model is None:  # 无模型
        return _degrade()  # 模板
    try:  # 调模型
        with stage("生成回答", group=StageGroup.GENERATE):  # 过程区可见
            response = await model.ainvoke([("system", _PROMPT), ("human", question)])  # 请求
        text = str(getattr(response, "content", "") or "").strip()  # 正文
    except Exception:  # 失败
        return _degrade()  # 模板
    return {"answer": text or _degrade()["answer"]}  # 返回