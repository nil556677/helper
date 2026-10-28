"""共享状态契约：对话轮次 Turn 与写契约表（多步共享状态的稳定形状）。

成对（user+assistant）是构造不变量，从根上避免「悬空提问」。本模块零业务依赖。
"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 默认时间戳
from enum import StrEnum  # 字符串枚举
from typing import Any  # 任意值

from pydantic import BaseModel, Field, model_validator  # 校验模型


class TurnStatus(StrEnum):  # 轮次状态
    """一次问答的终态；历史默认只喂 done。"""

    DONE = "done"  # 正常答完（必有 assistant 文本）
    FAILED = "failed"  # 运行失败
    CANCELLED = "cancelled"  # 用户停止
    UNANSWERED = "unanswered"  # 没证据 / 门限拒答


class TurnToolCall(BaseModel):  # 一次工具调用的审计记录
    """面向持久 / 审计，不喂模型；靠 tool_call_id 与内部消息契约关联。"""

    tool_call_id: str  # 与消息域调用 id 关联（全 ID）
    name: str  # 工具名
    status: str = "ok"  # ok / error
    artifact: Any = None  # 完整结果（审计通道）
    duration_ms: int | None = None  # 耗时
    error_code: str = ""  # 错误码


class Turn(BaseModel):  # 一“轮”问答
    """user + assistant 成对是构造不变量；未完成轮由 status 表达。"""

    turn_id: str  # 稳定 id（= 该轮 user 消息 id）
    status: TurnStatus  # 轮次状态
    user: str  # 用户问题
    assistant: str | None = None  # 回答文本（未完成时为 None/空）
    tool_calls: list[TurnToolCall] = Field(default_factory=list)  # 审计记录
    created_at: float = Field(default_factory=time.time)  # 创建时间

    @model_validator(mode="after")  # 构造期校验
    def _check(self) -> Turn:  # 校验器
        """done 必须带 assistant 文本；tool_call_id 必须唯一。"""
        if self.status is TurnStatus.DONE and not (self.assistant or "").strip():  # done 无回答
            raise ValueError("done 的轮必须有 assistant 文本")  # 拒绝
        ids = [t.tool_call_id for t in self.tool_calls]  # 调用 id 列表
        if len(ids) != len(set(ids)):  # 有重复
            raise ValueError("tool_calls 的 tool_call_id 必须唯一")  # 拒绝
        return self  # 通过


def merge_turns(current: list[Turn] | None, update: list[Turn] | None) -> list[Turn]:  # 按轮次合并
    """reducer：同 turn_id 覆盖、否则追加，保持顺序（重放幂等）。"""
    out: list[Turn] = list(current or [])  # 现有
    index: dict[str, int] = {t.turn_id: i for i, t in enumerate(out)}  # id → 索引
    for item in update or []:  # 逐条更新
        if item.turn_id in index:  # 已存在
            out[index[item.turn_id]] = item  # 覆盖
        else:  # 新增
            index[item.turn_id] = len(out)  # 记索引
            out.append(item)  # 追加
    return out  # 返回


def only_done(turns: list[Turn] | None) -> list[Turn]:  # 历史过滤
    """默认只喂 done 的轮（不给模型噪音）。"""
    return [t for t in (turns or []) if t.status is TurnStatus.DONE]  # 过滤


class WriteRule(BaseModel):  # 写契约表的一行
    """某状态 channel：谁写、怎么合并、什么不变式。"""

    channel: str  # 状态 channel 名
    owner: str  # 唯一写入方
    reducer: str  # merge / override / append
    invariant: str = ""  # 不变式说明（人话）


#: 写契约表（单一事实来源）：图内节点不能写不属于自己的 channel。
WRITE_TABLE: list[WriteRule] = [  # 表内容
    WriteRule(channel="turns", owner="装配/工具子图回写", reducer="merge", invariant="成对、done 必有文本"),  # 轮次
    WriteRule(channel="evidence", owner="检索", reducer="override", invariant="与 route 一致"),  # 证据
    WriteRule(channel="route", owner="检索", reducer="override", invariant="迁移与边路由同源"),  # 决策
    WriteRule(channel="answer", owner="生成", reducer="override", invariant="status=done 时非空"),  # 答案
    WriteRule(channel="status", owner="驱动器", reducer="override", invariant="只进终态一次"),  # 运行态
]  # 表结束
