"""流式小工具：把"关闭"一路传到最上游的 HTTP 连接。"""

from __future__ import annotations  # 类型注解延迟求值

import contextlib  # 抑制关闭时的异常
from typing import Any  # 任意值


async def aclose(stream: Any) -> None:  # 关闭一个流
    """尽力关闭异步迭代器；不是生成器就跳过，关的时候抛错也不管。

    **为什么必须显式关**：``async for`` 中途抛异常或跳出循环，**不会**自动关闭底层生成器，
    上游 httpx 连接要等垃圾回收才断——用户点了停止，模型那边还在生成、还在计费。
    每层包装（取消 / 脱敏 / token 流）都在 ``finally`` 里调一次，信号才能传到最里层。
    """
    if stream is None:  # 没有流
        return  # 直接返回
    closer = getattr(stream, "aclose", None)  # 只有异步生成器才有这个方法
    if closer is None:  # 普通迭代器 / 同步生成器
        return  # 跳过
    with contextlib.suppress(Exception):  # 关闭失败不该盖住取消
        await closer()  # 真正关闭
