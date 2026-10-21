"""向量集合注册表：**一个领域一组集合，名字带领域前缀**。

放到这里而不是散在各处常量，是为了让 ``api.stats()``（界面「集合与占用」）自动列出
所有领域的集合——新领域加一行注册即可，不用改 api/UI。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionSpec:
    """一个集合的声明。"""

    name: str
    """Qdrant 集合名（**带领域前缀**）。"""

    owner: str
    """所属领域（``rag`` / ``memory`` / ``tools``）。"""

    vectorless: bool = False
    """是否无向量集合（parent 这种只存 payload 的）。"""

    enrich_attrs: bool = True
    """写入时是否跑动态字段提取（parent 是证据块、不是检索单位，关掉）。"""


RAG_CHUNKS = CollectionSpec("rag_chunks", "rag")
RAG_PARENTS = CollectionSpec("rag_parents", "rag", vectorless=True, enrich_attrs=False)
MEMORY = CollectionSpec("memory", "memory")
TOOLS = CollectionSpec("tools", "tools")

#: 全部集合（界面/对账遍历它）。
ALL: tuple[CollectionSpec, ...] = (RAG_CHUNKS, RAG_PARENTS, MEMORY, TOOLS)

#: 旧集合名 → 新集合（改名前的库要迁移）。
LEGACY: dict[str, CollectionSpec] = {
    "chunks": RAG_CHUNKS,
    "parents": RAG_PARENTS,
}


def resolve(name: str) -> str:
    """把旧集合名解析成新名字（其它名字原样返回）。"""
    spec = LEGACY.get(str(name))
    return spec.name if spec is not None else str(name)


#: 名字 → 声明（``spec_for`` 用）。
_BY_NAME: dict[str, CollectionSpec] = {spec.name: spec for spec in ALL}


def spec_for(name: str) -> CollectionSpec | None:
    """按集合名（兼容旧名）取声明；未注册的集合返回 ``None``（调用方按默认处理）。"""
    return _BY_NAME.get(resolve(name))
