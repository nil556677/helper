"""工具接线层：langchain 工具接到 agent 的唯一收口（校验 / 元数据 / bind / ToolNode 执行）。

原则：只复用框架原生机制（`@tool` 派生 `BaseTool` / ``bind_tools`` / ``langgraph`` 的 ``ToolNode``），
不另造并行执行模型；工具的「结果」统一用 `core.Result`（带内错误，不抛异常）。
"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 名称合规校验
from types import ModuleType  # 模块类型收集
from typing import Any  # 任意值

from langchain_core.tools import BaseTool  # 框架工具基类
from langgraph.prebuilt import ToolNode  # 框架原生工具执行节点

from app.agent.contracts import (  # 契约
    IdRegistry,  # id 注册表（构图层强制唯一）
    Mutation,  # 变更画像
    ToolDescriptor,  # 工具描述符
)  # 契约导入结束
from app.core.error import err as make_err  # 失败 Result（核心统一）
from app.core.error import ok as make_ok  # 成功 Result（核心统一）
from app.core.result import Result  # 唯一结果契约

#: 工具名合法形状：小写蛇形、无点号、≤30；与 langchain 对工具名的约束一致。
_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")  # 名字正则

__all__ = [  # 对外导出
    "bind",  # 绑定到模型
    "collect",  # 模块收集
    "describe",  # 元数据描述符
    "err",  # 失败包络
    "make_tool_node",  # 建执行节点
    "ok",  # 成功包络
    "render",  # 包络 -> 模型 JSON
    "to_content_and_artifact",  # 拆分模型视角与审计视角
    "unwrap_tool_output",  # (content, artifact) -> 完整 Result
    "validate_unique",  # 名称合规 + 全局唯一
]  # 导出结束


class InvalidToolNameError(ValueError):  # 非法工具名
    """工具名不合规（必须 `[a-z0-9_]`、无点号、≤30）。"""


def collect(module: ModuleType) -> list[BaseTool]:  # 收集模块内工具
    """返回模块里 `@tool` 定义的 `BaseTool`；有 `__all__` 时只收其中声明为工具的。"""
    declared = getattr(module, "__all__", None)  # 显式导出清单（防误收 import 进来的工具）
    if declared:  # 有清单
        found = [getattr(module, name, None) for name in declared]  # 按名取
        tools = [value for value in found if isinstance(value, BaseTool)]  # 只留工具
        if tools:  # 有收获
            return tools  # 按声明顺序返回
    return [value for value in vars(module).values() if isinstance(value, BaseTool)]  # 无清单时退回全量筛


def validate_unique(tools: list[BaseTool]) -> list[BaseTool]:  # 校验名称唯一
    """名称合规 + 全局唯一；重复/非法在**构图期**直接抛，不留到运行期。

    用仓库现成的 `IdRegistry` 做去重（构图层已用它保证 node/stage_id 唯一，这里复用同一机制，
    不另起一套）。返回同一列表，方便链式调用：
    ``tools = wiring.collect(mod); tools = wiring.validate_unique(tools)``。
    """
    registry = IdRegistry()  # 名称注册表（复用契约层）
    for tool in tools:  # 逐个
        if tool is None:  # 跳过空位
            continue  # 不校验
        name = str(tool.name or "")  # 名字即 id
        if not _NAME_RE.match(name):  # 不合规
            raise InvalidToolNameError(  # 明确报错
                f"工具名不合法：{name!r}（须为 a-z0-9_，无点号，≤30）"  # 说明
            )  # 异常结束
        where = f"{type(tool).__module__}.{type(tool).__name__}"  # 来源（定位用）
        registry.register(name, where or "unknown")  # 重复会抛 DuplicateIdError
    return tools  # 原样返回


def describe(  # 生成工具元数据描述符
    tool: BaseTool,  # 框架工具
    *,
    version: int | None = None,  # 契约版本（缺省读工具自带）
    mutation: Mutation | None = None,  # 变更画像（缺省读工具自带）
    permissions: tuple[str, ...] | None = None,  # 权限标签（缺省读工具自带）
) -> ToolDescriptor:  # 描述符
    """复用框架的 name/description/args_schema，只补框架没有的元数据。

    优先用 `@tool_spec` 挂到工具上的 `_tool_meta`，调用方无需重复传 version/mutation/permissions。
    """
    meta = dict(((tool.metadata or {}).get("dsh")) or {})  # 工具自带的项目元数据
    return ToolDescriptor(  # 描述符
        name=str(tool.name or ""),  # 名称即 id
        version=int(version if version is not None else meta.get("version", 1)),  # 版本
        description=str(tool.description or ""),  # 能力描述
        args_schema=tool.args_schema,  # 参数契约（框架原样）
        result_schema=meta.get("result_schema"),  # 结果契约（tool_spec 声明）
        mutation=mutation if mutation is not None else meta.get("mutation", Mutation.READ_ONLY),  # 变更画像
        approval=str(meta.get("approval") or "none"),  # 审批策略（tool_spec 声明）
        risk=str(meta.get("risk") or "low"),  # 风险等级（tool_spec 声明）
        permissions=tuple(permissions) if permissions is not None else tuple(meta.get("permissions") or ()),  # 权限
        timeout_ms=int(meta.get("timeout_ms", 5000)),  # 工具级超时（tool_spec 声明）
    )  # 描述符结束


def bind(model: Any, tools: list[BaseTool], **kwargs: Any) -> Any:  # 绑定工具到模型
    """先过唯一性闸，再委托框架 ``model.bind_tools([...])`` 准备工具 Schema。"""
    tools = validate_unique(tools)  # 同名工具绝不进模型 Schema
    return model.bind_tools(  # 框架绑定
        tools,  # 已校验工具
        **kwargs,  # 透传额外参数
    )  # 绑定结束


def make_tool_node(  # 建框架原生工具执行节点
    tools: list[BaseTool],  # 工具列表
    *,
    handle_tool_errors: bool = False,  # 工具异常是否上抛
) -> ToolNode:  # LangGraph 工具节点
    """先过唯一性闸，再建 `langgraph.prebuilt.ToolNode`；默认不吞错，让带内 Result 流动。"""
    tools = validate_unique(tools)  # 分派靠 name，重名必炸，构图期拦下
    return ToolNode(  # 框架节点
        tools,  # 已校验工具
        handle_tool_errors=handle_tool_errors,  # 错误处理策略
    )  # 节点结束


def ok(data: Any = None, **meta: Any) -> Result:  # 成功 Result
    """工具成功返回的统一入口（委托 core.error.ok）；code="ok"。"""
    return make_ok(data, **meta)  # 核心统一


def err(  # 失败 Result
    code: str,  # 错误码（用 ErrorCode 常量）
    message: str = "",  # 人话说明
    *,
    retryable: bool = True,  # 是否可重试
    retry_after_s: float | None = None,  # 建议等待秒数
) -> Result:  # 包络
    """带内错误：不抛异常，让调用方（含模型）据返回推理下一步。"""
    return make_err(code, message, retryable=retryable, retry_after_s=retry_after_s)  # 核心统一


def to_content_and_artifact(  # 拆分模型视角与审计视角
    env: Result,  # 统一结果
    model_view: Any,  # 喂给模型的子集
) -> tuple[Any, str]:  # (content, artifact)
    """对应 langchain ``content_and_artifact`` 语义：content 给模型，artifact 走审计。"""
    return model_view, env.model_dump_json()  # 模型只看收敛文本，审计保留全文


def render(env: Result) -> str:  # 把统一 Result 转成给模型的 JSON 文本
    """模型视角：成功只给 `data`，失败只给 `{code,message,retryable}`（不再带缓存等框架字段）。"""
    if env.ok:  # 成功
        import json  # 只在需要时导入

        return json.dumps(env.data, ensure_ascii=False, default=str)  # 只暴露数据
    import json  # 只在需要时导入

    return json.dumps(  # 错误给模型可读的最小信息
        {"code": env.code, "message": env.message, "retryable": env.retryable},  # 错误三要素
        ensure_ascii=False,  # 中文直出
    )  # 返回


def unwrap_tool_output(value: Any) -> Result:  # 程序化调用还原完整 Result
    """tool.invoke/ainvoke 在 content_and_artifact 下只回 content 文本，这里按 JSON 还原包络。

    - 完整包络 JSON（含 ok/code）→ 直接还原；
    - 仅错误三要素（code/message/retryable）→ 还原失败包络；
    - 其它 JSON 对象 → 视为成功 data。
    """
    import json  # 解析 content

    if isinstance(value, Result):  # 已是包络
        return value  # 原样
    if isinstance(value, tuple) and len(value) == 2:  # (content, artifact)
        value = value[1]  # 走审计视角
        if isinstance(value, Result):  # 已是对象
            return value  # 原样
    if isinstance(value, str):  # 序列化文本
        try:  # 解析
            obj = json.loads(value)  # JSON
        except Exception:  # 非 JSON（如参数校验失败的可读文本）
            return Result(ok=False, code="schema_invalid", message=str(value), retryable=False)  # 还原为参数错
        if isinstance(obj, dict):  # 对象
            if "ok" in obj and "code" in obj:  # 完整包络
                try:  # 还原
                    return Result(**obj)  # 成功/失败都能还原
                except Exception:  # 字段不全
                    pass  # 继续按下面兜底
            if "code" in obj and "message" in obj:  # 错误三要素
                return Result(ok=False, code=str(obj.get("code")), message=str(obj.get("message")), retryable=bool(obj.get("retryable", False)))  # 失败包络
            return Result(ok=True, code="ok", data=obj)  # 视为成功 data
    return value  # 其它原样
