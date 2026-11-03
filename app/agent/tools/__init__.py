"""工具层：框架原生接线——`@tool` 收集、名称唯一校验、元数据、bind、ToolNode 执行。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent.tools.wiring import (  # 接线层
    InvalidToolNameError,  # 名称不合法
    bind,  # 绑定到模型
    collect,  # 收集
    describe,  # 描述符
    err,  # 失败包络
    make_tool_node,  # 执行节点
    ok,  # 成功包络
    render,  # 包络 -> 模型 JSON
    to_content_and_artifact,  # 视角拆分
    validate_unique,  # 名称唯一校验
)  # 导入结束

__all__ = [  # 导出
    "InvalidToolNameError",  # 名称非法
    "bind",  # 绑定
    "collect",  # 收集
    "describe",  # 描述符
    "err",  # 失败
    "make_tool_node",  # 执行节点
    "ok",  # 成功
    "render",  # 包络渲染
    "to_content_and_artifact",  # 视角拆分
    "validate_unique",  # 唯一校验
]  # 导出结束