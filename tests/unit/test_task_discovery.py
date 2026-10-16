"""任务类与自动注册（`app/tasks/`）。

**为什么这些必须被测试钉住**：任务从"手写一个元组"改成"一个任务一个类 + 自动发现"之后，
风险从"写错一行"变成了"**悄悄少注册一个 kind**"——而少注册的后果不是报错，是
`runner._fail_orphans()` 把用户排队中的该类型任务判成"类型已不存在"，直接把用户的任务判死。
所以这里重点钉四件事：

1. **发现要全**：三个任务一个都不少，标识由类名派生；
2. **类要合规**：必须实现 `run()`、必须声明 `title`（界面显示名）；
3. **失败要响**：模块导入失败 / 不合规 / 标识重复，必须抛，绝不静默跳过；
4. **重复调用要安全**：注册幂等、`registry.clear()` 之后能重建、不留半成品，
   而且**每次执行新建实例**（状态与资源不跨次残留）。
"""

from __future__ import annotations

import importlib
import inspect
import types
from collections.abc import Iterator
from typing import ClassVar

import pytest

from app import tasks as tasks_app
from app.core.jobs import api, clear_kind_cache, registry
from app.core.jobs.registry import JobKind, validate
from app.core.jobs.task import Task, TaskResult, key_from_class_name
from app.tasks import _discovery

#: 期望发现到的任务（加了新任务就更新这里——这条断言就是"防漏注册"的闸门）。
EXPECTED_KEYS = {"cleanup", "import", "rebuild"}


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    """每个用例前后都清空注册表与 JobKind 缓存（它们都是进程级的）。"""
    registry.clear()
    clear_kind_cache()
    yield
    registry.clear()
    clear_kind_cache()


def _module(name: str, *classes: type[Task]) -> types.ModuleType:
    """造一个假的"任务模块"（测发现逻辑，不往真实包目录里塞文件）。"""
    module = types.ModuleType(name)
    for cls in classes:
        setattr(module, cls.__name__, cls)
    return module


def _task_class(
    name: str,
    *,
    module: str,
    title: str = "示例任务",
    implement_run: bool = True,
    **attrs: object,
) -> type[Task]:
    """动态造一个任务类（`__module__` 指到假模块，才能被"本模块定义"的规则认到）。"""
    namespace: dict[str, object] = {"title": title, **attrs}
    if implement_run:
        namespace["run"] = lambda self, ctx, params: TaskResult(note="ok")
    cls = type(name, (Task,), namespace)
    cls.__module__ = module
    return cls


# ---------------------------------------------------------------------------
# 标识：由类名派生
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        ("ImportTask", "import"),
        ("CleanupTask", "cleanup"),
        ("RebuildTask", "rebuild"),
        ("OCRSyncTask", "ocr_sync"),          # 连续大写按一个词处理
        ("VectorMatrixTask", "vector_matrix"),  # 驼峰转下划线
        ("Plain", "plain"),                   # 不以 Task 结尾也认
    ],
)
def test_key_comes_from_class_name(class_name: str, expected: str) -> None:
    """**任务标识直接由类名派生**：不用手写 key，少一处能写错的地方。"""
    assert key_from_class_name(class_name) == expected


def test_production_kinds_use_derived_keys() -> None:
    """真实三个任务的标识 == 类名派生结果（而不是另写一份）。"""
    kinds = _discovery.discovered_kinds()
    assert {kind.key for kind in kinds} == EXPECTED_KEYS
    from app.tasks.cleanup import CleanupTask
    from app.tasks.import_doc import ImportTask
    from app.tasks.rebuild import RebuildTask

    assert CleanupTask.to_kind().key == "cleanup"
    assert ImportTask.to_kind().key == "import"
    assert RebuildTask.to_kind().key == "rebuild"


# ---------------------------------------------------------------------------
# 发现：要全
# ---------------------------------------------------------------------------


def test_discovers_all_production_kinds() -> None:
    """真扫 `app/tasks/`：三个任务一个不少，且都是 cpu 泳道（与迁移前一致）。"""
    kinds = _discovery.discovered_kinds()
    assert {kind.key for kind in kinds} == EXPECTED_KEYS
    assert {kind.lane for kind in kinds} == {"cpu"}
    for kind in kinds:
        validate(kind)
        assert kind.title.strip(), f"{kind.key} 必须给界面一个显示名"


