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

另外两件"顺手做完"的事：**过期日志清理**（只留最近 7 天）与**受限环境嗅探**（从原生输出里认出
"上次窗口没渲染出来"，见 §10.3）——它们都只读日志目录，失败不该挡住启动，所以放在建表之前。

**这里不做两件事**（它们是进程级副作用，只归真正的入口管，见 ``app/__main__.py``）：
fd 1/2 重定向（会把测试进程的输出也劫走）与 stdlib ``logging`` 劫持（会动 pytest 的日志捕获）。

异常处理：这里**不吞异常**——启动前提不满足时由入口（`app/__main__.py`）转成用户看得见的
提示 + 一份 `logs/startup-error.log`。悄悄降级启动比直接报错更糟。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.config import access
from app.core.db import schema
from app.core.logging import (
    init_logging,
    install_exception_hooks,
    is_initialized,
    log_environment_snapshot,
    purge_old_logs,
    sniff_restricted_hint,
)

_ready = False


def pin_temp_dir() -> Path:
    """把**整个进程**的临时目录钉进 `data/tmp/`，返回该目录。

    为什么必须做（本机实测）：`tempfile.gettempdir()` 在 TEMP/TMPDIR 都不可用时**会退回"当前工作目录"**
    —— 本机就是仓库根。后果是"任何用 tempfile 的库都会往仓库根里丢东西"：实测出现过
    ① NiceGUI 测试装置的 `nicegui-test-storage-*` 空目录（9 个，还因为工作区根上的删除保护事后删不掉）；
    ② jieba 的前缀词典缓存 `jieba.cache`（8.8 MB）。
    钉住之后这些产物统一落在 `data/tmp/`（跟着数据目录走，测试隔离也顺带生效）。
    """
    target = Path(paths.data_dir()) / "tmp"
    target.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(target)
    return target


def ensure_ready() -> None:
    """保证运行前提（幂等：重复调用几乎零开销）。

    异常：
        OSError: 目录建不出来（安装到只读位置、路径被同名文件占住）。
        sqlite3.Error / RuntimeError: 库损坏、迁移缺脚本、库版本比程序新。
    """
    global _ready

    # 0. 临时目录**每次调用都要钉**（不能放进下面的幂等区间）：它跟着 `data_dir()` 走，
    #    而测试里每个用例的数据目录都不一样——只钉一次的话，后面用到的就是上一个用例
    #    已经被删掉的路径（实测：`tempfile.mkdtemp` 直接 FileNotFoundError，还会连累别的用例）。
    #    这件事只花一次 mkdir + 一次赋值，不用省。
    pin_temp_dir()

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

    # 3b. 过期日志清理（只留最近 7 天）与"上次是不是被沙箱挡了"的嗅探。
    #     清理放在建表之前：它只动 logs/，失败也不该挡住建库。
    purge_old_logs()
    hint = sniff_restricted_hint()
    if hint:
        logger.warning(hint)

    # 4. 建表 / 迁移（幂等）
    version = schema.init_db()

    # 5. 起手把"东西都在哪"一次记全：排查"我的资料去哪了"时，这一行能省一轮来回。
    #    注意 data_dir 可被环境变量/配置改道，而 logs_dir 恒在安装目录——同一份日志里可能混着
    #    多次运行，先看这一行再往下读。
    logger.info("运行路径：{}", "；".join(f"{k}={v}" for k, v in paths.describe().items()))
    # 环境快照：版本 / flags / TEMP / 目录，出问题时先看这一段（技术方案 §10.3）
    log_environment_snapshot()
    logger.info("引导配置：{}（日志级别 {}）", config_path, config.log_level)
    logger.info("数据库：{}（schema v{}）", paths.db_file(), version)

    _ready = True
