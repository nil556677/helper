"""模型输出契约：RouteDecision 与澄清候选（模型 → 程序 的稳定接口）。

程序判断（检索门限）与模型意图（E-B 路由）共用同一对象；本模块零业务依赖。
"""

from __future__ import annotations  # 类型注解延迟求值

import hashlib  # 稳定哈希兜底
import re  # 规范化文本
from enum import StrEnum  # 字符串枚举
from typing import Any  # 任意值

from pydantic import BaseModel, Field, model_validator  # 校验模型


class DecisionTarget(StrEnum):  # 决策目标枚举
    """下一步怎么走（枚举优于自由文本）。"""

    ANSWER = "answer"  # 根据证据作答
    HANDOFF = "handoff"  # 移交给另一个 Agent
    CHITCHAT = "chitchat"  # 闲聊，不需检索/工具
    CLARIFY = "clarify"  # 需要追问
    TOOL = "tool"  # 需要调用工具
    KB = "kb"  # 需要走知识库检索
    FINALIZE = "finalize"  # 结束本轮


class Candidate(BaseModel):  # 一条澄清候选项
    """id 由程序生成（稳定、供 resume 对齐）；text 给人看。"""

    id: str  # 稳定 id（不采信模型随机 token）
    text: str  # 人话文案
    extra: dict[str, Any] = Field(default_factory=dict)  # 附加元数据


def candidate_id(text: str) -> str:  # 由程序稳定生成候选 id
    """规范化成短语义键；实在出不来再用稳定哈希兜底。"""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24]  # 小写化并去非法字符
    return slug or hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]  # 空则取稳定哈希


class RouteDecision(BaseModel):  # 一次“下一步怎么走”的决策
    """程序判断与模型意图共用；由下游（边/驱动器/恢复）只读。"""

    next: DecisionTarget  # 下一步目标（枚举）
    confidence: float | None = None  # 决策可信度（0–1）
    needs_clarification: bool = False  # 是否需要追问
    candidates: list[Candidate] = Field(default_factory=list)  # 澄清候选项（clarify 时非空）
    reason: str = ""  # 人话原因（进 trace / UI）
    version: int = 1  # 输出契约版本号

    @model_validator(mode="after")  # 跨字段校验
    def _check(self) -> RouteDecision:  # 校验器
        """confidence 越界、候选 id 重复都视为构造错误。"""
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):  # 越界
            raise ValueError("confidence 必须在 0–1 之间")  # 拒绝
        ids = [c.id for c in self.candidates]  # 候选 id 列表
        if len(ids) != len(set(ids)):  # 有重复
            raise ValueError("candidates 的 id 必须唯一")  # 拒绝
        return self  # 通过
