"""画像域数据模型：ProfileEntry + 作用域/类型/来源常量（app/memory 扁平域包，S9 落位）。

**域纪律**（docs/架构与目录设计.md）：本包是"用户记忆"领域，UI 不得直接 import（须经
`app/services/memory.py`）；其它领域（RAG/工具）也不得 import 本包——跨域协作一律走 services。
本包可 import core（通用能力），但不得反过来被 core 依赖。

**FINGERPRINT_KEY 预留**：若本包引入向量（P3），必须声明自己的嵌入指纹
（``fingerprint.memory``），否则换嵌入模型时画像向量与文档向量不同空间。
"""

from __future__ import annotations  # 延迟值注解

from dataclasses import dataclass  # 数据类
from dataclasses import field as dataclass_field

#: 画像类型。
PropKind = str  # 类型别名

#: 类型枚举值（中文注释语义）。
EXPLICIT = "explicit"  # 用户显式设定
INFERRED = "inferred"  # 模型推断
CONFIRMED = "confirmed"  # 用户确认过

#: 来源（用于置信度与优先级）。
SOURCE_USER_EXPLICIT = "user_explicit"  # 用户显式陈述
SOURCE_USER_CORRECTION = "user_correction"  # 用户更正（最高优先级）
SOURCE_INFERRED = "inferred"  # 模型推断

#: 首选写键（模型抽取白名单，见 manager 的 _PROFILE_KEYS）。
PROFILE_KEYS: tuple[str, ...] = (  # 画像键白名单
    "language",  # 语言
    "timezone",  # 时区
    "verbosity",  # 回答长度偏好
    "table_policy",  # 是否用表格
    "structure",  # 结构偏好
    "conclusion_first",  # 是否先给结论
    "format",  # 格式要求
    "banned_phrases",  # 禁用表达
    "permission_scope",  # 权限范围
    "terms",  # 术语体系
    "business_rules",  # 业务规则
)  # 白名单结束

#: 推断条目的默认有效期（秒）：90 天。
INFERRED_TTL_S = 90 * 86400  # 90 天

#: 本领域向量指纹键（P3 开启向量时校验；未启用则不用）。
FINGERPRINT_KEY = "fingerprint.memory"  # 记忆嵌入指纹


def default_namespace() -> str:  # 默认作用域
    """当前单租户默认命名空间（JSON 数组）；多租户时从 settings 读。"""
    import json  # 序列化

    return json.dumps(["default", "user"], ensure_ascii=False)  # 单租户占位


@dataclass(frozen=True)
class ProfileEntry:  # 一条画像条目（内存形状，写库/渲染共用）
    """结构化画像条目；字段对应 user_profile 表。"""

    prop_key: str  # 画像键（语言/时区/篇幅…）
    value: object  # 值（JSON 可序列化）
    prop_kind: str = INFERRED  # 类型（explicit/inferred/confirmed）
    source: str = SOURCE_INFERRED  # 来源
    confidence: float = 0.5  # 可信度 0..1
    version: int = 1  # 版本号（冲突递增）
    namespace: str = dataclass_field(default_factory=default_namespace)  # 作用域
    valid_until: float | None = None  # 有效期（None=长期）
    status: str = "active"  # 状态（active/superseded/contested/archived）
