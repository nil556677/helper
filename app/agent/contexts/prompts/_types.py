"""提示词模板：标识由**定义模块**自动生成，不需要（也不能）手写名字。

`uid` = 定义模块的 ``__name__``。两个模板模块路径不可能相同，因此唯一性由程序
保证；模块重命名/挪文件等于换了一个模板身份（有意为之）。显示用的短标签由
`label` 从模块路径派生，不参与查找。
"""

from __future__ import annotations  # 类型注解延迟求值

import sys  # 取调用方模块
from dataclasses import dataclass  # 冻结数据类
from string import Formatter  # 解析占位符

__all__ = ["PromptTemplate", "define", "placeholders"]  # 对外导出


def placeholders(template: str) -> tuple[str, ...]:  # 找出占位符
    """按出现顺序返回 ``{name}`` 里的 name；重复只算一次。"""
    seen: list[str] = []  # 结果（保序去重）
    for _, field, _, _ in Formatter().parse(template):  # 逐个字段
        if field and field not in seen:  # 是占位符且没出现过
            seen.append(field)  # 记
    return tuple(seen)  # 返回


@dataclass(frozen=True)
class PromptTemplate:  # 一个模板
    """``variables`` 必须与正文占位符**完全一致**；`origin` 由 `define()` 自动填。"""

    origin: str  # 定义模块路径（唯一标识）
    version: int  # 版本，改正文必须 +1
    template: str  # 正文
    variables: tuple[str, ...] = ()  # 需要哪些变量

    def __post_init__(self) -> None:  # 构造后校验
        """占位符与声明不一致 → 直接报错，不等运行期。"""
        if not self.origin:  # 没有身份
            raise ValueError("模板必须带 origin（请用 define() 定义）")  # 报错
        found = placeholders(self.template)  # 正文里的
        if found != tuple(self.variables):  # 与声明不一致
            raise ValueError(  # 报错
                f"模板 {self.origin!r} 的占位符 {found} 与声明 {tuple(self.variables)} 不一致"
            )

    @property
    def uid(self) -> str:  # 唯一标识
        """就是定义模块路径。"""
        return self.origin  # 返回

    @property
    def label(self) -> str:  # 显示用短标签（派生，非手写）
        """模块路径的最后一段。"""
        return self.origin.rsplit(".", 1)[-1]  # 取尾段

    def render(self, **values: object) -> str:  # 渲染
        """缺变量、多变量都抛 ValueError（不用 .format 静默吞）。"""
        missing = set(self.variables) - set(values)  # 缺的
        extra = set(values) - set(self.variables)  # 多的
        if missing or extra:  # 有出入
            raise ValueError(f"模板 {self.origin!r} 变量不匹配：缺 {sorted(missing)}，多 {sorted(extra)}")
        return self.template.format(**values)  # 渲染


def define(*, version: int, template: str, variables: tuple[str, ...] = ()) -> PromptTemplate:  # 定义模板
    """在模板模块里调用；`origin` 自动取调用方模块的 ``__name__``。"""
    origin = sys._getframe(1).f_globals.get("__name__", "")  # 上一层（模板模块）
    return PromptTemplate(origin=origin, version=version, template=template, variables=tuple(variables))  # 构造
