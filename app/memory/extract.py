"""画像 LLM 结构化抽取：从一段对话抽稳定偏好/约束（用默认 `llm` 槽位）。

**遵守 AGENTS.md 例外条款**：字段只写 `Field(description="中文")`，不写行尾注释
（description 会随 JSON Schema 发给模型）；每个字段都有默认值（模型没提到 → 空串/空表），
禁止硬凑。

本模块是画像写的**第三条通道**（附 C：①显式 ②行为统计 ③LLM 推断，能不用就不用③）。
`extract_profile` 降到同步壳：memory_distill 是同步 Task，内部跑模型调用。
"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 同步壳
from typing import Any  # 任意值

from pydantic import Field  # 字段定义（结构化输出）

from app.agent.llm.gateway import ModelGateway  # 模型网关（默认 llm 槽位）
from app.agent.llm.params import DEFAULT_PARAMS  # 默认参数
from app.agent.llm.schema import StructuredModel  # 结构化输出基类
from app.agent.llm.types import ModelRequest  # 请求契约
from app.core.result import Result  # 结构化信封
from app.memory.model import PROFILE_KEYS  # 画像键白名单

__all__ = ["ProfileExtraction", "extract_profile", "profile_candidates"]  # 导出


class ProfileExtraction(StructuredModel):  # 画像抽取结果
    """从一段对话里抽出的用户稳定偏好；未提及的字段一律空串/空表。"""

    verbosity: str = Field("", description="期望回答长度：short/normal/detailed，未提及则空")
    table_policy: str = Field("", description="是否希望用表格：table/none，未提及则空")
    structure: str = Field("", description="结构偏好：bullets/paragraph，未提及则空")
    conclusion_first: str = Field("", description="是否先给结论：yes/no，未提及则空")
    format: str = Field("", description="格式要求（如 markdown/简体），未提及则空")
    language: str = Field("", description="用户偏好语言，未提及则空")
    timezone: str = Field("", description="用户所在时区（如 UTC+8），未提及则空")
    banned_phrases: list[str] = Field(default_factory=list, description="用户明确禁止的措辞/表达，未提及则空表")
    terms: list[str] = Field(default_factory=list, description="自定义术语映射（如「单子=订单」），未提及则空表")
    business_rules: list[str] = Field(default_factory=list, description="用户/公司稳定业务规则，未提及则空表")


_PROMPT = (  # 抽取系统提示
    "你是用户偏好抽取器。从下面这段用户对话里，抽出用户表现出的、**稳定且可复用**的偏好或约束。\n"
    "规则：\n"
    "1. 只抽能跨会话复用的稳定偏好；一次性/场景性表达（如「这次用表格」）不要抽——留空。\n"
    "2. 没提到的字段留默认值（空串/空表），不要编造。\n"
    "3. banned_phrases 只认用户明确表达过的不喜欢/禁止；禁止猜测。\n"
    "4. 含糊表达（好像/可能/大概）不要当稳定偏好。"
)  # 提示结束


def _avail(slot: str = "llm") -> bool:  # 槽位可用性
    """默认模型可用才走 LLM 抽取；否则返回 False（调用方降级纯规则）。"""
    return ModelGateway(slot=slot, emit=None).available(DEFAULT_PARAMS)  # 探测


def profile_candidates(data: ProfileExtraction) -> list[dict[str, Any]]:  # 抽取结果 → 画像候选
    """把模型抽出的字段收敛成 `record_facts` 可直接消费的候选（只保留非空）。"""
    candidates: list[dict[str, Any]] = []  # 结果
    mapping = {  # 模型字段 → 画像键（白名单内的才收）
        "verbosity": "verbosity",  # 回答长度
        "table_policy": "table_policy",  # 是否表格
        "structure": "structure",  # 结构偏好
        "conclusion_first": "conclusion_first",  # 先结论
        "format": "format",  # 格式要求
        "language": "language",  # 语言
        "timezone": "timezone",  # 时区
    }  # 映射表
    for field_name, key in mapping.items():  # 逐标量字段
        value = getattr(data, field_name, "")  # 取值
        if value and key in PROFILE_KEYS:  # 非空且在白名单
            candidates.append({"key": key, "value": value, "source": "inferred", "confidence": 0.6})  # 候选
    for field_name, key in (("banned_phrases", "banned_phrases"), ("terms", "terms"), ("business_rules", "business_rules")):  # 列表字段
        values = list(getattr(data, field_name, None) or [])  # 列表
        if values and key in PROFILE_KEYS:  # 非空且在白名单
            candidates.append({"key": key, "value": values, "source": "inferred", "confidence": 0.55})  # 候选
    return candidates  # 返回


def extract_profile(  # 从一段对话抽画像候选（同步壳，供 memory_distill 调）
    conversation_fragment: str,  # 对话文本
    *,
    slot: str = "llm",  # 默认大模型槽位
) -> list[dict[str, Any]]:  # 画像候选列表
    """调默认模型结构化抽取；不可用/失败返回空（调用方降级纯规则通道）。

    返回候选（key/value/source/confidence），交 `manager.record_facts` 走统一去重+画像分流。
    """
    if not _avail(slot):  # 模型不可用
        return []  # 降级
    text = str(conversation_fragment or "").strip()  # 对话片段
    if not text:  # 空
        return []  # 空
    try:  # 模型调用失败不算致命
        request = ModelRequest(  # 请求
            model=slot,  # 槽位
            messages=[{"role": "user", "content": text}],  # 对话
            system=_PROMPT,  # 抽取规则
        )  # 结束
        gateway = ModelGateway(slot=slot, emit=None)  # 网关
        result: Result = asyncio.run(  # 同步壳（distill 是同步 Task）
            gateway.structured(request, ProfileExtraction, DEFAULT_PARAMS, repair_limit=1)  # 结构化输出
        )  # 跑
        if not result.ok or result.data is None:  # 失败
            return []  # 降级
        return profile_candidates(result.data)  # 收敛
    except Exception:  # 网络/解析失败
        return []  # 降级
