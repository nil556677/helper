"""类 B：模型网关（参数 / 重试 / 输入安全 / 输出校验与兜底）。

外部只 import 这里；内部模块职责：

- ``types``    —— 数据契约（ModelParams / ModelResult / 结构化信封）
- ``params``   —— 参数 → ChatOpenAI 关键字 / 缓存键（纯机制）
- ``retry``    —— 错误分类 + 退避 + 重试事件
- ``gateway``  —— 统一入口（generate / astream / structured）
- ``schema``   —— 结构化输出基类（字段必须有 description 与默认值）
- ``validate`` —— 输出清洗 + 两层校验 + 判决口径
- ``repair``   —— 带报告的有限次重生成
- ``fallback`` —— 确定性兜底
"""

from __future__ import annotations

from app.agent.llm.fallback import extractive_answer, note_for  # 兜底
from app.agent.llm.gateway import ModelGateway  # 网关
from app.agent.llm.params import DEFAULT_PARAMS, cache_key, describe, to_chat_kwargs  # 参数机制
from app.agent.llm.repair import (  # 修复
    REGENERATE_LIMIT,  # 次数上限
    regenerate,  # 答案修复
    repair_loop,  # 有限次修复
    repair_request,  # 答案修复请求
    structured_repair_request,  # 结构化修复请求
)
from app.agent.llm.retry import (  # 重试
    RETRYABLE_KINDS,  # 可重试类别
    RetryInfo,  # 重试事实
    RetryPolicy,  # 策略
    classify_error,  # 分类
    retry_async,  # 通用重试
)
from app.agent.llm.schema import (  # 结构化输出
    Issue,  # 单条字段错误
    StructuredModel,  # 基类约束
    issues_from,  # 异常 → 全部错误
    issues_text,  # 错误 → 人话
    parse_into,  # 整份校验
    schema_preview,  # schema 预览
)
from app.agent.llm.types import (  # 数据契约
    ModelChunk,  # 流式块
    ModelError,  # 错误载荷
    ModelParams,  # 参数
    ModelRequest,  # 请求
    ModelResult,  # 结果
    ModelUnavailable,  # 不可用
    ModelUsage,  # 用量
    Result,  # 结构化信封
    ResultMeta,  # 信封元信息
)
from app.agent.llm.validate import (  # 校验
    DEFAULT_MIN_COVERAGE,  # 默认阈值
    ValidationReport,  # 报告
    report_text,  # 人话原因
    sanitize_markdown,  # 清洗
    validate_answer,  # 总入口
)

__all__ = [  # 对外导出
    "DEFAULT_MIN_COVERAGE",  # 覆盖率阈值
    "DEFAULT_PARAMS",  # 共用默认参数
    "REGENERATE_LIMIT",  # 修复次数上限
    "RETRYABLE_KINDS",  # 可重试类别
    "Issue",  # 单条字段错误
    "ModelChunk",  # 流式块
    "ModelError",  # 错误载荷
    "ModelGateway",  # 网关
    "ModelParams",  # 参数
    "ModelRequest",  # 请求
    "ModelResult",  # 结果
    "ModelUnavailable",  # 不可用
    "ModelUsage",  # 用量
    "Result",  # 结构化信封
    "ResultMeta",  # 信封元信息
    "RetryInfo",  # 重试事实
    "RetryPolicy",  # 重试策略
    "StructuredModel",  # 结构化输出基类
    "ValidationReport",  # 校验报告
    "cache_key",  # 参数缓存键
    "classify_error",  # 错误分类
    "describe",  # 参数快照
    "extractive_answer",  # 原文兜底
    "issues_from",  # 异常 → 全部错误
    "issues_text",  # 错误 → 人话
    "note_for",  # 兜底说明
    "parse_into",  # 整份校验
    "regenerate",  # 修复调用
    "repair_loop",  # 有限次修复
    "repair_request",  # 修复请求
    "report_text",  # 报告人话
    "retry_async",  # 通用重试
    "sanitize_markdown",  # 清洗
    "schema_preview",  # JSON Schema 预览
    "structured_repair_request",  # 结构化修复请求
    "to_chat_kwargs",  # 参数翻译
    "validate_answer",  # 总校验
]  # 导出结束
