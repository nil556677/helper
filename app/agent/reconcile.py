"""E5 恢复与对账：崩溃扫描 + interrupted 标记 + 恢复决策 + 幂等键。

- ``stale_run_ids``：找出 ``trace_runs`` 里 status=running 且超过阈值的残留 run；
- ``mark_interrupted``：把它们标成 interrupted（不自动续；只让状态可解释）；
- ``resume_decision``：恢复矩阵（doc §8.1）——只有 interrupt 可 resume，其余不自动续；
- ``side_effect_key``：副作用幂等键（turn_id, step）。
"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 时间戳

from app.core.db.connection import get_connection  # 只读/写连接

#: running 超过此秒数视作崩溃残留（本地 run 很短）。
STALE_S = 60


def stale_run_ids(*, stale_s: float = STALE_S, now: float | None = None) -> list[str]:  # 崩溃残留
    """返回停留在 running 且超时的 run id（上次进程崩溃没来得及收尾的）。"""
    cutoff = (now if now is not None else time.time()) - float(stale_s)  # 阈值时刻
    rows = get_connection().execute(  # 查
        "SELECT id FROM trace_runs WHERE status = 'running' AND started_at < ?",  # 条件
        (cutoff,),  # 参数
    ).fetchall()  # 行
    return [str(row["id"]) for row in rows]  # id 列表


def mark_interrupted(run_ids: list[str]) -> int:  # 标 interrupted
    """把崩溃残留标成 interrupted（不自动续）。返回改动行数。"""
    if not run_ids:  # 空
        return 0  # 无操作
    marks = ",".join("?" * len(run_ids))  # 占位
    cur = get_connection().execute(  # 更新
        f"UPDATE trace_runs SET status = 'interrupted', error_kind = 'interrupted'"  # 赋值
        f" WHERE id IN ({marks}) AND status = 'running'",  # 只改仍 running 的
        run_ids,  # 参数
    )  # 更新
    return int(cur.rowcount)  # 返回


def resume_decision(  # 恢复决策（doc §8.1）
    status: str | None,  # 上轮终态（None/空 = 崩溃无终态）
    *,  # 仅关键字
    has_checkpoint: bool,  # 是否有可恢复 checkpoint
    waiting_interrupt: bool = False,  # 是否停在补参/审批的 interrupt
) -> str:  # resume/retry/new/interrupted
    """只信任 checkpoint 上的 interrupt；cancel/error/崩溃一律不自动续。"""
    if waiting_interrupt and has_checkpoint:  # 真 interrupt
        return "resume"  # 恢复
    if status == "done":  # 正常完成
        return "new"  # 新轮
    if status == "cancelled":  # 用户取消
        return "new"  # 新轮（不续）
    if status == "failed":  # 失败
        return "retry"  # 有界重试后转新轮（不静默续）
    return "interrupted"  # 崩溃/未知：标 interrupted，人工介入


def reconcile_once(*, stale_s: float = STALE_S) -> int:  # 一次对账（运行时 E5）
    """启动/周期调用：把崩溃残留标成 interrupted。返回改动行数。"""
    stale = stale_run_ids(stale_s=stale_s)  # 扫残留
    return mark_interrupted(stale)  # 标记


def side_effect_key(turn_id: str, step: str | int) -> str:  # 幂等键
    """副作用去重键（DB 唯一约束用它）。"""
    return f"{turn_id}@{step}"  # 拼接
