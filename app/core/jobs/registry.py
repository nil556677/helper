"""kind（任务类型）注册表：**引擎不认识任何具体功能**。

方向是"功能反过来注册给引擎"（避免 `core` 依赖具体功能）：

```python
jobs.register(JobKind(
    key="import", title="导入资料", lane="cpu",
    handler=import_handler, idempotent=True, max_attempts=3, lease_s=120,
))
```

`lane`（泳道）决定并发与"给界面让路"的策略：``cpu`` 泳道并发 1（实测 4 个纯 Python
任务并发会让界面延迟 p95 到 113ms），``io`` 泳道并发 2。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.core.jobs.model import JobContext

#: 允许的泳道
LANES = ("cpu", "io")

#: key 的形状：小写字母开头，只含小写字母/数字/下划线（进 SQL、进日志、进 payload 都要稳）。
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class JobKind:
    """一种任务的声明（功能侧只写这个）。

    参数：
        key: 任务类型标识（``import`` / ``embed`` / ``ocr`` / ``rebuild`` …）。
        title: 界面上显示的名字（「导入资料」）。
        lane: ``cpu`` 或 ``io``——决定并发上限与让路策略。
        handler: 真正干活的**同步**函数，在工作线程里跑；返回值是给用户看的一句话（可空）。
        idempotent: 是否幂等。**不幂等的不给自动重试**（重跑会造成重复副作用）。
        max_attempts: 业务失败重试上限（只数 ``fails``，不数崩溃）。
        lease_s: 租约时长（一批最长可能跑多久）——OCR/向量化按自己的批次时长声明。
        resource: 独占资源名（如 ``"onnx"``）：同名资源同时只允许一个任务在跑。
        on_cancel: 可选钩子：取消生效后清理自己的半成品（引擎只管状态）。
        payload_version: payload 结构版本。**改过 payload 结构就 +1**：队列里还躺着旧格式的任务时，
            引擎会在认领那一刻判失败并让人重新发起，而不是拿错参数硬跑。
        normalize: 可选：把入队时的原始参数**统一校验/规范化**成入库的形状
            （任务类自己提供，core 不认识任何具体参数形状）。不合法时抛 ``ValueError``。
    """

    key: str
    title: str
    lane: str
    handler: Callable[[JobContext], str | None]
    idempotent: bool = True
    max_attempts: int = 3
    lease_s: float = 60.0
    resource: str | None = None
    on_cancel: Callable[[JobContext], None] | None = None
    payload_version: int = 1
    normalize: Callable[[dict[str, object]], dict[str, object]] | None = None


#: 已注册的 kind（key → 声明）
_KINDS: dict[str, JobKind] = {}


def validate(spec: JobKind) -> None:
    """校验一条 kind 声明是否合规（**纯检查，不动注册表**）。

    为什么要独立出来：装配层在**启动时批量加载**任务声明，必须"全部合规才登记"——
    先校验完再写注册表，避免留下"注册了一半"的半成品状态。

    异常：
        ValueError: 任一字段不合法。消息里带上具体字段与收到的值（改起来不用猜）。
    """
    problems: list[str] = []
    if not isinstance(spec.key, str) or not KEY_PATTERN.match(spec.key):
        problems.append(f"key 必须匹配 {KEY_PATTERN.pattern}（收到 {spec.key!r}）")
    if not isinstance(spec.title, str) or not spec.title.strip():
        problems.append("title 不能为空（任务页要显示它）")
    if spec.lane not in LANES:
        problems.append(f"lane 只能是 {LANES}（收到 {spec.lane!r}）")
    if not callable(spec.handler):
        problems.append("handler 必须可调用")
    if not isinstance(spec.max_attempts, int) or spec.max_attempts < 1:
        problems.append(f"max_attempts 必须是 >=1 的整数（收到 {spec.max_attempts!r}）")
    if not isinstance(spec.lease_s, (int, float)) or spec.lease_s <= 0:
        problems.append(f"lease_s 必须 > 0（收到 {spec.lease_s!r}）")
    if not isinstance(spec.payload_version, int) or spec.payload_version < 1:
        problems.append(f"payload_version 必须是 >=1 的整数（收到 {spec.payload_version!r}）")
    if spec.resource is not None and not str(spec.resource).strip():
        problems.append("resource 要么不填，要么给一个非空名字")
    if spec.normalize is not None and not callable(spec.normalize):
        problems.append("normalize 要么不填，要么给一个可调用的参数校验函数")
    if problems:
        raise ValueError(f"任务类型 {spec.key!r} 声明不合法：" + "；".join(problems))


def register(spec: JobKind) -> None:
    """注册一种任务（先校验）。

    异常：
        ValueError: 声明不合法，或同一个 key 注册了两次（**早报错**：重复注册意味着
            两处代码都想当这个 kind 的主人，运行期才发现就晚了）。
    """
    validate(spec)
    if spec.key in _KINDS:
        raise ValueError(f"任务类型 {spec.key!r} 已经注册过了（每处只许注册一次）")
    _KINDS[spec.key] = spec


def register_all(specs: Iterable[JobKind]) -> None:
    """批量注册（**原子**：全部校验通过、且与已有 key 无冲突，才写进注册表）。

    启动时的自动加载走这里：任一条不合规就整体不登记，
    绝不留下"只注册了前两条"的中间状态（那种状态会让一半任务凭空消失）。
    """
    batch = list(specs)
    for spec in batch:
        validate(spec)
    duplicated = [spec.key for spec in batch if spec.key in _KINDS]
    if duplicated:
        raise ValueError(f"这些任务类型已经注册过了：{sorted(set(duplicated))}")
    batch_keys = [spec.key for spec in batch]
    repeated = sorted({key for key in batch_keys if batch_keys.count(key) > 1})
    if repeated:
        raise ValueError(f"同一批里重复声明了任务类型：{repeated}")
    for spec in batch:
        _KINDS[spec.key] = spec


def get(key: str) -> JobKind:
    """取一种任务的声明。

    异常：
        KeyError: 没注册过——消息是给人看的（谁忘了注册一眼能看出）。
    """
    try:
        return _KINDS[key]
    except KeyError as exc:
        raise KeyError(f"没有注册的任务类型：{key}（是不是忘了在启动时 jobs.register？）") from exc


def registered() -> tuple[JobKind, ...]:
    """所有已注册的 kind（按 key 排序，便于稳定输出与测试）。"""
    return tuple(_KINDS[key] for key in sorted(_KINDS))


def clear() -> None:
    """清空注册表（**只给测试用**：注册表是进程级的，用例之间必须互不影响）。"""
    _KINDS.clear()
