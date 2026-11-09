"""工具契约硬约束：每个工具都声明了参数/结果/超时（把"声明完整"变成 CI 检查）。"""

from __future__ import annotations  # 延迟值注解

from pydantic import BaseModel  # 结果契约基类

from app.agent.tools.catalog import tool_catalog  # 工具目录
from app.agent.tools.wiring import describe  # 元数据描述符


def test_all_tools_declare_full_contract() -> None:  # 全部工具契约齐备
    catalog = tool_catalog()  # 目录
    assert catalog  # 非空
    for name, tool in catalog.items():  # 逐个
        desc = describe(tool)  # 描述符
        assert desc.args_schema is not None, f"{name} 缺 args_schema"  # 参数契约
        assert isinstance(desc.result_schema, type) and issubclass(desc.result_schema, BaseModel), f"{name} 缺 result_schema"  # 结果契约
        assert int(desc.timeout_ms) > 0, f"{name} 缺 timeout_ms"  # 超时声明


def test_search_tool_contract_shape() -> None:  # 检索工具契约形状
    desc = describe(tool_catalog()["search_knowledge_base"])  # 描述符
    fields = set(desc.args_schema.model_fields)  # 参数字段
    assert fields == {"query", "mode", "top_k"}  # 查询 + 强度 + 条数
    description = str(desc.args_schema.model_fields["mode"].description)  # mode 说明
    assert "fast" in description and "deep" in description  # 说明覆盖各档