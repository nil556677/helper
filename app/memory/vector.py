"""记忆向量集合（P3）：把长期记忆 embedding 落到 Qdrant，替换 sqlite 内存余弦。

**选型**：复用 `app/integrations/qdrant.py` 的集合约定；sqlite 仍保留为**降级/审计**数据源
（嵌入不可用或 Qdrant 未运行时走 sqlite 全扫+衰减）。**指纹**：本域声明 `fingerprint.memory`，
换嵌入模型时必须重建（与文档向量不同空间）。

**域纪律**：本模块只被 `app/agent/memory/manager.py` 或 `app/services/memory.py` 调用；
UI 不直连。
"""

from __future__ import annotations  # 延迟值注解

import time  # 时间戳
from typing import Any  # 任意类型（客户端惰性）

from app.memory.model import FINGERPRINT_KEY  # 指纹键

__all__ = ["FINGERPRINT_KEY", "MEMORY_COLLECTION", "delete_memory_points", "ensure_memory_health", "search_memory_points", "upsert_memory_points"]  # 导出

#: 记忆专用集合名（与 chunks 隔离，避免跨集合串扰）。
MEMORY_COLLECTION = "user_memories"  # 记忆集合

#: 单查询上限（防过量召回）。
SEARCH_LIMIT = 50  # Qdrant 一次召回上限


def _client() -> Any:  # Qdrant 客户端（延迟导入）
    """可复用客户端；Qdrant 未就绪时返回 None（调用方走降级）。"""
    try:  # 未配置/未启动安全
        from app.integrations import qdrant as qi  # Qdrant 适配

        return qi.qdrant_client()  # 客户端
    except Exception:  # 未就绪
        return None  # 降级


def search_memory_points(  # 按向量召回记忆点（返回 memory_key + score）
    namespace: str,  # 作用域（过滤用）
    vector: list[float],  # 查询向量
    *,
    top_k: int = 10,  # 上限
    status: str = "active",  # 只召回该状态
) -> list[dict]:  # [{memory_key, score}]
    """Qdrant 余弦召回本作用域 active 的记忆点（与 RAG dense_search 同套话）。

    返回按相似度降序的 key+score；Qdrant 不可用/失败返回空（调用方降级 sqlite）。
    """
    client = _client()  # 客户端
    if client is None:  # 不可用
        return []  # 降级
    try:  # 查询失败降级
        from qdrant_client import models  # 过滤/查询模型

        hits = client.query_points(  # 查询
            collection_name=MEMORY_COLLECTION,  # 集合
            query=list(vector),  # 查询向量
            query_filter=models.Filter(  # 过滤
                must=[  # 必须满足
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),  # 作用域
                    models.FieldCondition(key="status", match=models.MatchValue(value=status)),  # 状态
                ],  # 必须结束
            ),  # 过滤结束
            limit=min(int(top_k) * 4, SEARCH_LIMIT),  # 放宽上限供内存重排（会计账后置）
            with_payload=True,  # 需要 payload 取 key
        )  # 查
        out: list[dict] = []  # 结果
        for hit in hits.points:  # 逐命中
            payload = hit.payload or {}  # 负载
            key = str(payload.get("memory_key") or "")  # 键
            if not key:  # 无键（防御）
                continue  # 跳过
            out.append({"memory_key": key, "score": float(hit.score or 0.0)})  # 候选
        return out  # 返回
    except Exception:  # 查询失败
        return []  # 降级


def ensure_memory_health() -> bool:  # 建集合/探活
    """确保记忆集合存在；Qdrant 不可用返回 False（降级 sqlite）。"""
    client = _client()  # 客户端
    if client is None:  # 不可用
        return False  # 降级
    try:  # 建集合（幂等）
        client.get_collection(MEMORY_COLLECTION)  # 探活
    except Exception:  # 集合不存在
        try:  # 建
            client.create_collection(  # 建集合
                collection_name=MEMORY_COLLECTION,  # 名
                vectors_config={"size": 768, "distance": "Cosine"},  # bge-m3 默认 768 维余弦
            )  # 建
            _ensure_payload_indexes(client)  # 建 payload 索引（过滤字段）
            _stamp_fingerprint(client)  # 记指纹
        except Exception:  # 建失败（如远端只读）
            return False  # 降级
    return True  # 可用


