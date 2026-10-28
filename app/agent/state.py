"""LangGraph 的状态（只放可序列化值）。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Annotated, Any, TypedDict  # 状态类型工具

from langchain_core.messages import AnyMessage  # 任意消息类型
from langgraph.graph.message import add_messages  # 消息追加 reducer


def merge_steps(current: list[dict[str, Any]], update: list[dict[str, Any]]) -> list[dict[str, Any]]:  # 步骤合并
    """按 ``id`` 合并步骤：同 id 覆盖、否则追加。

    为什么不用 ``operator.add``：恢复/重放时同一步骤会被加两次 → 重复。按 ``id`` 覆盖让
    重放幂等（同一个调用点派的 stage_id 只留一份）。
    """
    out = list(current or [])  # 现有
    index: dict[str, int] = {}  # id → 索引
    for i, item in enumerate(out):  # 建索引
        if item.get("id") is not None:  # 有 id
            index[str(item["id"])] = i  # 记录
    for item in update or []:  # 逐条更新
        ident = item.get("id")  # 取 id
        if ident is not None and str(ident) in index:  # 已存在
            out[index[str(ident)]] = item  # 覆盖（保持顺序）
        else:  # 新增
            if ident is not None:  # 有 id
                index[str(ident)] = len(out)  # 记录索引
            out.append(item)  # 追加
    return out  # 返回


class ChatState(TypedDict, total=False):  # 图状态（字段都可缺省）
    # 输入
    question: str  # 用户问题
    attachments: list[dict[str, Any]]  # 本轮附件（含可读文本）
    conversation_id: str  # 会话 id

    # 多轮
    messages: Annotated[list[AnyMessage], add_messages]  # 多轮消息（按 id 追加）
    history: list[dict[str, Any]]  # 新链路的纯字典历史

    # 路由
    route: dict[str, Any]  # 路由决策
    pending: dict[str, Any]  # 待补参数
    clarify_prompt: str  # 补参提示语

    # 检索
    hits: list[dict[str, Any]]  # 召回命中
    evidence: list[dict[str, Any]]  # 证据片段
    answerable: bool  # 检索后是否可进入生成（FSM 迁移与边路由共用这一个判断）

    # 答复
    answer: str  # 回答正文
    note: str  # 一句说明
    note_kind: str  # 说明样式
    fragments: list[dict[str, Any]]  # 引用片段
    turn: dict[str, Any]  # 一大轮的完整形状
    usage: dict[str, Any]  # token 用量（可序列化）

    # 元数据（对账 / checkpoint 定位）
    turn_id: str  # 逻辑轮次 id（= user 消息 id）
    status: str  # 运行状态（running/done/cancelled/failed）
    attempt: int  # 第几次尝试

    # 可观测 / 诊断（checkpoint 也序列化，恢复时可查）
    settings: dict[str, Any]  # 本轮设置
    basis: dict[str, Any]  # 检索依据
    gate: dict[str, Any]  # 证据门限判决（pass/weak/reject）
    reasoning: str  # 模型思考流
    conflict: str  # 资料与通用知识的冲突提示

    # 可观测
    steps: Annotated[list[dict[str, Any]], merge_steps]  # 过程步骤（按 id 合并，重放不重复）
    started_at: float  # 开始时间
    error: str  # 错误信息
