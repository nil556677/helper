"""参数：**唯一的共用默认值** + `ModelParams → ChatOpenAI` 的翻译与缓存键。

采样参数**不做后台集中配置**（改了会影响所有调用，还说不清是谁改的）。
- 全仓共用 :data:`DEFAULT_PARAMS` 这一套默认值；
- 每个调用点（答题 / 改写 / 路由 / 抽取）用 ``DEFAULT_PARAMS.with_overrides(...)``
  只声明自己不一样的那几项——差异留在调用处，看得见、可评审；
- 本模块另外负责：把显式给了的参数翻成 ChatOpenAI 关键字（``None`` 一律不传），
  以及「同槽位、不同参数不共用实例」的缓存键。
"""

from __future__ import annotations

from typing import Any  # 任意值

from app.agent.llm.types import ModelParams  # 参数形状

#: 全仓共用的一套默认参数（所有调用点从这里派生，别处不再另设默认）。
#:
#: | 参数 | 默认 | 为什么 |
#: |---|---|---|
#: | temperature | 0.2 | 资料问答要稳，不要发散 |
#: | top_p | 1.0 | 不额外做核采样截断（截断交给 temperature） |
#: | top_k | None | 多数 OpenAI 兼容网关不支持，默认不发 |
#: | frequency_penalty / presence_penalty | 0.0 | 保持中性，避免改写引用格式 |
#: | max_tokens | None | 交给模型/网关的默认上限，不硬猜 |
#: | stop | None | 停止词因业务而异，由调用处给 |
#: | seed | None | 默认不强制可复现（要复现的调用处自己给） |
#: | streaming | False | 默认非流式；答题这类调用处自己开 |
#: | timeout_s | 60.0 | 与旧行为一致；改写等短任务自己调小 |
#: | max_retries | 0 | **SDK 层不重试**：重试统一由网关 `RetryPolicy` 做（指数退避、最多 3 次），避免两层放大 |
DEFAULT_PARAMS = ModelParams(  # 共用默认
    temperature=0.2,  # 稳
    top_p=1.0,  # 不额外截断
    frequency_penalty=0.0,  # 中性
    presence_penalty=0.0,  # 中性
    max_tokens=None,  # 交给模型
    stop=None,  # 由调用处给
    seed=None,  # 不强制可复现
    streaming=False,  # 默认非流式
    timeout_s=60.0,  # 60 秒
    max_retries=0,  # 重试交给网关，SDK 不自己重试
)  # 默认结束


def to_chat_kwargs(params: ModelParams) -> dict[str, Any]:  # 参数 → ChatOpenAI 关键字
    """只翻显式给过的项；没给的不出现在结果里（SDK 用自己的默认）。"""
    kwargs: dict[str, Any] = {}  # 结果
    if params.temperature is not None:  # 显式温度
        kwargs["temperature"] = float(params.temperature)  # 传
    if params.top_p is not None:  # 显式核采样
        kwargs["top_p"] = float(params.top_p)  # 传
    if params.frequency_penalty is not None:  # 频率惩罚
        kwargs["frequency_penalty"] = float(params.frequency_penalty)  # 传
    if params.presence_penalty is not None:  # 存在惩罚
        kwargs["presence_penalty"] = float(params.presence_penalty)  # 传
    if params.max_tokens is not None:  # 输出上限
        kwargs["max_tokens"] = int(params.max_tokens)  # 传
    if params.stop is not None:  # 停止词
        kwargs["stop"] = list(params.stop)  # SDK 要 list
    if params.timeout_s is not None:  # 超时
        kwargs["timeout_s"] = float(params.timeout_s)  # 传
    if params.max_retries is not None:  # SDK 层重试
        kwargs["max_retries"] = int(params.max_retries)  # 传
    if params.seed is not None:  # seed 是 ChatOpenAI 的一等字段（不是额外体）
        kwargs["seed"] = int(params.seed)  # 直接传
    if params.streaming:  # 流式
        kwargs["streaming"] = True  # 只传 True，避免无意义覆盖
    transport: dict[str, Any] = {}  # 只能走 extra_body 的项
    if params.top_k is not None:  # top_k 不是 ChatOpenAI 字段
        transport["top_k"] = int(params.top_k)  # 放额外体（会以 extra_body 嵌套进请求）
    if transport:  # 有额外体
        kwargs["extra_body"] = transport  # 整块传
    return kwargs  # 返回


def cache_key(params: ModelParams) -> str:  # 实例缓存键
    """用参数的稳定表示做键：参数一变就是新实例，绝不串味。"""
    return repr(params)  # 冻结模型的 repr 字段序固定


def describe(params: ModelParams) -> dict[str, Any]:  # 参数的可读快照
    """给「本轮设置」/轨迹用：只列真正给了的项。"""
    return params.model_dump(exclude_none=True)  # 去掉未设置的
