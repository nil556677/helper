"""任务基类：**一个任务 = 一个类**（技术方案 §8.2）。

写法（一个任务一个模块，模块里就这一个类）：

```python
class ImportTask(Task):
    title = "导入资料"          # 界面显示名
    lane = "cpu"

    class Params(BaseModel):    # 参数形状（统一校验；不需要参数就不写）
        path: str
        name: str = ""

    def run(self, ctx: JobContext, params: "ImportTask.Params") -> TaskResult:
        ...
        return TaskResult(note="切出 12 个片段")
```

四条约定（都能在启动校验或运行期兜住）：

1. **任务标识 = 类名**：`ImportTask` → ``import``、`OCRCleanupTask` → ``ocr_cleanup``
   （去掉结尾的 ``Task``、驼峰转下划线）。**不用手写 key**，少一处能写错的地方。
2. **执行方法必须实现**：``run`` 是 ``@abstractmethod``，没实现就实例化不了（``TypeError`` 直接点名）。
3. **返回值统一**：``run`` 必须返回 :class:`TaskResult`（一句话结果 + 可选详情）。
   返回别的东西 = 任务失败并明确报错——"结果格式统一"才有意义，否则界面没法统一展示。
4. **参数统一**：在类里声明 ``Params``（pydantic 模型）。入队那一刻就校验并规范化，
   非法参数**排队前**就被拒；执行时按同一形状解析后交给 ``run``。

**实例化语义：每次执行新建一个实例**（见 :meth:`Task.as_handler`）。
重试、重复调用、并发都不会把上一轮的状态或资源带进下一轮；跨次复用的重资源
（ONNX session 等）放**模块级惰性单例**，不放 ``__init__``。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from loguru import logger  # noqa: F401 - 保留给子类/调试用；本模块自身不记日志
from pydantic import BaseModel, ValidationError

from app.core.jobs.model import JobContext, JobError
from app.core.jobs.registry import JobKind, validate


@dataclass(frozen=True)
class TaskResult:
    """任务执行结果（**统一格式**：每个任务的 ``run`` 都返回它）。

    参数：
        note: 一句话结果（进 ``jobs.note``，任务页直接显示）。
        detail: 可选：更详细的一段说明（进事件时间线，界面"详情"里折叠展示）。
    """

    note: str = ""
    detail: str = ""


#: 类名 → 任务标识的分词：小写/数字后面跟大写处断开，或一串大写后面跟小写处断开。
_KEY_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def key_from_class_name(name: str) -> str:
    """类名 → 任务标识（``ImportTask`` → ``import``；``OCRSyncTask`` → ``ocr_sync``）。"""
    base = name[:-4] if name.endswith("Task") and len(name) > 4 else name
    return _KEY_SPLIT.sub("_", base).lower()


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 的校验错误变成一句人话（点名字段）。"""
    parts: list[str] = []
    for item in exc.errors():
        field = ".".join(str(part) for part in item.get("loc", ())) or "参数"
        parts.append(f"{field}：{item.get('msg', '不合法')}")
    return "；".join(parts)


