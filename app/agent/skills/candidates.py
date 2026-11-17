"""Skill 沉淀 · 候选区：skill_candidates（自动提炼但未评审的草案，不直接进 skills/）。

流水线：condenser 产草案 → `record_candidate` 落候选（pending）→ `evaluate` 校验 Gate →
`decide` 通过后再 `materialize` 写成 skill.yaml。默认**人工/评测确认后才入库**，防过拟合单次运行。
"""

from __future__ import annotations  # 延迟值注解

import json  # 序列化
import time  # 时间戳
from typing import Any  # 任意值

from app.core.db.connection import get_connection, transaction  # DB

__all__ = ["decide", "evaluate", "get", "list_candidates", "materialize", "record_candidate"]  # 导出

#: 候选表 DDL（只追加草案，状态更新）。
_DDL = (  # DDL
    "CREATE TABLE IF NOT EXISTS skill_candidates ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"  # id
    "  skill_id TEXT NOT NULL,"  # 草案 skill id
    "  namespace TEXT DEFAULT 'auto',"  # 命名空间
    "  version INTEGER DEFAULT 1,"  # 版本
    "  status TEXT NOT NULL DEFAULT 'pending',"  # pending/approved/rejected
    "  source TEXT NOT NULL DEFAULT '',"  # run/trace/correct
    "  run_ids TEXT DEFAULT '[]',"  # 来源 run
    "  draft TEXT NOT NULL DEFAULT '{}',"  # SkillManifest 草案 JSON
    "  score REAL DEFAULT 0,"  # 评测分（可选）
    "  created_at REAL NOT NULL,"  # 创建
    "  decided_at REAL,"  # 决策时间
    "  decision_note TEXT DEFAULT ''"  # 决策备注
    ")",
    "CREATE INDEX IF NOT EXISTS idx_skill_candidates_status ON skill_candidates(status, created_at)",  # 按状态
)  # 结束


def _ensure() -> None:  # 幂等建表
    with transaction() as conn:  # 事务
        for stmt in _DDL:  # 逐条
            conn.execute(stmt)  # 执行


def record_candidate(  # 记一条候选择案（pending）
    draft: dict[str, Any],  # SkillManifest 草案
    *,
    source: str = "run",  # 来源
    run_ids: list[str] | None = None,  # 来源 run
    score: float = 0.0,  # 评测分
) -> int:  # 候选 id
    _ensure()  # 建表
    skill_id = str(draft.get("id") or "auto_skill")  # id
    with transaction() as conn:  # 事务
        cur = conn.execute(  # 插入
            "INSERT INTO skill_candidates(skill_id, namespace, version, status, source, run_ids, draft, score, created_at)"
            " VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (skill_id, str(draft.get("namespace") or "auto"), int(draft.get("version") or 1),  # 基础
             str(source), json.dumps(run_ids or [], ensure_ascii=False),  # 来源
             json.dumps(draft, ensure_ascii=False), float(score), time.time()),  # 草案
        )  # 插入
        return int(cur.lastrowid)  # 返回 id


def get(candidate_id: int) -> dict[str, Any] | None:  # 取候选
    _ensure()  # 建表
    row = get_connection().execute("SELECT * FROM skill_candidates WHERE id=?", (int(candidate_id),)).fetchone()  # 查
    return _row_to_dict(row) if row is not None else None  # 返回


def list_candidates(status: str = "", *, limit: int = 50) -> list[dict[str, Any]]:  # 列候选
    _ensure()  # 建表
    if status:  # 过滤状态
        rows = get_connection().execute(
            "SELECT * FROM skill_candidates WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, int(limit))
        ).fetchall()  # 查
    else:  # 全部
        rows = get_connection().execute(
            "SELECT * FROM skill_candidates ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()  # 查
    return [_row_to_dict(r) for r in rows]  # 返回


def decide(candidate_id: int, approved: bool, *, note: str = "") -> None:  # 决策（乐观：只 pending）
    _ensure()  # 建表
    status = "approved" if approved else "rejected"  # 目标状态
    with transaction() as conn:  # 事务
        conn.execute(  # 条件更新
            "UPDATE skill_candidates SET status=?, decided_at=?, decision_note=? WHERE id=? AND status='pending'",
            (status, time.time(), str(note), int(candidate_id)),
        )  # 更新


def evaluate(draft: dict[str, Any]) -> dict[str, Any]:  # 校验 Gate（与 tool_catalog 交叉校验）
    """草案必须通过 SkillManifest 校验；不过则不能入库。返回 {ok, errors}。"""
    from app.agent.skills.models import SkillManifest  # 契约
    from app.agent.skills.registry import validate  # 校验

    try:  # 建模 + 校验
        manifest = SkillManifest(**draft)  # 解析
        errors = validate(manifest)  # 校验
    except Exception as exc:  # 解析失败
        return {"ok": False, "errors": [str(exc)]}  # 不过
    return {"ok": not errors, "errors": errors}  # 返回


def materialize(draft: dict[str, Any], base_dir: str) -> str:  # 通过后写成 skill.yaml（入库）
    """把草案落成 `<base_dir>/<namespace>/<id>/skill.yaml`；返回文件路径。不做额外校验（调用前应先 evaluate）。"""
    from pathlib import Path  # 路径

    import yaml  # YAML

    target = Path(base_dir) / str(draft.get("namespace") or "auto") / str(draft.get("id") or "auto_skill")  # 目标目录
    target.mkdir(parents=True, exist_ok=True)  # 建目录
    path = target / "skill.yaml"  # 文件
    path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")  # 写
    return str(path)  # 返回路径


def _row_to_dict(row: Any) -> dict[str, Any]:  # 行 → dict（draft/run_ids 解析）
    out = dict(row)  # 转 dict
    for key in ("draft", "run_ids"):  # JSON 列
        try:  # 解析
            out[key] = json.loads(out.get(key) or ("{}" if key == "draft" else "[]"))  # 解析
        except Exception:  # 坏数据
            out[key] = {} if key == "draft" else []  # 兜底
    return out  # 返回