def test_each_task_lives_in_its_own_module() -> None:
    """一个任务一个模块：模块名与任务文件一一对应。"""
    names = {module.__name__ for module in _discovery.task_modules()}
    assert names == {"app.tasks.cleanup", "app.tasks.import_doc", "app.tasks.rebuild"}
    # `_discovery` 自己是下划线开头，不是任务模块
    assert not any(name.endswith("_discovery") for name in names)


def test_declared_returns_the_single_class() -> None:
    """一个模块里恰好一个任务类时，返回它。"""
    module = importlib.import_module("app.tasks.cleanup")
    classes = _discovery.declared(module)
    assert len(classes) == 1
    assert classes[0].__name__ == "CleanupTask"
    assert issubclass(classes[0], Task)


def test_imported_classes_do_not_count_as_local_tasks() -> None:
    """从别处 import 进来的 Task 子类不算本模块的任务（否则会被数两次）。"""
    imported = _task_class("Foreign", module="app.tasks.elsewhere")
    module = _module("app.tasks.host", imported)
    assert _discovery.task_classes(module) == []


# ---------------------------------------------------------------------------
# 合规：必须实现执行方法 / 必须声明显示名
# ---------------------------------------------------------------------------


def test_task_without_run_cannot_be_instantiated() -> None:
    """**没实现 `run` 就实例化不了**（抽象方法），而且报错直接点名。"""
    incomplete = _task_class("Incomplete", module="app.tasks.x", implement_run=False)
    assert inspect.isabstract(incomplete)
    with pytest.raises(TypeError, match="run"):
        incomplete.to_kind()


def test_task_without_title_is_rejected() -> None:
    """没声明界面显示名 → 报错。"""
    blank = _task_class("NoTitle", module="app.tasks.x", title="")
    with pytest.raises(ValueError, match="title"):
        blank.to_kind()


def test_invalid_derived_key_is_rejected() -> None:
    """类名派生出的标识不合法（这里是下划线开头）→ 校验期就报错。"""
    bad = _task_class("_Hidden", module="app.tasks.x")
    with pytest.raises(ValueError, match="key"):
        bad.to_kind()


def test_bad_class_attributes_are_rejected() -> None:
    """其余类属性写错 → 校验期就报出来，且点名字段。"""
    for attrs, expect in (
        ({"lane": "gpu"}, "lane"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"lease_s": 0}, "lease_s"),
        ({"payload_version": 0}, "payload_version"),
        ({"resource": "  "}, "resource"),
    ):
        cls = _task_class("Bad", module="app.tasks.x", **attrs)
        with pytest.raises(ValueError, match=expect):
            cls.to_kind()


def test_module_without_a_task_class_is_rejected() -> None:
    """任务模块里没有任务类 → 报错（别让人以为"放个文件就算注册了"）。"""
    with pytest.raises(ValueError, match="没有任务类"):
        _discovery.declared(_module("app.tasks.empty"))


def test_module_with_two_task_classes_is_rejected() -> None:
    """一个模块两个任务类 → 报错（约定：一个任务一个类）。"""
    first = _task_class("A", module="app.tasks.two")
    second = _task_class("B", module="app.tasks.two")
    with pytest.raises(ValueError, match="多个任务类"):
        _discovery.declared(_module("app.tasks.two", first, second))


# ---------------------------------------------------------------------------
# 执行语义：每次新建实例（资源/状态不跨次残留）
# ---------------------------------------------------------------------------


def test_engine_handler_creates_a_fresh_instance_per_run() -> None:
    """**每次执行新建实例**：上一次运行留下的实例状态/资源不会带到下一次。

    这正是"重复调用时的资源清理"的落点：对象随作用域回收，任务之间不共享可变状态。
    """
    created: list[Task] = []

    class Probe(Task):
        title = "探针"

        def __init__(self) -> None:
            self.marker = object()
            created.append(self)

        def run(self, ctx: object, params: object) -> TaskResult:
            return TaskResult(note="ok")

    handler = Probe.as_handler()
    assert handler(_ctx()) == "ok"
    assert handler(_ctx()) == "ok"

    assert len(created) == 2, "每次执行都该是一个新实例"
    assert created[0] is not created[1]
    assert created[0].marker is not created[1].marker, "实例状态不该被下一次复用"


def test_cancel_hook_is_none_unless_overridden() -> None:
    """没重写 `on_cancel` 就不注册取消钩子（引擎据此跳过）。"""
    plain = _task_class("Plain", module="app.tasks.x")
    assert plain.cancel_hook() is None
    assert plain.to_kind().on_cancel is None


