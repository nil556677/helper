"""全项目唯一的结果契约：正确 / 错误形状 + 统一判断。

任何层（纯逻辑 / 工具 / 模型网关 / HTTP / RAG / 编排）都传递同一个 `Result`（返回对象或
`.model_dump_json()` 给模型）。**基类极简**：只放通用形状与判断，域专属字段进 `meta`，
或由各模块继承 `Result` 扩展（基类本身不带任何域名）。`is_ok` / `should_retry` 是统一判断入口。
"""

from __future__ import annotations  # 延迟求值注解

from typing import Any  # 任意值

from pydantic import BaseModel, Field  # 校验模型


class Result(BaseModel):  # 唯一结果契约
    """正确 / 错误 / 可重试，一层表达；字段极简，域细节进 meta 或子类。"""

    ok: bool = False  # 成功 / 失败（主判断）
    code: str = "ok"  # 错误码（ErrorCode 单源；成功为 "ok"）
    message: str = ""  # 人话（错误给模型 / 用户看）
    retryable: bool = False  # 是否值得重试（统一"声明"）
    retry_after_s: float | None = None  # 建议等待秒
    data: Any = None  # 成功的数据（失败通常为 None）
    meta: dict[str, Any] = Field(default_factory=dict)  # 通用扩展位（耗时/cached/attempt 等）

    @property  # 只读
    def is_ok(self) -> bool:  # 是否成功
        """主判断一致的入口。"""
        return self.ok  # 成功

    @property  # 只读
    def should_retry(self) -> bool:  # 是否值得重试
        """失败且标了可重试 → 上层统一退避执行。"""
        return (not self.ok) and self.retryable  # 判定