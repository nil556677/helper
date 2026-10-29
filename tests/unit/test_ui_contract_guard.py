"""回归守护：UI 对后端的 import 必须能解析（防止"改了后端、界面断链"再次发生）。

背景（同一次审计）：`app/agent/events.py` 重构后删掉了 ``Event`` 类，但
``app/ui/screens/chat/events.py`` 还在 ``from app.agent.events import Event, EventType``，
整条聊天页 import 即崩 —— 因为后端单测从不 import UI，这个断链在 400 个全绿测试下发而逃。

本测试**静态扫描** UI 对后端包的 import，逐条验证被导入的名字确实存在：
- 对 ``from <pkg> import a, b`` → 用 ``importlib`` 解析模块，检查每个名字在 ``dir()`` 里；
- 对 ``import <pkg>`` / ``from <pkg> import <sub>` → 验证模块能导入。

只做"名字是否存在"的解析，不触发 NiceGUI/模型等重型副作用（用 ``importlib.util`` 的
惰性 ``find_spec`` 优先，解析失败再落到真实 ``importlib.import_module``）。
"""

from __future__ import annotations  # 类型注解延迟求值

import ast  # 解析源码
import importlib  # 解析模块
import importlib.util  # find_spec 惰性检测子模块
from pathlib import Path  # 路径

_APP = Path(__file__).resolve().parents[2] / "app"  # app 根
#: 需要真实 import 的后端包（碰到这些用 import_module，其它用惰性 find_spec）。
_IMPORTABLE = ("app.agent", "app.services", "app.core", "app.rag", "app.integrations")
#: 忽略的名字（__all__ 元数据 / 仅类型注解里用的 forward ref 不做强判）。
_KNOWN_SKIP = {"annotations", "Any"}


def _resolve_module(top: str):  # 解析模块对象
    if top in _IMPORTABLE or top.startswith(_IMPORTABLE):  # 后端包：真实导入
        return importlib.import_module(top)  # 导入
    return None  # 非白名单：不强导入（避免 UI 重副作用）


def _iter_ui_imports():  # 产出 (文件, 相对路径, 顶层模块名, 名字列表|None)
    for path in sorted((_APP / "ui").rglob("*.py")):  # 全部 UI 源文件
        if "__pycache__" in path.parts:  # 缓存
            continue  # 跳过
        rel = str(path.relative_to(_APP.parent)).replace("\\", "/")  # 相对位置
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))  # 解析
        except SyntaxError:  # 语法错误
            continue  # 留给 pytest 报
        for node in ast.walk(tree):  # 遍历
            if isinstance(node, ast.ImportFrom) and node.level == 0:  # 绝对 from-import
                module = str(node.module or "")  # 模块名
                if not module.startswith("app."):  # 只要后端/app 内部
                    continue  # 跳过第三方
                names = [a.name for a in node.names if a.name != "*"]  # 导入的名字
                yield rel, module, names
            elif isinstance(node, ast.Import):  # 直接 import app.xxx
                for alias in node.names:  # 逐个
                    if alias.name.startswith("app."):  # app 内部
                        yield rel, alias.name, None  # 只验能导入


def test_ui_imports_resolve_against_backend() -> None:  # UI 对后端 import 全可解析
    """UI 里每个 ``from app.X import name`` 的 name 必须真实存在，否则断链。

    这条在审计后补上，专防"后端删了符号、UI 还在用、后端单测测不出来"。
    """
    failures: list[str] = []  # 违规清单
    resolved: set[str] = set()  # 已解析模块缓存（避免重复导入）
    for rel, module, names in _iter_ui_imports():  # 逐条
        mod = _resolve_module(module) if module in _IMPORTABLE or module.startswith(_IMPORTABLE) else None  # 后端才导入
        if mod is None:  # 非后端模块（如 app.ui 内部）：跳过
            continue  # 不强判
        resolved.add(module)  # 记录
        if names is None:  # 只验模块可导入（import app.x）
            continue  # 已导入成功即通过
        have = set(dir(mod))  # 该模块真实可见名字（__all__ 只影响 import *，不影响具名 import）
        for name in names:  # 逐个名字
            if name in _KNOWN_SKIP:  # 元数据
                continue  # 跳过
            if name in have:  # 顶层符号在
                continue  # 通过
            # 不在 dir()：可能是子模块（from app.services import settings → app/services/settings.py）。
            try:  # find_spec 对"非包的父模块"会抛 ModuleNotFoundError
                sub_spec = importlib.util.find_spec(f"{module}.{name}")  # 探测子模块
            except (ImportError, AttributeError, ModuleNotFoundError):  # 父不是包/找不到
                sub_spec = None  # 视为不存在
            if sub_spec is not None:  # 子模块确实存在
                continue  # 通过
            failures.append(f"{rel}: from {module} import {name} —— 该名字/子模块不存在")  # 记录
    assert not failures, "UI 对后端 import 断链（需修）：\n" + "\n".join(sorted(failures))  # 必须全绿


def test_ui_never_imports_core_directly() -> None:  # R6：UI 禁止直连 core
    """界面层不得顶层 import ``app.core``（scripts/rule_check.py 的 R6 机械版，防回潮）。"""
    offenders: list[str] = []  # 违规清单
    for path in sorted((_APP / "ui").rglob("*.py")):  # 全部 UI
        if "__pycache__" in path.parts:  # 缓存
            continue  # 跳过
        rel = str(path.relative_to(_APP.parent)).replace("\\", "/")  # 相对位置
        tree = ast.parse(path.read_text(encoding="utf-8"))  # 解析
        for node in ast.walk(tree):  # 遍历
            if isinstance(node, ast.ImportFrom) and node.level == 0:  # 绝对 from
                if (node.module or "").startswith("app.core"):  # 直连 core
                    offenders.append(f"{rel}: from {node.module} import …")  # 记录
            elif isinstance(node, ast.Import) and any(alias.name.startswith("app.core") for alias in node.names):  # 直连
                offenders.append(f"{rel}: import {node.names[0].name}")  # 记录
    assert not offenders, f"UI 直连 app.core（违反 R6）：{sorted(set(offenders))}"  # 必须为空
