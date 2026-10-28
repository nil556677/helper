"""子进程守护（P7）：Qdrant sidecar 与独立 worker 共用。

入口统一走 ``app.core.proc.services``（``start/stop/status/restart``）；本包只导出
Supervisor / ChildSpec，以及崩溃后清理残留子进程的 ``cleanup_stale_children``。
"""

from app.core.proc.supervisor import ChildSpec, Supervisor, cleanup_stale_children

__all__ = [
    "ChildSpec",
    "Supervisor",
    "cleanup_stale_children",
]
