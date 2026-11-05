"""语义路由：启动期预计算 + 独立存储（- 每次用直接查询）。

把 `routes/*.yaml` 的描述向量在启动时算好并落 `data/route_embeddings.sqlite`；
运行时只 embed query 一次 + 内存余弦。描述或模型版本变化 → 按 text_hash 增量重建。
"""

from __future__ import annotations  # 延迟求值注解

import hashlib  # 文本哈希（失效判定）
import json  # 向量序列化
import sqlite3  # 独立存储
import time  # 时间戳
from dataclasses import dataclass  # 索引
from pathlib import Path  # 路径
from typing import Callable, Sequence  # 回调

from app.agent.contracts.routing import RouteSpec  # 固定路径

#: 向量口径版本（模型 + 维度）；变化则全部重建。
ROUTE_EMBED_VERSION = "bge-m3:768-32"  # 版本

#: 默认存储路径（运行时生成，gitignore）。
DEFAULT_STORE = Path("data/route_embeddings.sqlite")  # 路径

EmbedFn = Callable[[str], list[float]]  # 文本 → 向量


def text_hash(text: str) -> str:  # 内容哈希（改描述即失效）
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]  # 短哈希


def source_text(spec: RouteSpec) -> str:  # 描述 + 示例拼成 embedding 源文本
    parts = [spec.description, *spec.examples]  # 主文本 + 示例
    return "\n".join(part for part in parts if part).strip()  # 去空


def _normalize(vector: Sequence[float]) -> tuple[float, ...]:  # L2 归一化
    """bge-m3 已是单位向量；这里再归一次，保证任何 embed 实现都能算余弦。"""
    norm = (sum(float(x) * float(x) for x in vector) ** 0.5) or 1.0  # 模长
    return tuple(float(x) / norm for x in vector)  # 归一


@dataclass(frozen=True)
class RouteIndex:  # 内存路由向量矩阵
    routes: tuple[str, ...]  # 路径名（顺序）
    vectors: tuple[tuple[float, ...], ...]  # 对应向量（已归一）

    def scores(self, query: Sequence[float]) -> list[tuple[str, float]]:  # 余弦排序
        """返回 [(route, score)] 按分数降序；query 可未归一（内部归一次）。"""
        q_len = (sum(x * x for x in query) ** 0.5) or 1.0  # query 模长
        out: list[tuple[str, float]] = []  # 结果
        for route, vec in zip(self.routes, self.vectors):  # 逐条
            dot = sum(a * b for a, b in zip(query, vec))  # 点积
            out.append((route, float(dot / q_len)))  # 归一化余弦
        out.sort(key=lambda item: item[1], reverse=True)  # 降序
        return out  # 返回


def _open(path: Path) -> sqlite3.Connection:  # 开库并建表
    path.parent.mkdir(parents=True, exist_ok=True)  # 确保目录
    conn = sqlite3.connect(str(path))  # 连接
    conn.execute(  # 建表
        "CREATE TABLE IF NOT EXISTS route_vectors("  # 表
        " route_id TEXT, text_hash TEXT, model_version TEXT,"  # 键
        " dim INT, vector TEXT, created_at REAL,"  # 数据
        " PRIMARY KEY(route_id, model_version))"  # 主键
    )
    return conn  # 返回


def load_or_build(  # 加载或（按需）重建索引
    routes: Sequence[RouteSpec],  # 固定路径
    embed: EmbedFn,  # 文本 → 向量
    *,
    store_path: Path = DEFAULT_STORE,  # 存储
    model_version: str = ROUTE_EMBED_VERSION,  # 口径版本
) -> RouteIndex:  # 索引
    """缺向量才 embed（增量）；文本/版本变了重算该项；删除已不存在的行。"""
    conn = _open(store_path)  # 开库
    names: list[str] = []  # 路径名
    vectors: list[tuple[float, ...]] = []  # 向量
    seen: set[str] = set()  # 本轮的 route_id
    for spec in routes:  # 逐条路径
        seen.add(spec.route_id)  # 记录
        hashed = text_hash(source_text(spec))  # 内容哈希
        row = conn.execute(  # 查旧向量
            "SELECT vector FROM route_vectors "  # 查询
            "WHERE route_id=? AND model_version=? AND text_hash=? LIMIT 1",  # 条件
            (spec.route_id, model_version, hashed),  # 参数
        ).fetchone()  # 取
        if row is not None:  # 命中缓存
            vector = _normalize(json.loads(row[0]))  # 还原并归一
        else:  # 缺或变了 → 重新 embed
            vector = _normalize(embed(source_text(spec)))  # 计算并归一
            conn.execute(  # upsert
                "INSERT OR REPLACE INTO route_vectors"  # 写
                "(route_id,text_hash,model_version,dim,vector,created_at) VALUES(?,?,?,?,?,?)",  # 插入
                (spec.route_id, hashed, model_version, len(vector), json.dumps(vector), time.time()),  # 值
            )
        names.append(spec.route_id)  # 名字
        vectors.append(vector)  # 向量
    conn.execute(  # 清理过期行（已不在 routes 里）
        "DELETE FROM route_vectors WHERE model_version=? AND route_id NOT IN (%s)"  # 删除
        % ",".join("?" * len(seen)),  # 占位
        [model_version, *sorted(seen)],  # 参数
    )
    conn.commit()  # 提交
    conn.close()  # 关闭
    return RouteIndex(tuple(names), tuple(vectors))  # 返回索引


_def_index: RouteIndex | None = None  # 进程级惰性单例
_def_embed: EmbedFn | None = None  # 进程级 embed 单例


def default_embed() -> EmbedFn:  # 懒加载本地 bge-m3
    """首次调用才构造 ONNX 嵌入器（避免 import 时就加载模型）。"""
    global _def_embed  # 改模块级
    if _def_embed is None:  # 未建
        from app.integrations.embeddings import OnnxBgeM3Embeddings  # 本地 embed

        _def_embed = OnnxBgeM3Embeddings().embed_query  # 取方法
    return _def_embed  # 返回


def default_route_index() -> RouteIndex:  # 懒加载内置索引
    """首次调用才 embed（避免冷启动就在 import 时加载 ONNX）。"""
    global _def_index  # 改模块级
    if _def_index is None:  # 未建
        from app.agent.routing.rules.loader import load_routes  # 路由定义

        routes = load_routes()  # 载路由
        _def_index = load_or_build(routes, default_embed())  # 建索引
    return _def_index  # 返回