def test_cancel_hook_uses_overridden_method() -> None:
    """重写了 `on_cancel` 才会挂上去。"""
    calls: list[object] = []

    class Cancelable(Task):
        title = "可取消"

        def run(self, ctx: object, params: object) -> TaskResult:
            return TaskResult(note="ok")

        def on_cancel(self, ctx: object) -> None:
            calls.append(ctx)

    hook = Cancelable.cancel_hook()
    assert hook is not None
    hook("ctx")  # type: ignore[arg-type]
    assert calls == ["ctx"]


# ---------------------------------------------------------------------------
# 失败不静默
# ---------------------------------------------------------------------------


def test_broken_module_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务模块导入失败 → **抛出去**，不许静默跳过。

    静默少注册一个 kind 会让用户排队中的该类型任务被判成"类型已不存在"——
    那比启动失败严重得多。
    """
    def boom() -> list[types.ModuleType]:
        raise ImportError("这个任务模块坏了")

    monkeypatch.setattr(_discovery, "task_modules", boom)
    with pytest.raises(ImportError, match="坏了"):
        _discovery.discovered_kinds()


def test_duplicate_key_across_modules_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个模块声明同一个标识（同类名）→ 报错并点出两个模块名。"""
    first = _task_class("Same", module="app.tasks.a")
    second = _task_class("Same", module="app.tasks.b")
    monkeypatch.setattr(
        _discovery,
        "task_modules",
        lambda: [_module("app.tasks.a", first), _module("app.tasks.b", second)],
    )
    with pytest.raises(ValueError, match="两次"):
        _discovery.discovered_kinds()


def test_register_all_is_atomic() -> None:
    """一条不合规 → **整体不登记**，绝不留下"注册了一半"的注册表。"""
    good = JobKind(key="good", title="好任务", lane="cpu", handler=lambda ctx: None)
    bad = JobKind(key="bad", title="坏任务", lane="gpu", handler=lambda ctx: None)
    with pytest.raises(ValueError, match="lane"):
        registry.register_all([good, bad])
    assert registry.registered() == (), "有不合规的就不该登记任何一条"


def test_register_all_rejects_duplicates() -> None:
    """同一批里标识重复 → 报错（两个类抢同一个标识）。"""
    first = JobKind(key="dup", title="甲", lane="cpu", handler=lambda ctx: None)
    second = JobKind(key="dup", title="乙", lane="cpu", handler=lambda ctx: None)
    with pytest.raises(ValueError, match="重复声明"):
        registry.register_all([first, second])


def test_conflicting_redeclaration_is_rejected() -> None:
    """同名但声明不同（有人偷偷改了已有任务的声明）→ 报错，而不是静默覆盖。"""
    tasks_app.register_kinds()
    clash = JobKind(
        key="import",
        title="另一个导入任务",
        lane="cpu",
        handler=lambda ctx: None,
    )
    registry._KINDS["import"] = clash  # 模拟"另一处代码抢了同一个标识"
    with pytest.raises(ValueError, match="不一致"):
        tasks_app.register_kinds()


# ---------------------------------------------------------------------------
# 重复调用：幂等 + 可重建
# ---------------------------------------------------------------------------


def test_register_kinds_is_idempotent() -> None:
    """连调三次：还是那三个，不重复、不报错。"""
    for _ in range(3):
        tasks_app.register_kinds()
    assert [kind.key for kind in api.kinds()] == sorted(EXPECTED_KEYS)


def test_register_kinds_rebuilds_after_clear() -> None:
    """`registry.clear()` 之后能重新注册回来（测试每个用例都会先清空）。"""
    tasks_app.register_kinds()
    registry.clear()
    assert api.kinds() == ()
    tasks_app.register_kinds()
    assert {kind.key for kind in api.kinds()} == EXPECTED_KEYS


def test_kind_identity_is_stable_across_calls() -> None:
    """同一个任务类构造出的 JobKind 是**同一个对象**（handler 是闭包，必须缓存）。

    否则"重复注册是否一致"的判等会误报冲突，启动就会崩。
    """
    cls = _discovery.declared(importlib.import_module("app.tasks.cleanup"))[0]
    assert cls.to_kind() is cls.to_kind()
    assert cls.to_kind().handler is cls.to_kind().handler


def _ctx() -> object:
    """一个够用的假 JobContext（这些用例只关心"几次调用"）。"""

    class _Ctx:
        job_id = 1
        payload: ClassVar[dict[str, object]] = {}

        def report(self, *args: object) -> None: ...

    return _Ctx()
