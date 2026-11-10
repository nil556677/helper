"""画像持久化：user_profile 表读写 + 合并规则（域包内自含 SQL）。

**合并规则**（方案 6.4.4/6.3.4）：
- `user_correction > user_explicit > inferred`（同键来源强覆盖弱）；
- 同来源取新近；inferred 需达 `memory.profile.infer_threshold` 才写，且进 contest 由人工复核；
- 显式 `confirm()` 强制覆盖（version+1 软替代旧版，可回滚）。

只被 `app/agent/memory/manager.py` 或 `app/services/memory.py` 调用（域纪律）。
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳

from app.core.db.connection import get_connection, transaction  # DB
from app.memory.model import (  # 数据模型与常量
    FINGERPRINT_KEY,  # 指纹键（预留）
    INFERRED,  # 推断类型
    ProfileEntry,  # 条目
    default_namespace,  # 默认作用域
)

__all__ = ["FINGERPRINT_KEY", "ProfileStore"]  # 导出


class ProfileStore:  # 画像仓储
    """user_profile 表的 upsert/get_all/confirm/archive（版本化、作用域隔离）。"""

    def __init__(self, namespace: str | None = None) -> None:  # 构造
        self.namespace = namespace if namespace is not None else default_namespace()  # 作用域

    def upsert(  # 写入（经合并规则，不进热路径）
        self,  # 实例
        entry: ProfileEntry,  # 待写条目
        *,  # 关键字
        infer_threshold: float = 0.7,  # 推断阈值（低于不写）
    ) -> bool:  # 是否实际写入
        """默认合并：强来源/新近覆盖旧 active，写 contest_v1；弱来源进 contest 待人工。

        返回 True = 成为新 active；False = 被拒（conf低被阈值拦/冲突进 contest）。
        """
        if entry.prop_kind == INFERRED and entry.confidence < infer_threshold:  # 推断未达阈值
            return False  # 拒绝
        active = self._active_row(entry.prop_key)  # 现活跃
        stamp = time.time()  # 时间戳
        with transaction() as conn:  # 单事务
            if active is None:  # 无活跃 → 直接 active
                self._insert(conn, entry, stamp)  # 写
                return True  # 成为 active
            should_win = self._should_win(entry, active)  # 能否覆盖（同源新近即覆盖）
            if should_win:  # 可覆盖（强来源/新近同源）
                self._supersede(conn, entry.prop_key, active["version"])  # 旧标 superseded
                self._insert(conn, entry, stamp)  # 新 active（继承 namespace）
                return True  # 覆盖成功
            # 不能覆盖 → 冲突：写一条 contested（人为稍弱），保留 active
            contest = ProfileEntry(  # 冲突条目
                prop_key=entry.prop_key,  # 键
                value=entry.value,  # 值
                prop_kind="contested",  # 不用类型常量（冲突记录）
                source=entry.source,  # 来源
                confidence=min(entry.confidence, 0.3),  # 压限
                namespace=self.namespace,  # 作用域
                status="contested",  # 冲突态
            )  # 构造
            self._insert(conn, contest, stamp)  # 写冲突记录
            return False  # 未成 active

    def confirm(self, prop_key: str, value: object) -> None:  # 用户显式更正（最高优先级）
        """强制覆盖为显式值（version+1 软替代旧版；用户显式 > 一切）。"""
        entry = ProfileEntry(  # 构造显式条目
            prop_key=prop_key,  # 键
            value=value,  # 值
            prop_kind="explicit",  # 显式
            source="user_correction",  # 用户更正
            confidence=0.95,  # 高可信
            namespace=self.namespace,  # 作用域
        )  # 结束
        self.upsert(entry, infer_threshold=0.0)  # 显式不再受推断阈值限制

    def get_all(self, *, status: str = "active") -> list[ProfileEntry]:  # 列活跃条目
        """按状态列当前命名空间的画像条目（新→旧）。"""
        rows = get_connection().execute(  # 查
            "SELECT prop_key, prop_kind, value, source, confidence, version, valid_until, status"
            " FROM user_profile WHERE namespace = ? AND status = ? ORDER BY updated_at DESC",
            (self.namespace, status),
        ).fetchall()
        return [  # 转条目
            ProfileEntry(  # 构造
                prop_key=str(r["prop_key"]),  # 键
                value=__import__("json").loads(r["value"]),  # 值
                prop_kind=str(r["prop_kind"]),  # 类型
                source=str(r["source"] or ""),  # 来源
                confidence=float(r["confidence"] or 0),  # 可信度
                version=int(r["version"] or 1),  # 版本
                namespace=self.namespace,  # 作用域
                valid_until=r["valid_until"],  # 有效期
                status=str(r["status"]),  # 状态
            )  # 结束
            for r in rows  # 逐行
        ]  # 返回

    def archive(self, prop_key: str) -> None:  # 归档某键（用户删除）
        """把某键的 active 条目归档（status=archived），不再参与渲染。"""
        with transaction() as conn:  # 单事务
            conn.execute(  # 归档
                "UPDATE user_profile SET status = 'archived', updated_at = ?"
                " WHERE namespace = ? AND prop_key = ? AND status = 'active'",
                (time.time(), self.namespace, prop_key),
            )  # 更新

    def _active_row(self, prop_key: str) -> dict | None:  # 查现活跃
        """命中当前 active（含版本号）。"""
        return get_connection().execute(  # 查
            "SELECT version, source, confidence, updated_at, prop_kind FROM user_profile"
            " WHERE namespace = ? AND prop_key = ? AND status = 'active' ORDER BY version DESC LIMIT 1",
            (self.namespace, prop_key),
        ).fetchone()

    @staticmethod
    def _should_win(entry: ProfileEntry, active: dict) -> bool:  # 合并优先级判定
        """强来源覆盖弱；同来源一律覆盖（新近优先）；否则不覆盖（进冲突）。"""
        strength = {  # 来源强度表
            "user_correction": 3,  # 用户更正最高
            "user_explicit": 2,  # 用户显式次之
            "confirmed": 2,  # 已确认
            "inferred": 1,  # 模型推断最低
        }  # 强度表结束
        new_s = strength.get(entry.source, 1)  # 新来源强度
        old_s = strength.get(str(active["source"] or ""), 1)  # 旧来源强度
        return new_s >= old_s  # 更强或同源（新近）都覆盖

    def _supersede(self, conn, prop_key: str, old_version: int) -> None:  # 旧版标 superseded
        """把指定版本标 superseded（保留可回滚）。"""
        conn.execute(  # 更新
            "UPDATE user_profile SET status = 'superseded', updated_at = ?"
            " WHERE namespace = ? AND prop_key = ? AND version = ?",
            (time.time(), self.namespace, prop_key, int(old_version)),
        )  # 更新

    def _insert(self, conn, entry: ProfileEntry, stamp: float) -> None:  # 插入 active
        """插入新版本（version = 当前最大 +1；主键用 uuid 文本）。"""
        import json  # 序列化
        import uuid  # 主键

        latest = conn.execute(  # 当前最大版本
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM user_profile WHERE namespace = ? AND prop_key = ?",
            (self.namespace, entry.prop_key),
        ).fetchone()
        version = int(latest["v"] or 1)  # 下一版本号
        conn.execute(  # 插入
            "INSERT OR REPLACE INTO user_profile(profile_id, namespace, prop_key, prop_kind, value,"
            " source, confidence, version, valid_until, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                str(uuid.uuid4()),  # 主键
                self.namespace,  # 作用域
                entry.prop_key,  # 键
                entry.prop_kind,  # 类型
                json.dumps(entry.value, ensure_ascii=False, default=str),  # 值
                entry.source,  # 来源
                float(entry.confidence or 0.5),  # 可信度
                version,  # 版本
                entry.valid_until,  # 有效期
                stamp,  # 创建
                stamp,  # 更新
            ),  # 参数
        )  # 写
