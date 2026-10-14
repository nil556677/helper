"""引导配置的形状（pydantic 校验）。

技术方案 §12 把配置分成**两处**，这里是"引导项"那一处——只有"打开数据库之前就必须知道"的
东西（日志级别、数据目录覆盖）。其余全部（大模型四槽位、api key、检索参数、界面偏好）
都在 SQLite 里，由界面来改。

**为什么引导项也要校验**：``config.json`` 是用户能手改的文件。坏值必须在**加载期**被拒并隔离，
而不是等到某一步才炸——那时往往已经在界面渲染路径上，表现就是"窗口打不开"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: 允许的日志级别（与 core/logging 的用法一致）。
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class BootstrapConfig(BaseModel):
    """``config.json`` 的全部内容——就这两项。

    ``extra="forbid"``：键名拼错要立刻报错。"用户以为改了、其实没生效"是最难查的一类问题。
    ````_`` 开头的键由加载器当注释剥掉（见 access._strip_notes），不参与校验。
    """

    model_config = ConfigDict(extra="forbid")

    log_level: LogLevel = "INFO"
    #: 数据目录覆盖；None = 用安装目录下的 ``data/``。
    #: 注意优先级：环境变量 ``HELPER_DATA_DIR`` > 这一项 > 默认值。
    data_dir: str | None = None
