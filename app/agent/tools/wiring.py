"""工具接线层：langchain 工具接到 agent 的唯一收口（校验 / 元数据 / bind / ToolNode 执行）。

原则：只复用框架原生机制（`@tool` 派生 `BaseTool` / ``bind_tools`` / ``langgraph`` 的 ``ToolNode``），
不另造并行执行模型；工具的「结果包络」统一用契约层 `ResultEnvelope`（带内错误，不抛异常。
接入真实工具时在这里「收集 → 校验唯一 → 描述 → 绑定 → 建执行节点」，随后挂进 LangGraph 图。
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
    ResultEnvelope,  # 结果包络
    ToolDescriptor,  # 工具描述符
)  # 契约导入结束

#: 工具名合法形状：小写蛇形、无点号、≤30；与 langchain 对工具名的约束一致。
_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")  # 名字正则

__all__ = [  # 对外导出
    "collect",  # 模块收集
    "validate_unique",  # 名称合规 + 全局唯一
    "describe",  # 元数据描述符
    "bind",  # 绑定到模型
    "make_tool_node",  # 建执行节点
    "ok",  # 成功包络
    "err",  # 失败包络
    "to_content_and_artifact",  # 拆分模型视角与审计视角
    "render",  # 包络 -> 模型 JSON
]  # 导出结束


class InvalidToolNameError(ValueError):  # 非法工具名
    """工具名不合规（必须 `[a-z0-9_]`、无点号、≤30）。"""


def collect(module: ModuleType) -> list[BaseTool]:  # 收集模块内工具
    """返回模块里 `@tool` 定义的 `BaseTool`（按定义顺序）；类型就是框架的。"""
    return [value for value in vars(module).values() if isinstance(value, BaseTool)]  # 筛出工具


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
        mutation=mutation if mutation is not None else meta.get("mutation", Mutation.READ_ONLY),  # 变更画像
        permissions=tuple(permissions) if permissions is not None else tuple(meta.get("permissions") or ()),  # 权限
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
    """先过唯一性闸，再建 `langgraph.prebuilt.ToolNode`；默认不吞错，让带内 ResultEnvelope 流动。"""
    tools = validate_unique(tools)  # 分派靠 name，重名必炸，构图期拦下
    return ToolNode(  # 框架节点
        tools,  # 已校验工具
        handle_tool_errors=handle_tool_errors,  # 错误处理策略
    )  # 节点结束


def ok(data: Any = None, **meta: Any) -> ResultEnvelope:  # 成功包络
    """工具成功返回的统一入口；code="ok"。"""
    return ResultEnvelope(ok=True, code="ok", data=data, meta=dict(meta))  # 包络


def err(  # 失败包络
    code: str,  # 错误码（用契约层 ErrorCode 常量）
    message: str = "",  # 人话说明（给模型/调用方）
    *,
    retryable: bool = True,  # 是否可重试
    retry_after_s: float | None = None,  # 建议等待秒数
) -> ResultEnvelope:  # 包络
    """带内错误：不抛异常，让调用方（含模型）据返回推理下一步。"""
    return ResultEnvelope(  # 包络
        ok=False,  # 失败
        code=code,  # 错误码
        message=message,  # 说明
        retryable=retryable,  # 可重试标记
        retry_after_s=retry_after_s,  # 建议等待
    )  # 包络结束


def to_content_and_artifact(  # 拆分模型视角与审计视角
    envelope: ResultEnvelope,  # 结果包络
    model_view: Any,  # 喂给模型的子集
) -> tuple[Any, str]:  # (content, artifact)
    """对应 langchain ``content_and_artifact`` 语义：content 给模型，artifact 走审计。"""
    return model_view, envelope.model_dump_json()  # 模型只看收敛文本，审计保留全文


def render(envelope: ResultEnvelope) -> str:  # 把统包络转成给模型的 JSON 文本
    """统一返回形：喂给模型的 ToolMessage 内容就是这段 JSON（错误时 message 模型可见）。"""
    return envelope.model_dump_json()  # JSON 文本