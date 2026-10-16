"""界面偏好：窗口几何这类"用户调过一次、下次要一样"的状态。

**为什么放在数据库而不是 config.json**（技术方案 §12）：引导项是"打开数据库之前就必须知道"
的东西（日志级别、数据目录），窗口几何不是——它跟着数据走、跟着数据一起备份才合理。

对外只收发 ``bytes``（Qt 的 ``QByteArray`` 在界面层再转），这样这一层保持 UI-free、可单测。
"""

from __future__ import annotations

import base64

from loguru import logger

from app.core.db import kv

#: settings 表里的键名（一个键一件事，别塞一个复合结构进去）
KEY_WINDOW_GEOMETRY = "window_geometry"


def load_window_geometry() -> bytes | None:
    """取上次保存的窗口几何。

    返回：Qt ``saveGeometry()`` 产出的字节；没存过或存坏了返回 None。
    """
    raw = kv.read_json(KEY_WINDOW_GEOMETRY)
    if not isinstance(raw, dict):
        return None
    encoded = raw.get("geometry")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        logger.warning("窗口几何数据解不出来，按首次运行处理：{}", exc)
        return None


def save_window_geometry(data: bytes) -> None:
    """保存窗口几何（大小 + 位置 + 是否最大化）。

    参数：
        data: Qt ``saveGeometry()`` 的结果。
    异常：
        OSError / sqlite3.Error: 写库失败（调用方决定是否提示；退出路径上一般只记日志）。
    """
    kv.write_json(KEY_WINDOW_GEOMETRY, {"geometry": base64.b64encode(data).decode("ascii")})
