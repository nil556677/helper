"""节点/步骤装饰：一次 stage 同时发 UI 事件与 trace span。

作者只写两样人话：``label``（显示标题）与 ``group``（归到哪一组）；
id 由「模块.函数.短名」从调用位置自动派生，不再手写 ``"rag.dense_recall"`` 这类字符串。
"""

from __future__ import annotations  # 类型注解延迟求值

import sys  # 取调用帧（id 从这里派生）
import time  # 计时
from collections.abc import Iterator  # 迭代器
from contextlib import AbstractContextManager, contextmanager  # 上下文管理器
from dataclasses import dataclass  # 结果

from app.agent import events  # 事件
from app.agent.context import RunContext, current_run  # 当前 run
from app.agent.contracts import (  # 命名、分组与唯一性
    DuplicateIdError,  # 重复 id 异常
    IdRegistry,  # id 注册表
    Namer,  # 命名器
    StageGroup,  # 步骤分组
)  # 导入结束
from app.core.trace.recorder import trace_span  # span

_REGISTRY = IdRegistry()  # 步骤 id 注册表（两处撞车时第一次跑到就抛）


@dataclass
class StageInfo:  # 步骤附加信息
    """节点可在退出前补 detail，供 UI 显示“找到 N 段”；stage_id 供测试断言派生结果。"""

    stage_id: str  # 自动派生的步骤 id
    detail: str = ""  # 明细


def stage(  # 步骤（在调用处声明 label 与 group）
    label: str,  # 人话标题
    *,
    group: StageGroup | None = None,  # 用户级分组；None = 只埋 trace，不进用户步骤
    name: str = "",  # 同一函数里的第二个步骤才需要短名
    detail: str = "",  # 进入时的明细
) -> AbstractContextManager[StageInfo]:  # 上下文管理器
    """id 从调用位置派生；同一函数里有第二个步骤时必须给 ``name=``，否则 id 撞车直接抛。"""
    frame = sys._getframe(1)  # 调用者帧（必须在这里取：__enter__ 时已经是 contextlib 的帧）
    stage_id = Namer.stage_id(Namer.from_frame(frame), name)  # 自动派生步骤 id
    where = f"{frame.f_globals.get('__name__') or ''}:{frame.f_lineno}"  # 来源位置（重复判据）
    try:  # 重复判据在第一次跑到该处时就生效
        _REGISTRY.claim(stage_id, where)  # 登记：同一处每轮重复调用合法，两处撞车直接抛
    except DuplicateIdError as exc:  # id 撞车
        raise DuplicateIdError(f"{exc}；同一函数里的第二个步骤请写 name=") from exc  # 补上改法
    return _run_stage(stage_id, label, group, detail)  # 交给生成器


@contextmanager
def _run_stage(  # 真正发事件 / 建 span 的生成器
    stage_id: str,  # 步骤 id
    label: str,  # 标题
    group: StageGroup | None,  # 分组
    detail: str = "",  # 明细
) -> Iterator[StageInfo]:  # 迭代器
    """进入发 running，成功发 done，失败发 failed 并重新抛出。"""
    ctx = current_run()  # 当前上下文
    started = time.perf_counter()  # 开始
    info = StageInfo(stage_id=stage_id, detail=detail)  # 可写明细
    group_key = group.value if group is not None else ""  # 事件里用字符串
    if ctx is not None:  # 有 run 才发
        ctx.check_cancelled()  # 开始前检查取消
        ctx.emit(events.stage(stage_id, "running", title=label, detail=detail, group=group_key))  # running
    try:  # 包住真实步骤
        with trace_span(stage_id, "node", input={"detail": detail}):  # trace span
            yield info  # 执行
    except BaseException as exc:  # 失败/取消
        ms = int((time.perf_counter() - started) * 1000)  # 耗时
        if ctx is not None:  # 有 run
            ctx.emit(  # 失败事件
                events.stage(  # 构造
                    stage_id,  # 步骤 id
                    "failed",  # 状态
                    title=label,  # 标题
                    detail=str(exc)[:200],  # 明细（截断）
                    duration_ms=ms,  # 耗时
                    group=group_key,  # 分组
                )  # 构造结束
            )  # emit 结束
            _record(ctx, stage_id, group_key, label, str(exc)[:200], "failed", ms)  # 记步骤
        raise  # 继续抛
    else:  # 成功
        ms = int((time.perf_counter() - started) * 1000)  # 耗时
        if ctx is not None:  # 有 run
            ctx.check_cancelled()  # 结束后检查取消
            ctx.emit(  # 完成事件
                events.stage(  # 构造
                    stage_id,  # 步骤 id
                    "done",  # 状态
                    title=label,  # 标题
                    detail=info.detail,  # 明细
                    duration_ms=ms,  # 耗时
                    group=group_key,  # 分组
                )  # 构造结束
            )  # emit 结束
            _record(ctx, stage_id, group_key, label, info.detail, "done", ms)  # 记步骤


def _record(  # 记一条用户级步骤
    ctx: RunContext,  # 运行上下文
    stage_id: str,  # 步骤 id
    group: str,  # 分组（空 = 不记）
    label: str,  # 标题
    detail: str,  # 明细
    status: str,  # done/failed
    ms: int,  # 耗时
) -> None:  # 无返回
    """没有分组的步骤（如 finalize）只埋 trace，不进用户能看到的步骤列表。"""
    if not group:  # 纯埋点步骤
        return  # 不记
    ctx.steps.append(  # 追加
        {  # 步骤
            "id": stage_id,  # 步骤 id
            "group": group,  # 分组
            "title": label,  # 标题
            "detail": detail,  # 明细
            "status": status,  # 状态
            "ms": ms,  # 耗时
        }  # 步骤结束
    )  # 追加结束
