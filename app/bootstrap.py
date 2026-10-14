"""启动初始化（幂等）：装钩子 → 目录 → 日志 → 引导配置 → 建表。

**为什么收在一个函数里、顺序还不能改**：这几件事以后还会更多（自检、下载模型…），
必须按固定顺序只做一次；散在入口与各页面里迟早做成两份（§12 记着"两处配置源"的教训）。

顺序的理由（每条都对应一个真实故障）：

1. **先装异常钩子**：后面每一步都可能失败，而没有钩子就没有现场（§11 第 3 条）；
2. **目录**要在日志与数据库之前（两者都落在里面）;
3. **日志要先用默认级别建起来，再读配置改级别**：`ensure_default` / `load` 产生的
   "已从模板生成配置""坏配置已隔离"这类事件发生在 sink 建立之前的话就**只进 stderr 没了**
   （双击启动没有控制台，等于什么都没留下）；
4. **建表 / 迁移**放最后：它失败要能在日志里看到原因。

异常处理：这里**不吞异常**——启动前提不满足时由入口（`app/__main__.py`）转成用户看得见的
提示 + 一份 `logs/startup-error.log`。悄悄降级启动比直接报错更糟。
"""

from __future__ import annotations

from loguru import logger

from app.core import paths
from app.core.config import access
from app.core.db import schema
from app.core.logging import init_logging, install_exception_hooks, is_initialized

_ready = False


def ensure_ready() -> None:
    """保证运行前提（幂等：重复调用几乎零开销）。

    异常：
        OSError: 目录建不出来（安装到只读位置、路径被同名文件占住）。
        sqlite3.Error / RuntimeError: 库损坏、迁移缺脚本、库版本比程序新。
    """
    global _ready
    if _ready:
        return

    # 1. 先装钩子：没有它，后面任何一步失败都只留一行 stderr
    install_exception_hooks()

    # 2. 目录（日志与数据库都落在里面）
    paths.ensure_dirs()

    # 3. 先用默认级别把日志建起来，再按配置调级别——这样"生成配置/隔离坏配置"这类事件留得下来
    if not is_initialized():
        init_logging()
    config_path = access.ensure_default()
    config = access.load()
    init_logging(config.log_level)

    # 4. 建表 / 迁移（幂等）
    version = schema.init_db()

    # 5. 起手把"东西都在哪"一次记全：排查"我的资料去哪了"时，这一行能省一轮来回。
    #    注意 data_dir 可被环境变量/配置改道，而 logs_dir 恒在安装目录——同一份日志里可能混着
    #    多次运行，先看这一行再往下读。
    logger.info("运行路径：{}", "；".join(f"{k}={v}" for k, v in paths.describe().items()))
    logger.info("引导配置：{}（日志级别 {}）", config_path, config.log_level)
    logger.info("数据库：{}（schema v{}）", paths.db_file(), version)

    _ready = True
