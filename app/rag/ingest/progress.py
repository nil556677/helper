"""进度 / 回调相关的私有件（导入进度、取消、退出检查）。"""

from __future__ import annotations

from collections.abc import Callable

#: 进度回调：``(已处理的单元数, 总单元数——不知道就是 0, 一句人话)``。
#: 导入任务把它接到 ``JobContext.report`` 上：进度、取消、退出检查都靠它。
ProgressFn = Callable[[int, int, str], None]
