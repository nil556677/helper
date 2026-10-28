"""SQLite 初始化入口（净室 v1）。

旧 v20 迁移链已删除；本模块只转发 :mod:`app.core.db.schema_v1`。
旧库会由 ``schema_v1.initialize`` 拒绝，提示运行 ``scripts/reset_workspace.py``。
"""

from __future__ import annotations

from app.core.db.connection import get_connection
from app.core.db.schema_v1 import DDL_SQL, SCHEMA_VERSION, initialize, read_version

__all__ = ["DDL_SQL", "SCHEMA_VERSION", "init_db", "initialize", "read_version"]


def init_db() -> int:
    """建/校验 v1 schema；返回版本号。旧版本库抛 ValueError。"""
    conn = get_connection()
    return initialize(conn)


def init_schema(conn=None) -> int:
    """兼容入口：对给定连接（默认线程连接）建 v1 schema。"""
    return initialize(conn if conn is not None else get_connection())


def schema_version(conn=None) -> int:
    """读当前 schema 版本（默认线程连接）。"""
    return read_version(conn if conn is not None else get_connection())
