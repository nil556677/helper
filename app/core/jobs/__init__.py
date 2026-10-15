"""任务系统（**项目级基础设施**，技术方案 §13）。

它不是某个功能的附属：资料导入、向量化、OCR、索引重建、批量导出、定期清理都用它。
任何功能只做一件事——**注册一个 kind**（`registry.JobKind`），引擎一行不改。

模块划分：

| 模块 | 放什么 |
| --- | --- |
| `model.py` | 数据形状与契约：`Job` / `JobError` / `JobCancelled` / `JobInterrupted` / `JobContext` |
| `registry.py` | kind 注册表（引擎不认识任何具体功能，功能反过来注册进来） |
| `store.py` | **所有** SQL（别处不许直接写 `jobs` 表） |
| `api.py` | 对外接口：界面与功能只调这里 |
| `runner.py` | 调度线程 + 泳道执行器 + 优雅退出 + 延迟看门狗（M2） |

**三条不变量**（§13.6，会写成测试）：界面回调只做三件事；工作线程不碰界面对象；
事务不跨模型调用。
"""

from app.core.jobs.api import (
    cancel,
    enqueue,
    get,
    is_paused,
    kinds,
    list_jobs,
    pause,
    register,
    resume,
    retry,
    stats,
)
from app.core.jobs.model import (
    PRIORITY_DEFAULT,
    PRIORITY_MAINTENANCE,
    PRIORITY_WAITING,
    Job,
    JobCancelled,
    JobContext,
    JobError,
    JobInterrupted,
)
from app.core.jobs.registry import JobKind

__all__ = [
    "PRIORITY_DEFAULT",
    "PRIORITY_MAINTENANCE",
    "PRIORITY_WAITING",
    "Job",
    "JobCancelled",
    "JobContext",
    "JobError",
    "JobInterrupted",
    "JobKind",
    "cancel",
    "enqueue",
    "get",
    "is_paused",
    "kinds",
    "list_jobs",
    "pause",
    "register",
    "resume",
    "retry",
    "stats",
]
