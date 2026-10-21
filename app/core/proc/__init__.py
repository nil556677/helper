"""子进程守护（P7）：Qdrant sidecar 与独立 worker 共用。"""

from app.core.proc.lifecycle import build_specs, start_sidecars, stop_sidecars
from app.core.proc.supervisor import ChildSpec, Supervisor

__all__ = [
    "ChildSpec",
    "Supervisor",
    "build_specs",
    "start_sidecars",
    "stop_sidecars",
]
