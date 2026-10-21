"""日志过滤器/脱敏：loguru patcher 与兜底打码。

``_patch`` 是 loguru 的 ``patcher``：兜底脱敏 + 保证 run id 一定存在。
:data:`RUN_ID` 在**本模块**内定义（:func:`new_run_id` 只把它换新、不挪走），保证 patcher
在运行期读到的始终是当前值。
"""

from __future__ import annotations

import re
import uuid

#: 本次运行的短 id：把"一次启动"的所有记录串起来（用户报"刚才那次"，我们能一眼筛出来）。
RUN_ID = uuid.uuid4().hex[:6]

#: 形似密钥的片段：兜底脱敏用（见模块说明第 2 条）。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"((?i:api[_-]?key)\s*[=:]\s*)[A-Za-z0-9_\-]{12,}"), r"\1***"),
    (re.compile(r"((?i:authorization:\s*bearer\s+))[A-Za-z0-9._\-]{12,}"), r"\1***"),
)


def new_run_id() -> str:
    """给本次运行换一个新的 run id（测试用；正常运行只在进程启动时定一次）。"""
    global RUN_ID
    RUN_ID = uuid.uuid4().hex[:6]
    return RUN_ID


def redact(text: str) -> str:
    """把文本里形似密钥的片段打码。

    参数：
        text: 原始文本（日志消息或要展示的异常说明）。
    返回：打码后的文本；没有命中模式时原样返回。

    注意：这是**兜底**，不是安全边界。真正防泄漏的是"不给 loguru 开 diagnose"。
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _patch(record: object) -> None:
    """loguru patcher：兜底脱敏 + 保证 run id 一定存在。

    局部变量不经过这里（见模块说明第 2 条），所以别把安全责任放在这个函数上。
    """
    message = record["message"]  # type: ignore[index]
    if isinstance(message, str):
        record["message"] = redact(message)  # type: ignore[index]
    record["extra"].setdefault("run", RUN_ID)  # type: ignore[index]
    # 来源兜底：loguru 的 {name}/{line} 在"记录不是 Python 帧产生"时是 None（截获来的第三方日志、
    # Qt 回调都是这种），直接打印就成了「None:0」这种没法看的东西。这里给个兜底值，
    # 真正知道来源的地方（stdlib 桥、Qt 处理器）会 bind 覆盖它。
    record["extra"].setdefault("src", record["name"] or "?")  # type: ignore[index]
    record["extra"].setdefault("at", record["line"] or 0)  # type: ignore[index]