def _ensure_payload_indexes(client: Any) -> None:  # 建过滤字段索引
    """给 namespace/status 建 KEYWORD 索引（查询过滤快，参照 RAG ensure_collection）。"""
    from contextlib import suppress  # 抑制失败（索引缺失不致命）

    from qdrant_client import models  # 索引模型

    for field in ("namespace", "status"):  # 可过滤字段
        with suppress(Exception):  # 建失败不影响主体
            client.create_payload_index(  # 建索引
                collection_name=MEMORY_COLLECTION,  # 集合
                field_name=field,  # 字段
                field_schema=models.PayloadSchemaType.KEYWORD,  # 关键字索引
            )  # 建


def _stamp_fingerprint(client: Any) -> None:  # 记录指纹
    """把当前嵌入模型指纹写入集合元数据（换模型重建提示用）。"""
    from contextlib import suppress  # 抑制写失败

    with suppress(Exception):  # 写失败不影响（降级仍有 sqlite）
        client.update_collection(  # 更新元数据
            MEMORY_COLLECTION,  # 集合
            metadata={"fingerprint": FINGERPRINT_KEY, "model": "bge-m3"},  # 指纹+模型
        )  # 更新


def upsert_memory_points(  # 批量写向量点
    namespace: str,  # 作用域（过滤用）
    points: list[dict],  # [{memory_key, vector, value, status}]
) -> int:  # 写入点数
    """把若干记忆的向量写入 Qdrant（payload 带 namespace/status 供过滤）。

    返回写入数；Qdrant 不可用返回 0（调用方继续走 sqlite）。
    """
    client = _client()  # 客户端
    if client is None:  # 不可用
        return 0  # 降级
    from qdrant_client import models  # 点模型

    payloads = []  # 待写点
    for item in points:  # 逐条
        vec = item.get("vector")  # 向量
        if not vec:  # 无向量
            continue  # 跳过
        payloads.append(  # 点
            models.PointStruct(  # 点结构
                id=hash(f"{namespace}:{item.get('memory_key')}") & 0x7FFFFFFF,  # 稳定点 id（同键同点）
                vector=list(vec),  # 向量
                payload={  # 负载（检索侧过滤）
                    "namespace": namespace,  # 作用域
                    "memory_key": item.get("memory_key"),  # 键
                    "status": item.get("status", "active"),  # 状态
                    "t": float(item.get("t") or time.time()),  # 时间戳（时效过滤）
                },  # 负载结束
            )  # 结束
        )  # 追加
    if not payloads:  # 无点
        return 0  # 无写
    try:  # 写
        client.upsert(  # 批量写
            collection_name=MEMORY_COLLECTION,  # 集合
            points=payloads,  # 点
        )  # 写
        return len(payloads)  # 写入数
    except Exception:  # 写失败
        return 0  # 降级


def delete_memory_points(namespace: str) -> int:  # 删作用域全部向量点
    """按作用域清空记忆向量点（合规删除联动）。"""
    client = _client()  # 客户端
    if client is None:  # 不可用
        return 0  # 无需删
    try:  # 删
        from qdrant_client import models  # 过滤模型

        client.delete(  # 删点
            collection_name=MEMORY_COLLECTION,  # 集合
            points_selector=models.FilterSelector(  # 按 namespace 过滤
                filter=models.Filter(  # 过滤条件
                    must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace))],  # 作用域
                )  # 条件
            ),  # 选择器
        )  # 删
        return 1  # 成功
    except Exception:  # 删除失败
        return 0  # 忽略
