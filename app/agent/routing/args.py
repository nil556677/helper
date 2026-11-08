"""参数完整性：对照工具 `args_schema` 的必填字段判断缺参（规则/查表，零 LLM）。"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

__all__ = ["missing_required", "required_fields"]  # 导出


def required_fields(tool_names: list[str] | tuple[str, ...]) -> list[str]:  # 必填字段
    """从工具目录里按名取 args_schema，汇总必填字段（去重保序）。"""
    from app.agent.tools.catalog import tool_catalog  # 工具目录（单一来源）

    catalog = tool_catalog()  # 名 → 工具
    result: list[str] = []  # 结果
    for name in tool_names or ():  # 逐工具
        tool = catalog.get(str(name))  # 取工具
        if tool is None:  # 未注册
            continue  # 跳过
        schema = getattr(tool, "args_schema", None)  # 参数模型
        fields = getattr(schema, "model_fields", None)  # pydantic 模型
        if fields:  # 是 pydantic
            for key, info in fields.items():  # 逐字段
                if key not in result and getattr(info, "is_required", lambda: False)():  # 必填
                    result.append(key)  # 记录
            continue  # 下一个
        if isinstance(schema, dict):  # JSON Schema
            for key in schema.get("required") or ():  # 必填数组
                if key not in result:  # 去重
                    result.append(str(key))  # 记录
    return result  # 返回


def missing_required(tool_names: list[str] | tuple[str, ...], entities: dict[str, Any]) -> list[str]:  # 缺哪些必填
    """必填字段中，实体里没有的即是缺参。"""
    provided = entities or {}  # 已提供
    return [name for name in required_fields(tool_names) if name not in provided]  # 缺的