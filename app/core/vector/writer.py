"""统一写入器：**所有模块把数据传进来 → 补动态字段 → 写 Qdrant**（唯一写入口）。

设计见 ``docs/动态字段与存储边界改造方案.md`` §4.4。核心保证：

1. **提取在存储之前**：``enrich`` 在 ``VectorSession.add`` 之前跑，写入库的内容已带
   ``attrs*`` 与 ``attrs_version``；
2. **所有模块共用**：RAG / memory / tools 只需构造 :class:`StoredRecord` 列表，不写提取代码；
3. **集合可关**：``CollectionSpec.enrich_attrs=False`` 的集合（如 parent）跳过提取；
4. **同批去重**：同一 ``text`` 在一次调用里只提取一次。

**不放这里的事**：向量怎么算（调用方算好放进 ``vector``）、``key`` 怎么分配（调用方定）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredRecord:
    """要写进向量库的一条记录（child / memory / tool …）。"""

    key: str
    """Qdrant point key（``doc:version:seq`` / ``mem:42`` …）。"""

    vector: object | None
    """dense 向量（ndarray）；无向量集合传 ``None``。"""

    payload: dict
    """调用方自己的元数据（``text``/``section_path``/``status`` …）。"""

    text: str = ""
    """提取动态字段用的正文；空则回退 ``payload["text"]``。"""

    extract: bool = True
    """本条是否参与动态字段提取（默认参与）。"""


def store_records(collection: str, records: Sequence[StoredRecord]) -> int:
    """**唯一写入口**：先补动态字段，再写 Qdrant。返回写入条数。

    参数：
        collection: 集合名（``rag_chunks`` / ``memory`` …）。
        records: 待写记录；``key`` 必须唯一（重复投递由 Qdrant upsert 幂等覆盖）。
    返回：写了几条。
    """
    from app.core.attrs.api import apply_attrs, build_attrs
    from app.core.config.store import mark_attrs_version
    from app.core.vector import api
    from app.core.vector.collections import spec_for

    spec = spec_for(collection)
    do_extract = spec.enrich_attrs if spec is not None else True

    entries: list[dict] = []
    # 同批同文本只提取一次，但**每条都要应用结果**（复用一个缓存，别漏掉后续记录）
    cache: dict[str, dict] = {}
    enriched = False
    for record in records:
        payload = dict(record.payload)
        extractable = do_extract and record.extract
        text = (record.text or str(payload.get("text") or "")) if extractable else ""
        if text:
            attrs = cache.get(text)
            if attrs is None:
                attrs = build_attrs(text)
                cache[text] = attrs
                enriched = True
            apply_attrs(payload, attrs)
        entries.append({"key": record.key, "vector": record.vector, "payload": payload})

    if entries:
        with api.VectorSession(collection) as session:
            session.add(entries)
        # 写了带属性的数据 → 把当前 ATTRS_VERSION 记为"索引就是按它建的"（供 UI 判过期）
        if enriched:
            mark_attrs_version()
    return len(entries)
