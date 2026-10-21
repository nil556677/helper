"""RAG 的领域模型：数据形状的**规范导入路径**。

为什么需要它：入库侧（切分 / 写入）与检索侧都要引用"片段记录"和"导入结果"，以前得从
``splitter.records`` / ``ingest.streaming`` 各自 import。收在这里之后，外部只认
``app.rag.model``，内部怎么拆不影响调用方。

**只做再导出，不写逻辑**：类型仍由各自模块定义（避免循环依赖，也省搬家成本）。
"""

from app.core.chunk.records import ChunkGroup, ChunkRecord
from app.rag.ingest.streaming import ImportResult

__all__ = ["ChunkGroup", "ChunkRecord", "ImportResult"]
