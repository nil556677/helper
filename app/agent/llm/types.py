"""类 B 的数据契约：模型参数、流式块、非流式结果、结构化结果信封。

**采样参数由调用处声明**（答题要 0.2、改写要 0.0、抽取要 0.0……每个调用点都不一样），
本模块只定义形状与取值范围，**不提供任何全局默认值**——避免"改一处配置，所有调用变样"。
"""

from __future__ import annotations

from dataclasses import dataclass, field  # 流式块与默认值
from typing import Any  # 任意值

from pydantic import BaseModel, ConfigDict, Field  # 校验模型

from app.agent.middleware.base import ModelRequest  # 请求形状复用中间件已定义的那一个

__all__ = [  # 对外导出
    "ModelChunk",  # 流式块
    "ModelParams",  # 采样参数
    "ModelRequest",  # 请求形状（再导出）
    "ModelResult",  # 非流式结果
    "ModelUnavailable",  # 槽位没配全
    "ModelUsage",  # token 用量
]  # 导出结束


class ModelParams(BaseModel):  # 一次模型调用的可调参数
    """全部采样/连接参数；**None = 这一项不传给 SDK**，由调用处按需显式给出。

    为什么不设默认值：同一个网关被不同业务调用，温度/上限本来就该各写各的；
    把默认值集中在配置里，等于把"每个调用点的差异"藏进全局状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)  # 写错参数名直接报错；不可变

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)  # 采样温度
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)  # 核采样
    top_k: int | None = Field(default=None, gt=0)  # 部分兼容网关支持（走 extra_body）
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)  # 频率惩罚
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)  # 存在惩罚
    max_tokens: int | None = Field(default=None, gt=0)  # 输出上限
    stop: tuple[str, ...] | None = None  # 停止词（不可变，便于当缓存键）
    seed: int | None = Field(default=None, ge=0)  # 可复现采样（ChatOpenAI 一等字段）
    streaming: bool = False  # 是否流式
    timeout_s: float | None = Field(default=None, gt=0)  # 单次请求超时（秒）
    max_retries: int | None = Field(default=None, ge=0)  # SDK 层重试（默认 0：重试统一由网关 RetryPolicy 做）

    def with_overrides(self, **changes: Any) -> ModelParams:  # 派生一份
        """在**共用默认值**的基础上只改需要的项；返回新实例，不改自己（冻结）。"""
        merged = {**self.model_dump(), **changes}  # 合并（未知键会因 extra=forbid 报错）
        return ModelParams(**merged)  # 重新校验后返回


@dataclass(frozen=True)
class ModelChunk:  # 流式块
    """模型吐出的一个增量：正文 / 思考 / 用量（三者可能只有其一）。"""

    text: str = ""  # 正文增量
    reasoning: str = ""  # 思考增量（部分网关给 reasoning_content）
    usage: dict[str, Any] = field(default_factory=dict)  # 流末尾才可能有的用量明细


class ModelUsage(BaseModel):  # token 用量
    """非流式结果里的用量；缓存/推理明细进 extra。"""

    prompt_tokens: int = 0  # 输入
    completion_tokens: int = 0  # 输出
    total_tokens: int | None = None  # 总数
    extra: dict[str, Any] = Field(default_factory=dict)  # 额外明细（缓存读/写、推理 token）


class ModelResult(BaseModel):  # 非流式结果
    """一次 generate() 的完整结果（流式由 astream 逐块给，不走这里）。"""

    text: str = ""  # 正文
    usage: ModelUsage = Field(default_factory=ModelUsage)  # 用量
    attempt: int = 1  # 实际用掉的尝试次数（含重试）
    elapsed_ms: int = 0  # 总耗时
    finish_reason: str = ""  # 结束原因（stop / length / content_filter）
    model: str = ""  # 槽位名（不是模型 id，便于看"走的哪个槽"）


class ModelUnavailable(RuntimeError):  # 槽位不可用
    """槽位没配全 / 密钥解不开：调用方应走兜底，**不要重试**。"""
