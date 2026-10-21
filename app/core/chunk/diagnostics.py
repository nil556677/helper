"""切分诊断：**降级/异常不许静默**。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
切分过程中的"降级"（硬切、跳过图片、合并短块）与异常（切完仍超预算）都先记进
:class:`DiagnosticSink`，结束时由上层一次性转成任务事件（``job_events``），
在界面任务详情里可见，而不是只写日志。

## 输入 / 输出

- 输入：``code`` + :class:`Severity` + 一句话消息 + 可选细节。
- 输出：按 ``code`` 聚合后的 :class:`Diagnostic` 列表；也可汇总成一句 note。

## 关键约定

1. **按 code 聚合**：同类问题只留首条样本 + 计数，否则一份坏文档能刷出上万条；
2. **severity 是行为契约**：``ERROR`` = 数据不可用/有丢失风险（上层据此判失败），
   ``WARNING`` = 已降级但必须让用户知道，``INFO`` = 正常但值得记录；
3. **输出顺序稳定**：:meth:`DiagnosticSink.items` 先按严重度、再按 ``code`` 排序，
   测试与界面都不依赖插入顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """诊断级别。

    值直接写进 ``job_events``（字符串），所以用 ``StrEnum``；级别决定上层的动作：
    是否让任务失败、是否在界面上提示。
    """

    ERROR = "error"
    """数据不可用 / 有丢失风险 → 任务失败。"""

    WARNING = "warning"
    """已降级处理，但必须让用户知道。"""

    INFO = "info"
    """正常但值得记录（例如跳过了图片）。"""


@dataclass(frozen=True)
class Diagnostic:
    """一条诊断：一个 ``code`` 的**首条样本 + 聚合计数**。

    不可变：聚合时不是改旧对象，而是新建一条（``count`` 加一），
    这样已经把列表交出去的上层不会被后续写入影响。
    """

    code: str
    """机器判据（写进 ``job_events.kind``，界面据此分组）。"""

    severity: Severity
    message: str
    """一句话（界面直接显示）。"""

    detail: str = ""
    """细节/位置（界面折叠显示）。"""

    count: int = 1
    """同类诊断出现了几次（聚合后）。"""


@dataclass
class DiagnosticSink:
    """按 ``code`` 聚合的诊断收集器。

    不保证线程安全：一个 :class:`~app.core.chunk.core.Splitter` 实例只在一个导入任务里用，
    生命周期内单线程写入即可，不需要为此加锁。
    """

    _items: dict[str, Diagnostic] = field(default_factory=dict)

    def add(self, code: str, severity: Severity, message: str, detail: str = "") -> None:
        """记一条；同 code 只保留首条样本并计数。

        参数：
            code: 机器判据（稳定字符串，界面/测试按它断言）。
            severity: 严重度；同 code 后续再报**不覆盖**首条的级别。
            message: 给用户的一句话。
            detail: 可选细节（首个样本的现场，通常是原文片段/位置）。
        返回：无。
        """
        existing = self._items.get(code)
        if existing is None:
            self._items[code] = Diagnostic(code, severity, message, detail)
        else:
            # 保留**首条**的 message/detail：第一条通常是原始现场；
            # 后续只把次数加上去，避免同一问题刷屏
            self._items[code] = Diagnostic(
                existing.code,
                existing.severity,
                existing.message,
                existing.detail,
                existing.count + 1,
            )

    def extend(self, other: DiagnosticSink) -> None:
        """合并另一个收集器（例如多个 block 的结果）。

        参数：
            other: 另一个 sink；按它的 ``count`` 逐次 :meth:`add`，
                保持"同 code 聚合计数、保留首条样本"的语义。
        返回：无。
        """
        for item in other._items.values():
            for _ in range(item.count):
                self.add(item.code, item.severity, item.message, item.detail)

    def items(self) -> list[Diagnostic]:
        """全部诊断（按严重度排序：error → warning → info）。

        返回：新列表；同一严重度内按 ``code`` 升序（输出稳定，便于测试/展示）。
        """
        order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
        return sorted(self._items.values(), key=lambda item: (order[item.severity], item.code))

    def has_errors(self) -> bool:
        """是否有 error 级诊断（上层据此决定任务是否失败）。"""
        return any(item.severity is Severity.ERROR for item in self._items.values())

    def summary(self) -> str:
        """一句话摘要（写进任务 note）。

        返回：形如 ``"2 条错误、3 条告警"``；**按发生次数统计**（同 code 的
            ``Diagnostic.count`` 累加）；没有任何 error/warning 时返回空串
            （INFO 不占 note——"正常但值得记录"的内容在详情里看）。
        """
        if not self._items:
            return ""
        errors = sum(
            item.count for item in self._items.values() if item.severity is Severity.ERROR
        )
        warnings = sum(
            item.count for item in self._items.values() if item.severity is Severity.WARNING
        )
        parts: list[str] = []
        if errors:
            parts.append(f"{errors} 条错误")
        if warnings:
            parts.append(f"{warnings} 条告警")
        return "、".join(parts)
