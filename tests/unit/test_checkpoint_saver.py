"""检查点 saver 的生命周期：不能被 GC 提前关掉连接（否则首次使用就炸）。"""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent import checkpoint


class _State(TypedDict):
    x: int


def _graph(saver):
    """一个最小图：只把 x + 1，用来验证 checkpoint 能真正读写。"""
    builder = StateGraph(_State)
    builder.add_node("inc", lambda state: {"x": state["x"] + 1})
    builder.add_edge(START, "inc")
    builder.add_edge("inc", END)
    return builder.compile(checkpointer=saver)


def test_saver_survives_gc_and_can_checkpoint(tmp_path: Path) -> None:
    """回归：``build_async_saver`` 曾让 contextmanager 被 GC → 连接被关 → 首次使用报
    ``RuntimeError: threads can only be started once``（启动正常、第一次提问才炸）。
    """

    async def main() -> tuple[int, int, bool]:
        saver = await checkpoint.build_async_saver(path=tmp_path / "cp.sqlite")
        gc.collect()  # 模拟运行中任意时刻的 GC（旧代码就是在这里丢掉连接）
        await asyncio.sleep(0.05)  # 给异步生成器 finalize 的机会

        graph = _graph(saver)
        cfg = {"configurable": {"thread_id": "t1"}}
        r1 = await graph.ainvoke({"x": 0}, cfg)  # 第一次：会走 saver.setup()
        r2 = await graph.ainvoke({"x": 10}, cfg)  # 第二次：走 checkpoint 读写
        alive = saver.conn._thread.is_alive()  # aiosqlite 工作线程还活着
        await checkpoint.close_async_savers()  # 同一 loop 里关，避免线程泄漏
        return int(r1["x"]), int(r2["x"]), alive

    r1, r2, alive = asyncio.run(main())
    assert (r1, r2) == (1, 11)  # 图正常跑完
    assert alive is True  # 连接线程没有被 GC 掐掉


def test_close_async_savers_is_idempotent(tmp_path: Path) -> None:
    """关两次不报错（退出路径会被重复调用）。"""

    async def main() -> None:
        await checkpoint.build_async_saver(path=tmp_path / "cp2.sqlite")
        await checkpoint.close_async_savers()
        await checkpoint.close_async_savers()  # 幂等

    asyncio.run(main())
