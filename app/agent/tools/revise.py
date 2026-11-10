"""工具参数「调用前修复」助手：参数校验不过时，结构化反馈 + 有限次重生成收敛。

面向 5.3.2：模型生成的参数不合法，不应无限"错→自猜→再错"。这里提供有限次(默认 2)的
修复循环：把 `schema / 错误参数 / 逐字段校验报告` 交给调用方注入的 `repair` 回调
（真实实现可调 LLM，复用 app/agent/llm/repair.py 的结构化修复思路），每次重生成后
重新校验；仍不通过则抛 `ToolError(PROTOCOL_ARG_INVALID)` 终止。

本模块是**独立助手**：`check`（校验）与 `repair`（重生成）都可由调用方注入，便于测试；
默认 `check` 用 args_schema 的 pydantic 校验并产出结构化 issues。暂不强接工具调用主路径
（需要真实 LLM 网关上下文，接入点见优化方案 P3c）。
"""

from __future__ import annotations  # 延迟值注解

from collections.abc import Callable  # 回调类型
from typing import Any  # 任意值

from pydantic import ValidationError  # 捕获校验错误

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.tools.tooling import ToolError  # 业务错（参数不合法）

__all__ = ["Issue", "collect_issues", "fix_tool_args"]  # 导出

#: 修复重生成次数上限（默认 2：一次自猜失效后再给一次机会）。
DEFAULT_REPAIR_LIMIT = 2  # 次数

#: 修复报告里的单条问题（与校验器产出的结构化对象对齐）。
Issue = dict[str, Any]  # 类型别名（loc 路径 / msg 说明 / type 类型）


def collect_issues(schema: type[Any], args: dict[str, Any]) -> list[Issue]:  # 结构化校验
    """用 args_schema 校验参数，返回逐字段问题列表；合法返回 []。

    问题条目：``{"loc": ["order_id"], "msg": "...", "type": "string_too_short"}``，
    与 pydantic ValidationError 的 ``errors()`` 对齐，便于回喂模型逐条修正。
    """
    if schema is None:  # 未声明契约
        return []  # 无法校验 → 视为合法
    try:  # 校验
        schema.model_validate(args)  # 合法
    except ValidationError as exc:  # 有错
        return [  # 逐条提炼
            {"loc": list(entry.get("loc") or []), "msg": str(entry.get("msg") or ""), "type": str(entry.get("type") or "")}  # 每条
            for entry in exc.errors()  # 遍历
        ]  # 返回
    return []  # 合法


def fix_tool_args(  # 有限次修复循环
    args: dict[str, Any],  # 模型给出的原参数
    schema: type[Any] | None,  # 参数契约（可能 None）
    *,  # 关键字
    repair: Callable[[dict[str, Any], list[Issue]], dict[str, Any]],  # 重生成回调（并入报告）
    check: Callable[[type[Any], dict[str, Any]], list[Issue]] = collect_issues,  # 校验器（可注入）
    limit: int = DEFAULT_REPAIR_LIMIT,  # 最多修复次数
) -> dict[str, Any]:  # 收敛后的合法参数
    """校验→修复→再校验，有限次收敛；仍不通过抛 ToolError(PROTOCOL_ARG_INVALID)。

    `repair(args, issues)` 由调用方注入：真实实现把 schema/issues 交 LLM 重生成一版参数并返回。
    """
    current = dict(args)  # 当前参数
    issues = check(schema, current)  # 首轮校验
    if not issues:  # 首轮即合法
        return current  # 直接返回
    for _ in range(limit):  # 有限次重生成
        current = dict(repair(current, issues))  # 让调用方给一版修正
        issues = check(schema, current)  # 再校验
        if not issues:  # 终于合法
            return current  # 返回
    # 用尽仍不合法 → 明确终止（不无限重试）
    raise ToolError(  # 业务错（参数不合法）
        str(ErrorCode.PROTOCOL_ARG_INVALID),  # 协议参数错
        f"参数多次修复仍不合法：{issues}",  # 说明（结构化报告可查）
    )  # 结束