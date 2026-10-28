"""结构化输出模型的基类与错误收集（类 B 的输出契约）。

**规则（2026-10-27 用户定，基类在类创建时强制检查）**：

1. 字段说明只写在 ``Field(description="中文")``，**不再写行尾注释**——description 会随
   JSON Schema 发给模型，行尾 ``#`` 注释连 Pydantic 都看不到，写在那里等于没写。
2. **每个字段都必须有默认值**：不存在「可填可不填」的字段——模型省略时由默认值补齐。
3. 禁止额外字段（``extra="forbid"``）：模型乱加键算错误。

**不强制转换**（2026-10-27 用户明确要求）：值错了就是错了，不做「中文是→True」「null→默认值」
这类猜测式兜底——猜错比报错更贵。做法是**一次解析收全部错误**（Pydantic 默认就收集全部，
不是错一个停一个），把每个字段的问题整包回传给模型重新生成（见 :func:`issues_from` /
:func:`issues_text` 与 gateway 的结构化修复循环）。
"""

from __future__ import annotations

import json  # 错误位置拼接用
from dataclasses import dataclass  # 错误条目
from typing import Any  # 任意值

from pydantic import BaseModel, ConfigDict, ValidationError  # 校验模型与错误类型
from pydantic_core import PydanticUndefined  # 判断「有没有默认值」

#: 单个值的展示长度上限（错误信息里不贴整段长文本）。
_INPUT_LIMIT = 80  # 截断长度


class StructuredModel(BaseModel):  # 结构化输出模型基类
    """要交给大模型做结构化输出的模型都继承它；不合规的类**在定义时就报错**。"""

    model_config = ConfigDict(extra="forbid")  # 模型多给字段算错误

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:  # 子类建好后检查
        """确保每个字段都有 description 与默认值，否则抛 TypeError。"""
        super().__pydantic_init_subclass__(**kwargs)  # 走父类
        problems: list[str] = []  # 收集问题
        for name, field in cls.model_fields.items():  # 逐字段
            if not field.description:  # 没有说明
                problems.append(f"{name}：缺 Field(description=...)，模型看不到这个字段的含义")  # 记
            if field.default is PydanticUndefined and field.default_factory is None:  # 没有默认值
                problems.append(f"{name}：缺默认值，必须写成 field: T = Field(default=..., description=...)")  # 记
        if problems:  # 有不合规项
            raise TypeError(  # 定义时就失败
                f"{cls.__name__} 不符合结构化输出约定：\n- " + "\n- ".join(problems)  # 信息
            )  # 报错结束


@dataclass(frozen=True)
class Issue:  # 一条字段错误
    """给模型看的「错在哪」；一次校验会把所有字段的错误一起收进来。"""

    path: str  # 字段路径（intent / items.0.flag / <整体>）
    message: str  # 为什么不行（Pydantic 的原始说明）
    given: str = ""  # 模型给的原值（截断后的 repr）


def issues_from(exc: BaseException) -> list[Issue]:  # 异常 → 全部错误
    """Pydantic 的 ``ValidationError`` 一次就带全部字段错误；其他异常当「整体一条」。"""
    if isinstance(exc, ValidationError):  # 字段级错误
        result: list[Issue] = []  # 结果
        for item in exc.errors():  # 逐条（Pydantic 已收集全部，不是第一条）
            location = ".".join(str(part) for part in item.get("loc") or ()) or "<整体>"  # 路径
            result.append(  # 记
                Issue(  # 条目
                    path=location,  # 路径
                    message=str(item.get("msg") or "校验失败"),  # 说明
                    given=_short(item.get("input")),  # 原值
                )  # 条目结束
            )  # 记结束
        return result  # 返回
    if isinstance(exc, json.JSONDecodeError):  # 整体不是 JSON
        return [Issue(path="<整体>", message=f"输出不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}")]  # 一条
    return [Issue(path="<整体>", message=str(exc)[:300])]  # 其他异常合成一条


def _short(value: Any) -> str:  # 原值短表示
    """别把整段长文本贴进错误报告。"""
    text = repr(value)  # 表示
    return text if len(text) <= _INPUT_LIMIT else text[:_INPUT_LIMIT] + "…"  # 截断


def issues_text(issues: list[Issue]) -> str:  # 错误列表 → 给模型看的话
    """逐条编号，方便模型按条修；没有错误时返回空串。"""
    if not issues:  # 没有
        return ""  # 空
    lines: list[str] = []  # 行
    for index, issue in enumerate(issues, 1):  # 逐条编号
        given = f"（你给的是 {issue.given}）" if issue.given else ""  # 原值提示
        lines.append(f"{index}. {issue.path}：{issue.message}{given}")  # 一行
    return "\n".join(lines)  # 拼接


def parse_into(schema: type[BaseModel], data: Any) -> tuple[BaseModel | None, list[Issue]]:  # 解析一次
    """校验整份数据；成功给对象，失败给**全部**字段错误（不是第一条）。"""
    try:  # 尝试校验
        return schema.model_validate(data), []  # 成功
    except ValidationError as exc:  # 字段错误
        return None, issues_from(exc)  # 全量返回
    except Exception as exc:  # 其他（比如不是对象）
        return None, issues_from(exc)  # 一条整体错误


def schema_preview(cls: type[BaseModel]) -> dict[str, Any]:  # 看模型会收到什么
    """给开发自查/测试用：返回将发给模型的 JSON Schema。"""
    return cls.model_json_schema()  # 直接产出
