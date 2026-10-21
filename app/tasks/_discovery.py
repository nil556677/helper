"""发现 ``app/tasks/`` 下的**任务类**（一个任务 = 一个类 = 一个模块）。

**约定**（越少越不容易忘）：

1. 任务模块里定义**一个** :class:`~app.core.jobs.Task` 的子类（类名随意，语义清楚就行）；
2. **任务标识由类名派生**（``Ingest`` → ``ingest``），不用手写；
   类名与文件名一致、不带 ``Task`` 后缀；
3. 必须声明 ``title``（界面显示名），**必须实现** ``run(ctx, params)``（抽象方法，没实现过不了校验）。

``_`` 开头的模块不是任务模块（放共享基类/内部工具，例如本文件）。

发现过程**只读模块、不改注册表**，所以可以被反复调用（测试要先 ``registry.clear()``
再重建注册表，这条是硬要求）。

**绝不吞异常**：模块导入失败、类不合规、key 重复，都必须抛出去。静默少注册一个 kind，
``runner`` 会把用户排队中的该类型任务判成"这个任务类型已经不存在了"——那是比启动失败
严重得多的数据破坏。
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from app.core.jobs.registry import JobKind
from app.core.jobs.task import Task

#: 任务包名（本模块的父包）。
_PACKAGE = "app.tasks"


def task_modules() -> list[ModuleType]:
    """导入并返回 ``app/tasks/`` 下全部任务模块（按名字排序，跳过 ``_`` 开头的内部模块）。

    异常：
        Exception: **原样抛出**模块导入期的任何异常（见模块说明：静默跳过更危险）。
    """
    package = importlib.import_module(_PACKAGE)
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        modules.append(importlib.import_module(f"{_PACKAGE}.{info.name}"))
    return modules


def task_classes(module: ModuleType) -> list[type[Task]]:
    """一个任务模块里定义的 Task 子类。

    只认**本模块自己定义**的类（``__module__`` 相符）：别人 import 进来的不算，
    否则模块之间互相引用会把同一个任务数两次。
    """
    found: list[type[Task]] = []
    for value in vars(module).values():
        if not (isinstance(value, type) and issubclass(value, Task) and value is not Task):
            continue
        if value.__module__ != module.__name__:
            continue
        found.append(value)
    return found


def declared(module: ModuleType) -> list[type[Task]]:
    """校验并返回一个任务模块里**恰好那一个**任务类。

    异常：
        ValueError: 模块里没有任务类，或放了不止一个（"一个任务一个类"是约定）。
    """
    classes = task_classes(module)
    if not classes:
        raise ValueError(
            f"{module.__name__} 里没有任务类：任务模块必须定义一个 Task 子类"
            "（一个任务一个类；共享基类请放进 `_` 开头的模块）"
        )
    if len(classes) > 1:
        names = "、".join(sorted(cls.__name__ for cls in classes))
        raise ValueError(
            f"{module.__name__} 里定义了多个任务类（{names}）：一个模块只放一个任务类"
        )
    return classes


def discovered_kinds() -> tuple[JobKind, ...]:
    """发现全部任务：**逐条校验 → 查重 → 全过才返回**（不留半成品，也不改注册表）。

    异常：
        TypeError: 任务类没实现 ``run``（或声明类型不对）。
        ValueError: 缺 ``key``/``title``、字段不合规、key 重复。
    """
    found: list[tuple[str, JobKind]] = []
    for module in task_modules():
        for cls in declared(module):
            # `to_kind()` 里做全部合规检查：必须实现 run、必须有 key/title、字段形状。
            found.append((module.__name__, cls.to_kind()))

    owners: dict[str, str] = {}
    for module_name, kind in found:
        if kind.key in owners:
            raise ValueError(
                f"任务标识 {kind.key!r} 被声明了两次：{owners[kind.key]} 与 {module_name}"
                "（每个标识只许一个主人）"
            )
        owners[kind.key] = module_name

    return tuple(kind for _, kind in sorted(found, key=lambda pair: pair[1].key))
