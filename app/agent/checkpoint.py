"""E2 检查点：SQLite 检查点的持久化机制（读取/修剪/配置）。

**选型说明**：本项目图节点是异步的，driver 必须 ``await graph.ainvoke``，而同步
``SqliteSaver`` 不支持异步读写（会 raise ``NotImplementedError``）。因此**生产侧要用
``AsyncSqliteSaver``（worker 单事件循环）**。

本模块目前提供（同步、可测的机制层）：
- ``config_for``：driver 追加 thread_id + 元数据的片段（已接进 driver）；
- ``latest_state``：同步读某线程最新状态（用同步 ``graph.get_state``，本地工具/对账用）；
- ``prune``：按线程只保留最近 N 个 checkpoint（对 checkpoint 库直接 SQL）。

**真挂载（AsyncSqliteSaver 编译进 worker 父图）**：见方案 §5/E2——因 AsyncSaver 绑定单个
事件循环、而共享装配的 ``_graph()`` 是同步接缝，无法在单测/批处理多循环下无副作用挂载；
需在真 worker 单循环环境完成（列为 E2 收尾项，避免破坏共享装配与现有测试）。
"""

from __future__ import annotations  # 类型注解延迟求值

import contextlib  # 关闭 saver 时抑制异常
import sqlite3  # 清理 SQL
from pathlib import Path  # 路径
from typing import Any  # 任意值

from app.core import paths  # 数据目录

#: **必须保活**：``AsyncSqliteSaver.from_conn_string()`` 是 ``@asynccontextmanager``，
#: 它内部用 ``async with aiosqlite.connect(...)``。只调 ``__aenter__`` 而把 cm 留在局部变量里，
#: 函数一返回 cm 就没引用了——GC 时异步生成器被 finalize，``async with`` 退出 → **连接被关、
#: aiosqlite 工作线程停止**。而 saver 手里仍留着这个死连接，第一次真正使用（langgraph 的
#: ``setup()``）会看到线程已死并再次 ``await conn`` → 二次 ``thread.start()`` →
#: ``RuntimeError: threads can only be started once``（线上现象：启动正常、第一次提问才炸）。
_SAVER_CONTEXTS: list[Any] = []


async def build_async_saver(path: str | Path | None = None) -> Any:  # 在 running loop 建 AsyncSaver
    """生产（worker 单循环）用：异步 SqliteSaver。

    必须在**最终运行该图的事件循环**里调用（AsyncSqliteSaver 绑定 loop）。
    返回可挂 ``compile(checkpointer=)`` 的实例。
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # 异步 saver

    target = Path(path) if path is not None else checkpoints_path()  # 库文件
    target.parent.mkdir(parents=True, exist_ok=True)  # 建目录
    cm = AsyncSqliteSaver.from_conn_string(str(target))  # 上下文管理器
    saver = await cm.__aenter__()  # 进入（绑定当前 loop）
    _SAVER_CONTEXTS.append(cm)  # **保活**：不能让它被 GC（见 _SAVER_CONTEXTS 说明）
    return saver  # 返回


async def close_async_savers() -> None:  # 关闭所有 saver 连接
    """退出时关掉所有 saver 的连接（幂等；尽力而为，失败不影响退出）。

    用 :meth:`__aexit__` 关：它会走 ``async with aiosqlite.connect(...)`` 的退出分支，
    正常 close 连接、停掉工作线程，避免留下 ``checkpoints.sqlite-wal/-shm``。
    """
    contexts = list(_SAVER_CONTEXTS)  # 取快照
    _SAVER_CONTEXTS.clear()  # 先清空（幂等）
    for cm in contexts:  # 逐个关
        with contextlib.suppress(Exception):  # 退出阶段不能让清理异常冒出去
            await cm.__aexit__(None, None, None)  # 关连接


def checkpoints_path() -> Path:  # checkpoint 库文件
    return paths.data_dir() / "checkpoints.sqlite"  # data/checkpoints.sqlite


def config_for(conversation_id: str, *, turn_id: str = "", attempt: int = 1) -> dict[str, Any]:  # config 片段
    """构造 driver 追加到 config 的 thread_id / metadata（与现有 run_ctx 并存）。"""
    return {  # 返回
        "configurable": {"thread_id": conversation_id},  # 线程
        "metadata": {"turn_id": turn_id, "attempt": int(attempt)},  # 元数据（对账定位）
    }  # 返回结束


def latest_state(graph: Any, conversation_id: str) -> Any:  # 读最新状态
    """同步读取某线程（会话）的最新 checkpoint 状态（LangGraph 快照）。"""
    return graph.get_state({"configurable": {"thread_id": conversation_id}})  # 返回快照


def prune(keep: int = 50, path: str | Path | None = None) -> int:  # 按线程清理
    """每个 thread 只保留最近 ``keep`` 个 checkpoint；返回删除条数。

    直接读写 checkpoint 库（框架表：checkpoints / checkpoint_writes）。由 worker 周期调用。
    """
    target = Path(path) if path is not None else checkpoints_path()  # 库文件
    conn = sqlite3.connect(str(target))  # 连接
    removed = 0  # 删除数
    try:  # 有异常也要关
        if not _table_exists(conn, "checkpoints"):  # 库还没建过表
            conn.commit()  # 无操作也提交（建空写）
            return 0  # 无 checkpoint 可清
        rows = conn.execute(  # 各线程 checkpoint 数
            "SELECT thread_id, COUNT(*) AS c FROM checkpoints GROUP BY thread_id"  # 分组
        ).fetchall()  # 行
        for thread_id, count in rows:  # 逐线程
            if int(count) <= keep:  # 没超
                continue  # 跳过
            keep_ids = conn.execute(  # 最近 keep 个的 checkpoint_id（降序）
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?"
                " ORDER BY checkpoint_id DESC LIMIT ?",  # 排序
                (thread_id, int(keep)),  # 参数
            ).fetchall()  # 保留 id
            if not keep_ids:  # 空
                continue  # 跳过
            cutoff = keep_ids[-1][0]  # 第 keep 个（最旧保留点）
            cur = conn.execute(  # 删除更旧的 checkpoints
                "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id < ?",  # 条件
                (thread_id, cutoff),  # 参数
            )  # 删除
            removed += int(cur.rowcount)  # 累计
            _delete_cleaned_writes(conn, thread_id, cutoff)  # 顺带删关联 writes/blobs
        conn.commit()  # 提交
    finally:  # 关连接
        conn.close()  # 关
    return removed  # 返回


def _delete_cleaned_writes(conn: sqlite3.Connection, thread_id: str, cutoff: str) -> None:  # 删关联行
    """删除 checkpoints 的旁表里 <= 已删 checkpoint 的数据（表可能不存在，逐个容错）。

    **表名随 LangGraph 版本不同**：1.2.16 实际建的是 ``writes``（不是 ``checkpoint_writes``）；
    旧版本才用 ``checkpoint_writes`` / ``checkpoint_blobs``。必须按实际表名清理，否则只删
    ``checkpoints``、旁表无限增长（真实库验证过：检查点 56→3，writes 132 一条没少）。
    """
    for table in ("writes", "checkpoint_writes", "checkpoint_blobs"):  # 框架旁表（含 1.2.16 的 writes）
        if _table_exists(conn, table):  # 表在
            conn.execute(  # 删
                f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_id < ?",  # 条件
                (thread_id, cutoff),  # 参数
            )  # 删除


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:  # 表是否存在
    row = conn.execute(  # 查
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)  # 条件
    ).fetchone()  # 结果
    return row is not None  # 返回
