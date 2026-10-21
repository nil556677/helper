"""「向量」设置分区（P1.12）背后的服务：集合与占用。

**为什么单独一个服务**：这些动作要么危险（清空集合），要么耗时（对账），
不能让界面直接调底层 `api`——否则"确认/排队/失败提示"三件事会散落在各个按钮回调里。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.vector import api


def overview() -> dict[str, Any]:
    """「向量」页要的数据：各集合的条数与维度。

    返回：
        ``{"collections", "error"?}``。
    ``error`` 非空表示向量后端暂时不可用（界面降级显示，不抛 500）。
    """
    error = ""
    try:
        collections: list[dict[str, Any]] = [
            {
                "name": str(item.get("collection") or ""),
                "vectors": int(api.count(str(item.get("collection") or ""))),
                "dims": int(item.get("dims") or api.dims()),
            }
            for item in api.stats()
        ]
    except Exception as exc:  # Qdrant 锁被占/连不上：页面降级，别让整页 500
        collections = []
        error = str(exc) or type(exc).__name__
        logger.warning("向量页数据暂不可用：{}", error)
    return {
        "collections": collections,
        "error": error,
    }


def dashboard_url() -> str:
    """Qdrant 自带控制台地址；本地嵌入式返回空串（没有 HTTP dashboard）。"""
    from app.core.config import store as params_store

    try:
        base = str(params_store.load()["vector.url"] or "").strip().rstrip("/")
    except Exception:
        return ""
    if not base:
        return ""
    return f"{base}/dashboard"


def dashboard_key() -> str:
    """控制台要填的 API key（**只在用户点"打开控制台"时经剪贴板交给用户**，不写日志）。"""
    from app.core.config import store as params_store

    try:
        return str(params_store.load()["vector.api_key"] or "").strip()
    except Exception:
        return ""


def dashboard_web_ui_ready() -> bool:
    """本机 Qdrant 是否带 Web UI 静态文件（官方 Windows zip 默认不带）。"""
    from urllib.parse import urlsplit

    from app.core import paths
    from app.core.config import store as params_store

    try:
        base = str(params_store.load()["vector.url"] or "")
        host = (urlsplit(base).hostname or "").lower()
    except Exception:
        return False
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return True  # 远程服务端由对方提供 Web UI，本地无法判断
    return (paths.project_root() / "bin" / "qdrant" / "static" / "index.html").is_file()
