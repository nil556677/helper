"""检索引擎：词法（FTS5）+ 向量 + 融合。

**现在还只有分词**（见 :mod:`app.core.retrieval.tokenize`）：切分与检索策略等界面交互定稿后再做。
分词放在这里，是因为**写入与查询必须共用同一套切法**——它既不属于导入，也不属于检索，
而是两者之间的契约。
"""

from app.core.retrieval.tokenize import cut, to_tokens

__all__ = ["cut", "to_tokens"]
