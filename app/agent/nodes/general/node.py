"""L4e 通用兜底节点（general_agent）：纯 LLM、无工具、不闲聊，只做能力引导。"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from app.agent.context import bind_node_run  # 绑 run（发 stage 事件）
from app.agent.llm.provider import get_chat_model  # 统一模型入口

__all__ = ["general_agent_node"]  # 导出

_PROMPT = (  # 提示
    "你是资料问答助手的兜底引导。不要闲聊、不要伪造数据或工具能力。"
    "若用户问题超出能力范围，如实说明，并引导其提出可检索资料回答的问题。"
)  # 提示结束


def _degrade() -> dict[str, Any]:  # 无模型降级
    return {"answer": "我是资料问答助手：可以帮你在本地资料中检索并据资料回答。请描述你想查的资料问题。", "note": ""}  # 模板


@bind_node_run
async def general_agent_node(state: dict[str, Any], config: Any = None) -> dict[str, Any]:  # 图节点
    """有模型才生成引导话术；无模型/失败用模板，绝不假装有数据。"""
    del config  # 不用图配置
    from app.agent.contracts import StageGroup  # 分组
    from app.agent.stages import stage  # 过程步

    model = get_chat_model()  # 统一入口
    question = str(state.get("question") or "")  # 问题
    if model is None:  # 无模型
        return _degrade()  # 模板
    try:  # 用 ContextBuilder 配上下文（含工作记忆/近轮），再调模型
        from app.agent.contexts.builder import build_context  # builder（嵌入已在图装配时注入一次）
        facts: list[dict[str, Any]] = list(state.get("memory_facts") or [])  # 优先用 load_context 已召回的
        profile_text = str(state.get("memory_profile_text") or "")  # 优先用入口画像
        if not facts:  # 没有 → 兜底召回
            try:  # 事实取数失败不阻塞
                from app.agent.memory.manager import recall  # 记忆统一入口

                bundle = recall(question, top_k=6)  # 统一召回（事实+画像）
                facts = list(bundle.facts)  # 语义召回
                profile_text = "\n".join(bundle.profile_lines) or profile_text  # 画像
            except Exception:  # 无表/坏库
                facts = []  # 空
        out = build_context(  # 装配（内容错误不阻塞）
            system=_PROMPT,  # S 引导
            working=state.get("working_memory") or {},  # 锚点
            facts=facts,  # 长期事实
            recent=list(state.get("turns") or []),  # 近轮
            profile_text=profile_text,  # 画像块（稳定约束）
            question=question,  # 问题
        )  # 结束
        sys_text = out.system or _PROMPT  # system
        human = out.messages[-1]["content"] if out.messages else question  # 人类消息
        with stage("生成回答", group=StageGroup.GENERATE):  # 过程区可见
            response = await model.ainvoke([("system", sys_text), ("human", human)])  # 请求
    except Exception:  # 装配/调用失败
        return _degrade()  # 模板
    text = str(getattr(response, "content", "") or "").strip()  # 正文
    return {"answer": text or _degrade()["answer"]}  # 返回
