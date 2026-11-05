"""节点/步骤命名：从代码位置派生 id，不手写拼接。"""

from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 可调用类型
from types import FrameType  # 调用帧类型


class Namer:  # 命名器
    """id = 目录 + 文件 + 方法；全部来自代码位置，作者只写人话标题。"""

    BASE = "app.agent"  # 相对这个包前缀截断

    @staticmethod  # 纯函数
    def relative_module(module: str) -> str:  # 去掉基前缀
        """`app.agent.nodes.rag.graph` -> `nodes.rag.graph`。"""
        prefix = Namer.BASE + "."  # 前缀
        if module.startswith(prefix):  # 命中前缀
            return module[len(prefix):]  # 截断
        return module  # 原样

    @classmethod  # 类方法
    def node_id(cls, module: str, name: str) -> str:  # 节点 id
        """目录.文件.方法。"""
        relative = cls.relative_module(module)  # 相对路径
        return f"{relative}.{name}" if relative else name  # 拼接

    @classmethod  # 类方法
    def from_callable(cls, func: Callable[..., object]) -> str:  # 从函数派生
        """读 __module__ / __name__；两者缺一（C 扩展、partial 等）直接报错。"""
        module = getattr(func, "__module__", "") or ""  # 模块名
        name = getattr(func, "__name__", "") or ""  # 函数名
        if not module or not name:  # 派生不出稳定 id
            raise ValueError(f"无法从可调用对象派生 id：{func!r}")  # 拒绝空 id
        return cls.node_id(module, name)  # 派生

    @classmethod  # 类方法
    def from_frame(cls, frame: FrameType) -> str:  # 从调用帧派生
        """读帧里的模块名与函数名；模块级代码/匿名函数不配当节点，直接报错。"""
        module = str(frame.f_globals.get("__name__") or "")  # 模块名
        name = str(frame.f_code.co_name)  # 函数名
        if not module or name.startswith("<"):  # <module> / <lambda> / <listcomp>
            raise ValueError(f"stage() 必须写在具名函数里，当前是 {name!r}")  # 报错
        return cls.node_id(module, name)  # 派生

    @classmethod  # 类方法
    def stage_id(cls, base: str, name: str = "") -> str:  # 步骤 id
        """节点 id + 可选短名。"""
        return f"{base}.{name}" if name else base  # 拼接
