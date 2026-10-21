"""第一个功能：检索问答（RAG）。

- ``ingest/``：入库编排（解析 → 切分 → 判重 → 落库并建索引）；
- ``splitter/`` / ``semantic/``：切分引擎与无标点长段落的语义兜底；
- ``store/``：``documents`` / ``chunks`` 的唯一出入库口；
- ``model.py``：领域类型（``ImportResult`` / ``ChunkGroup`` / ``ChunkRecord``）的规范入口；
- ``plan.py`` / ``retrieval.py``：待补（``app/core/retrieval/`` 契约落地后）。

检索与作答还没做：**切分与检索策略等界面交互定稿后再谈**（用户要求）。

分词（`core/retrieval/tokenize.py`）已经就位，因为**写入与查询必须共用同一套切法**——
它不属于导入、也不属于检索，而是两者之间的契约。
"""
