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

import contextlib
import tempfile
import threading
from pathlib import Path

from loguru import logger

from app.core import paths
from app.core.config import access
from app.core.db import schema
from app.core.logging import (
    check_disk_space,
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


def start_model_warmup() -> None:  # 后台预热
    """启动后异步加载嵌入/精排，避免第一问等 3–5 秒。"""
    def _warm() -> None:  # 后台线程
        """失败只告警，不阻断启动。"""
        with contextlib.suppress(Exception):  # 模型缺失也不崩
            from app.core.embed import embed_texts  # 延迟导入

            embed_texts(["预热"])  # 触发加载
            logger.info("嵌入模型预热完成")  # 日志
        with contextlib.suppress(Exception):  # 精排缺失也不崩
            from app.core.rerank import score_pairs  # 延迟导入

            score_pairs("预热", ["预热"])  # 触发加载
            logger.info("精排模型预热完成")  # 日志

    threading.Thread(target=_warm, name="model-warmup", daemon=True).start()  # 后台跑


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

    # 3. 先用默认级别把日志建起来（后面建库/迁移的事件要留得下来）
    if not is_initialized():
        init_logging()

    # 3a. **先建库**：引导配置现在存在 settings 表里（config.json 只做一次性迁移）。
    version = schema.init_db()
    access.ensure_default()
    # v1：**只用 Qdrant 服务端模式**。没配 ``vector.url`` 时默认 127.0.0.1:6333，
    # 由 helper 自己拉起 ``bin/qdrant/qdrant.exe``（存储 data/qdrant-server）。
    try:
        from app.core.config import store as params_store

        if not str(params_store.load()["vector.url"] or "").strip():
            params_store.save_values({"vector.url": "http://127.0.0.1:6333"})
            logger.info("默认 Qdrant 服务端模式（127.0.0.1:6333）")
    except Exception as exc:
        logger.warning("初始化 Qdrant 服务端默认值失败：{}", exc)
    config = access.load()
    init_logging(config.log_level, debug=config.log_debug)

    # 3b. 过期日志清理（只留最近 7 天）与"上次是不是被沙箱挡了"的嗅探。
    #     清理只动 logs/，失败也不该挡住建库。
    purge_old_logs()
    hint = sniff_restricted_hint()
    if hint:
        logger.warning(hint)

    # 4a. v1 向量集合：单集合 kb_chunks。
    #     服务端模式要等 Qdrant sidecar 起来（入口 services.start() 之后）再建，这里跳过。
    try:
        from app.core.config import store as params_store

        server_mode = bool(str(params_store.load()["vector.url"] or "").strip())
    except Exception:
        server_mode = False
    if server_mode:
        logger.info("服务端模式：向量集合自检延后到 Qdrant 启动后")
    else:
        try:
            from app.integrations import qdrant as qi

            qi.ensure_collection(qi.qdrant_client())
        except Exception as exc:
            logger.warning("向量集合自检失败（不影响启动，检索/导入会另行报错）：{}", exc)

    # 4b. T3.2 数据库体检：只在"上次没干净退出"时做（干净退出会写标记，启动读一次就清掉）。
    #     发现问题不在这里处理（只读；由调用方决定从备份恢复 / 重命名坏库）。
    from app.core.db import health

    health_problem = health.verify_unclean_exit()
    if health_problem is not None:
        raise RuntimeError(
            f"数据库体检未通过（quick_check 返回 {health_problem}）：库可能损坏。"
            "可以从备份恢复 data/，或重命名 data/helper.db 让其重建（会丢未备份的资料）。"
        )

    # 5. 起手把"东西都在哪"一次记全：排查"我的资料去哪了"时，这一行能省一轮来回。
    #    所有路径都写死在安装目录旁（见 paths）——data/ 与 logs/ 都是；同一份日志里可能混着
    #    多次运行，先看这一行再往下读。
    logger.info("运行路径：{}", "；".join(f"{k}={v}" for k, v in paths.describe().items()))
    # 环境快照：版本 / flags / TEMP / 目录，出问题时先看这一段（技术方案 §10.3）
    log_environment_snapshot()
    space_warning = check_disk_space(paths.data_dir())
    if space_warning:
        logger.warning(space_warning)
    logger.info("引导配置：数据库 settings 表（日志级别 {}；config.json 只做一次性迁移）", config.log_level)
    logger.info("数据库：{}（schema v{}）", paths.db_file(), version)

    _ready = True
