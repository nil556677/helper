"""项目路径的唯一来源（"东西放哪"只在这里回答）。

为什么必须有这个模块：开发期"安装目录"是仓库根，以后打包后是 exe 所在目录。
数据、模型、日志、配置全挂在它下面，而散落各处的 ``Path(__file__).parent`` 在打包后会指到
临时解包目录（``_MEIPASS``），数据会写丢或写进只读位置。

本模块属于 app/core，**不 import 任何界面库**。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: 数据目录名。**写死在安装目录旁**：配置里不再有 data_dir，也没有环境变量能改道
#: （改动记录见技术方案 §12；2026-10 用户拍板「几个文件地址直接写死」）。
DATA_DIR_NAME = "data"


def file_size(path: Path) -> int:
    """文件大小（字节）；读不到算 0（用于统计与日志，失败不该打断流程）。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0

#: SQLite 主库文件名。
DB_FILENAME = "helper.db"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里。

    返回：打包后为 True。判据用 ``sys.frozen``（PyInstaller 官方约定），
    不用 ``sys._MEIPASS`` 是否存在——那个属性只在部分模式下出现。
    """
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """安装目录：开发期是仓库根，打包后是 exe 所在目录。

    返回：绝对路径。这是 config.json、data/、models/、logs/ 的共同父目录，
    也是"程序与数据放在一起"这条约定的落点。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # core/paths.py -> core/ -> 仓库根
    # 本文件在 app/core/paths.py：往上三层才是仓库根（app/core → app → 仓库根）
    return Path(__file__).resolve().parents[2]


def config_file() -> Path:
    """老 ``config.json`` 的位置（**只用于一次性迁移/隔离**，运行时配置在数据库）。"""
    return project_root() / "config.json"


def data_dir() -> Path:
    """数据目录：**恒为** ``{安装目录}/data``。

    为什么写死：路径只有一个答案，排查时不必先问"你把它挪哪去了"。
    以前支持 config.json 的 ``data_dir`` 与环境变量 ``HELPER_DATA_DIR`` 改道，两个旋钮都已去掉；
    测试要隔离就走 monkeypatch（把 :func:`project_root` 或本函数换掉），不再依赖环境变量。
    """
    return project_root() / DATA_DIR_NAME


def db_file() -> Path:
    """SQLite 主库文件。"""
    return data_dir() / DB_FILENAME


def files_dir() -> Path:
    """导入资料的副本目录。

    为什么留副本：原件可能被用户删掉、改名或移走，而"入库失败可从副本续跑"这条路径
    依赖副本一直存在。
    """
    return data_dir() / "files"


def inbox_dir() -> Path:
    """上传暂存目录（**任务系统落地后新增**）。

    为什么要它：上传进来的字节不能塞进任务表（表里只放引用），也不能只留在内存里——
    否则"导到一半关窗口"就白导了。所以流程是：上传 → 落 inbox → 排一条导入任务 →
    任务读 inbox → 副本进 ``files/`` → 删 inbox 文件。
    """
    return data_dir() / "inbox"


#: 文件名里不能出现的字符（Windows 保留字符 + 路径分隔符 + 控制字符）
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows 设备保留名（这些名字做文件名会打开设备而不是文件）
_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, *, max_len: int = 120) -> str:
    """把用户给的文件名净化成**可落盘**的形状（保留中文，去掉路径与非法字符）。

    三条都踩过：
    - 路径分隔符 / ``..``：``Path(name).name`` 只取最后一段，杜绝目录逃逸；
    - **Windows 保留名**（``CON`` / ``NUL`` / ``COM1``…）：直接落盘会写进设备；
    - **超长名**：NTFS 单段上限 255 字符，inbox 前缀还要占 13 个——超了就永远导不进去。
      截断时**保后缀**（扩展名决定用哪个解析器）。

    参数：
        name: 原始文件名。
        max_len: 净化后主名的最大长度（不含后缀的余量在内）。
    """
    base = Path(str(name)).name
    base = _UNSAFE_FILENAME.sub("_", base).strip(" .")
    if not base:
        return "unnamed"
    stem, dot, suffix = base.rpartition(".")
    keep_suffix = f".{suffix}"[:16] if dot and suffix else ""
    stem = (stem or "unnamed")[: max(1, max_len - len(keep_suffix))]
    base = f"{stem}{keep_suffix}"
    if base.split(".", 1)[0].upper() in _RESERVED_FILENAMES:
        base = f"_{base}"
    return base or "unnamed"


def resolve_stored_path(stored: str | None) -> Path | None:
    """把库里的 ``stored_path`` 解析成 ``files/`` 下的绝对路径；越界返回 None。

    **纵深防御**：入库时已经 sanitize 过（``<file_hash12>/<safe_name>``），但库可能被外部
    工具改过、或带着老数据。这里统一校验"解析后必须仍在 ``files/`` 内"——
    绝对路径、``..``、指向外部的符号链接都会被拒绝。读文件的地方都该走它。
    """
    text = str(stored or "").strip()
    if not text:
        return None
    base = files_dir().resolve()
    try:
        candidate = (base / text).resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        from loguru import logger

        logger.warning("拒绝越界的资料副本路径（不在 files/ 内）：{}", text)
        return None
    return candidate


def logs_dir() -> Path:
    """日志目录。"""
    return project_root() / "logs"


def models_dir() -> Path:
    """本地模型目录（以后的 ONNX 向量 / 精排模型）。"""
    return project_root() / "models"


def tmp_dir() -> Path:
    """仓库内临时目录。

    为什么不用系统 TEMP：受限令牌下列不出也写不进 ``tempfile.mkdtemp`` 建的 0o700 目录，
    测试与中间产物统一放这里就不受该限制影响。
    """
    return project_root() / ".tmp"


def ensure_dirs() -> None:
    """建立运行所需目录（幂等）。

    为什么每次都 stat 一遍（而不是"建过就跳过"）：缓存只能证明"曾经建过"，运行期用户
    可能把 ``data`` 删掉；``is_dir()`` 比 ``mkdir`` 便宜得多，缺哪个补哪个。

    异常：
        OSError: 目录不可创建（如装到了只读位置）——由调用方转成界面提示。
    """
    # 注意**不建 `files/` 与 `models/`**：现在还没有资料库（做得不满意、已撤掉等重新设计）
    # 也还没有本地模型（P2 才落地）。主动建空目录会让人以为"这个能力已经有了"；
    # 对应功能落地时自己创建（方案 §6.2：不预建空目录）。
    for path in (data_dir(), logs_dir()):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)


def describe() -> dict[str, str]:
    """把当前路径事实汇总成一份可打印的字典。

    用途：**启动时打进日志**（排查"我的资料去哪了"时，一眼能看出这次用的是哪个库），
    以及启动失败时写进错误记录。**所有路径都是写死的**（没有改道旋钮），
    而 ``logs_dir`` 恒在安装目录——所以同一份日志里可能混着多次运行，先看这一行再往下读。
    """
    return {
        "安装目录": str(project_root()),
        "旧配置文件": str(config_file()),
        "数据目录": str(data_dir()),
        "数据库": str(db_file()),
        "资料副本": str(files_dir()),
        "日志目录": str(logs_dir()),
        "模型目录": str(models_dir()),
        "打包运行": "是" if is_frozen() else "否",
    }
