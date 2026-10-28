"""类型标签块：统一格式、转义、**空块不输出**。

为什么转义：资料/附件/工具输出里可能含 ``</evidence>`` 这类闭合标签，不处理就可能
“提前关块”，把后面的内容变成指令。这里把 ``& < >`` 转成实体，块结构不会被内容破坏。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

__all__ = ["block", "container", "escape_attr", "escape_text", "join_blocks"]  # 对外导出


def escape_text(text: str) -> str:  # 正文转义
    """先转 ``&`` 再转 ``<`` / ``>``，避免二次转义。"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: Any) -> str:  # 属性转义
    """属性值只需管引号与尖括号。"""
    text = str(value).replace("&", "&amp;").replace('"', "&quot;")  # 先转特殊字符
    return text.replace("<", "&lt;").replace(">", "&gt;")  # 再转尖括号


def block(tag: str, body: str, **attrs: Any) -> str:  # 造一个标签块
    """body 为空（含纯空白）时返回空串——调用方直接 join 即可，不会产生空标签。"""
    text = str(body or "").strip()  # 去首尾空白
    if not text:  # 空块
        return ""  # 不输出
    attr_text = "".join(f' {key}="{escape_attr(value)}"' for key, value in attrs.items())  # 属性
    return f"<{tag}{attr_text}>\n{escape_text(text)}\n</{tag}>"  # 成块


def container(tag: str, inner: str, **attrs: Any) -> str:  # 容器块（内层不再转义）
    """内层已经是块文本时用它；空内容返回空串。"""
    text = str(inner or "").strip()  # 去首尾空白
    if not text:  # 空容器
        return ""  # 不输出
    attr_text = "".join(f' {key}="{escape_attr(value)}"' for key, value in attrs.items())  # 属性
    return f"<{tag}{attr_text}>\n{text}\n</{tag}>"  # 成块


def join_blocks(*blocks: str) -> str:  # 拼接多个块
    """过滤空块，用空行隔开。"""
    items = [item for item in blocks if item]  # 去掉空串
    return "\n\n".join(items)  # 拼接
