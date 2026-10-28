"""检索算法积木（保留）：分词 ``tokenize``（jieba + 领域词）、BM25 数学 ``core.sparse``。"""

from app.core.retrieval.tokenize import cut, to_tokens

__all__ = ["cut", "to_tokens"]
