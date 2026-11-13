"""worker 检查点接线（E2/E5 运行时）：在 worker 的服务事件循环里一次性初始化。

``setup_loop`` 在**最终运行对话图的那个事件循环**里被调用（AsyncSqliteSaver 绑定该 loop）：
- 建 AsyncSqliteSaver；
- 用它编译父图并 ``assembly.set_graph``（worker 的对话走这张 checkpointed 图）；
- 起一个后台任务做启动对账（``reconcile_once``）与周期性 ``checkpoint.prune``。

注意：这段是**运行时接线**，需在真 worker 里验证；它就在该进程的单事件循环里跑。
"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 后台任务
import contextlib  # 抑制
from pathlib import Path  # 路径

from app.agent import (  # 相关模块
    assembly,  # 装配器（注入图）
    checkpoint,  # 检查点
    reconcile,  # 对账
)

#: prune 周期（秒）。worker 单机单循环，避免 checkpoint 无限增长。
_PRUNE_INTERVAL_S = 300
#: 崩溃残留判定阈值（秒）。
_STALE_S = reconcile.STALE_S

#: 常驻维护任务的引用：**必须持有**。``create_task`` 的返回值没人引用时，任务可能在
#: 等待期间被 GC 掉（"Task was destroyed but it is pending"），prune/对账就静默停摆。
_MAINTENANCE_TASKS: set[asyncio.Task] = set()


async def setup_loop(  # 在服务 loop 里一次性初始化
    *,
    checkpoint_path: str | Path | None = None,  # 显式 checkpoint 库路径
    keep: int = 50,  # 每会话保留 checkpoint 数
    prune_interval_s: float = _PRUNE_INTERVAL_S,  # prune 周期
) -> None:  # 无返回
    """建 saver + 编译图并注入 + 起对账/prune 后台任务。"""
    # 注意：agent 路径的 AsyncDB（stream.ASYNC_DB_ENABLED）暂不启用——真实 worker 下
    # ack 之后图会挂住（待专项排查），先保持同步路径以保证聊天可用。
    saver = await checkpoint.build_async_saver(path=checkpoint_path)  # 建异步 saver
    graph = assembly.build(checkpointer=saver)  # 编译路由父图（挂 saver）
    assembly.set_graph(graph)  # 注入为 worker 对话图
    loop = asyncio.get_running_loop()  # 当前 loop
    task = loop.create_task(_maintenance(checkpoint_path, keep, prune_interval_s))  # 起维护任务
    _MAINTENANCE_TASKS.add(task)  # 保活（见 _MAINTENANCE_TASKS 说明）
    task.add_done_callback(_MAINTENANCE_TASKS.discard)  # 完成即移除
    with contextlib.suppress(Exception):  # 对账失败不能阻止服务
        reconcile.reconcile_once(stale_s=_STALE_S)  # 启动对账：崩溃残留标 interrupted
    with contextlib.suppress(Exception):  # 补跑失败不能阻止服务
        from app.agent.approval import service as approval_service  # 确认服务

        await approval_service.catchup_approved()  # 补齐"已确认但未执行"的查询记录（崩溃残留）


async def _maintenance(  # 后台维护
    checkpoint_path: str | Path | None,  # 库路径
    keep: int,  # 保留数
    prune_interval_s: float,  # 周期
) -> None:  # 常驻
    """按周期 prune checkpoint；对账也定期补一次（防长会话堆积残留）。"""
    while True:  # 常驻
        await asyncio.sleep(float(prune_interval_s))  # 等一个周期
        with contextlib.suppress(Exception):  # 各自失败不影响另者
            checkpoint.prune(keep=keep, path=checkpoint_path)  # 修剪 checkpoint
        with contextlib.suppress(Exception):  # 失败不影响
            reconcile.reconcile_once(stale_s=_STALE_S)  # 周期对账