def _run_coroutine(coro: Any) -> Any:
    """在当前线程把协程跑完（异步任务）。

    工作线程里没有事件循环 → 直接 ``asyncio.run``。
    若当前线程**已经有**循环在跑（inline 测试模式跑在 async 用例里），
    就换一条自己的线程跑——否则 ``run_until_complete`` 会直接报错。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=worker, name="task-async", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class Task(ABC):
    """一种后台任务（子类见模块说明）。"""

    #: 界面上显示的名字（任务页那一行）。**必填**。
    title: ClassVar[str] = ""

    #: 泳道：``cpu`` 并发 1（纯 Python 重活），``io`` 并发 2。
    lane: ClassVar[str] = "cpu"

    #: 参数形状（pydantic 模型）。声明了就在入队时统一校验；不需要参数可以不写。
    Params: ClassVar[type[BaseModel] | None] = None

    #: 是否幂等。**不幂等的任务永不自动重试**（重跑会造成重复副作用）。
    idempotent: ClassVar[bool] = True

    #: 业务失败重试上限（只数 fails，不数崩溃）。
    max_attempts: ClassVar[int] = 3

    #: 租约时长：一批最长可能跑多久（handler 每次 report 都会续租）。
    lease_s: ClassVar[float] = 60.0

    #: 独占资源名（如 ``"onnx"``）：同名资源同时只允许一个任务在跑。
    resource: ClassVar[str | None] = None

    #: payload 结构版本。**改过 payload 结构就 +1**：队列里还躺着旧格式时会被明确判失败。
    payload_version: ClassVar[int] = 1

    @abstractmethod
    def run(self, ctx: JobContext, params: Any) -> TaskResult:
        """干活（在**工作线程**里跑）。

        参数：
            ctx: 任务上下文（``payload`` / ``report()`` / ``should_stop()``）。
            params: 按 ``Params`` 解析好的参数（没声明 Params 时是原始 dict）。
        返回：:class:`TaskResult`（**必须是它**）。
        """

    def on_cancel(self, ctx: JobContext) -> None:  # noqa: B027 - 故意可选：不是所有任务都需要清理半成品
        """可选钩子：取消生效后清理自己的半成品（默认什么都不做）。

        **只拿得到 ``ctx``**（引擎在任务线程之外调用它），所以要清理什么必须能从
        ``ctx.payload`` 推出来——不要依赖运行期实例上的临时状态。
        """

    # ---------------- 参数：入队时校验、执行时解析 ----------------

    @classmethod
    def normalize_params(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """入队路径：校验并规范化参数（不合法抛 ``ValueError``）。

        放在入队那一刻做：让"参数不对"立刻返回给调用方，而不是排半天队再失败。
        """
        model = cls.Params
        if model is None:
            return dict(payload or {})
        try:
            return model.model_validate(payload or {}).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError(
                f"{cls.title or cls.__name__} 的参数不合法：{_format_validation_error(exc)}"
            ) from exc

    @classmethod
    def parse_params(cls, payload: dict[str, Any]) -> Any:
        """执行路径：把库里的 payload 解析成 ``Params``（不合法抛 ``JobError``，判不可重试）。

        为什么执行期还要再校验一次：库里可能躺着**老格式**的排队任务（或有人绕过 api 直接写库），
        那种任务应当明确判失败并说清原因，而不是拿错参数硬跑。
        """
        model = cls.Params
        if model is None:
            return dict(payload or {})
        try:
            return model.model_validate(payload or {})
        except ValidationError as exc:
            raise JobError(
                f"任务参数不合法（可能是旧格式）：{_format_validation_error(exc)}",
                kind="bad_request",
                retryable=False,
            ) from exc

    # ---------------- 给引擎的接线 ----------------

    @classmethod
    def as_handler(cls) -> Callable[[JobContext], str | None]:
        """给引擎的 handler：**每次执行新建实例**，并统一处理结果与资源。

        统一做的事（所以每个任务不用各写一遍）：
        1. 解析参数（老格式/非法 → 明确判失败）；
        2. 执行 ``run``（``async def`` 也支持）；
        3. **校验返回值必须是** :class:`TaskResult`；
        4. 结果详情写进事件时间线（界面"详情"里能看到）。
        """

        def call(ctx: JobContext) -> str:
            params = cls.parse_params(dict(ctx.payload or {}))
            outcome = cls().run(ctx, params)
            if inspect.isawaitable(outcome):
                outcome = _run_coroutine(outcome)
            result = _require_result(cls, outcome)
            if result.detail:
                from app.core.jobs import store

                store.record_result(ctx.job_id, detail=result.detail)
            return result.note

        # 起个带类名的名字：日志与异常栈里能一眼看出是哪个任务（否则只有 `<locals>.call`）
        call.__name__ = "run"
        call.__qualname__ = f"{cls.__name__}.run"
        return call

    @classmethod
    def cancel_hook(cls) -> Callable[[JobContext], None] | None:
        """取消钩子：子类没重写就返回 None（引擎据此跳过）。"""
        if cls.on_cancel is Task.on_cancel:
            return None

        def call(ctx: JobContext) -> None:
            cls().on_cancel(ctx)

        return call

    @classmethod
    def to_kind(cls) -> JobKind:
        """变成引擎认识的 :class:`JobKind`（**顺带把合规检查做全**）。

        结果按类缓存：``handler`` 是闭包，不缓存的话每次构造都是新对象，
        注册表做"重复注册是否一致"的判等时会误报冲突。

        异常：
            TypeError: 抽象方法没实现（``run`` 是必须实现的）。
            ValueError: 没声明 ``title``、``Params`` 不是 pydantic 模型、或字段形状不合法。
        """
        cached = _KIND_CACHE.get(cls)
        if cached is not None:
            return cached

        if inspect.isabstract(cls):
            missing = "、".join(sorted(getattr(cls, "__abstractmethods__", ())))
            raise TypeError(
                f"任务类 {cls.__name__} 没有实现必须实现的方法：{missing or 'run'}"
            )
        if not cls.title:
            raise ValueError(f"任务类 {cls.__name__} 没有声明 title（界面上显示的名字）")
        if cls.Params is not None and not (isinstance(cls.Params, type) and issubclass(cls.Params, BaseModel)):
            raise ValueError(
                f"任务类 {cls.__name__} 的 Params 必须是一个 pydantic 模型（或干脆不写）"
            )

        kind = JobKind(
            key=key_from_class_name(cls.__name__),
            title=cls.title,
            lane=cls.lane,
            handler=cls.as_handler(),
            idempotent=cls.idempotent,
            max_attempts=cls.max_attempts,
            lease_s=cls.lease_s,
            resource=cls.resource,
            on_cancel=cls.cancel_hook(),
            payload_version=cls.payload_version,
            normalize=cls.normalize_params,
        )
        # 字段级合规（标识形状 / lane / lease_s …）也在这里做：
        # 校验只有一处，任何拿到 JobKind 的地方都已经被检查过。
        validate(kind)
        _KIND_CACHE[cls] = kind
        return kind


def _require_result(cls: type[Task], value: Any) -> TaskResult:
    """强制"任务结果统一格式"：不是 :class:`TaskResult` 就明确判失败。

    用 ``JobError``（不可重试）而不是当"内部错误"崩掉：返回格式不对是**代码 bug**，
    重试三次没有任何意义，只会让用户多等；一句明确的话比三条 traceback 有用。
    """
    if isinstance(value, TaskResult):
        return value
    raise JobError(
        f"任务 {cls.__name__}.run 必须返回 TaskResult（统一结果格式），"
        f"实际返回了 {type(value).__name__}",
        kind="bad_request",
        retryable=False,
    )


#: 任务类 → JobKind 的缓存（见 :meth:`Task.to_kind` 的说明）。
_KIND_CACHE: dict[type[Task], JobKind] = {}


def clear_kind_cache() -> None:
    """清掉 JobKind 缓存（**只给测试用**：改了类属性后需要重新构造）。"""
    _KIND_CACHE.clear()
