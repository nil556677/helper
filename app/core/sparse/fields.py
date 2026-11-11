"""关键词多字段 BM25：字段清单 + 每字段权重与 BM25 常数（单一事实源）。

依据（生产经验比例，见关键词索引设计）：
- 权重：标题 5x、重要关键词 3x、实体/标签 2.5x、正文 1x（基准）；比例上限 ≤10:1。
- 每字段独立 ``b``（长度归一）：标题/实体是短字段 b 压低（0.3~0.5），正文/代码用标准 0.75。
- ``k1`` 各字段统一（1.2）。
"""

from __future__ import annotations  # 延迟值注解

#: 字段名（与 Qdrant 命名稀疏向量、每字段统计键共用）。
FIELD_TITLE = "title"  # 标题/主题（信号最强）
FIELD_KWD = "kwd"  # 重要关键词（低频高区分度领域概念）
FIELD_ENTITY = "entity"  # 实体/标签（编号/错误码/结构化 tags）
FIELD_CONTENT = "content"  # 正文（基准，信号最弱但最全）
FIELD_CODE = "code"  # 代码/标识符（可选，技术文档）

#: 字段顺序（稳定顺序用于加权合并、统计遍历）。
FIELDS: tuple[str, ...] = (FIELD_TITLE, FIELD_KWD, FIELD_ENTITY, FIELD_CONTENT, FIELD_CODE)  # 全部字段

#: 实际进入 Qdrant 稀疏索引的字段（code 可选，暂不入；写入方只产这 4 个）。
INDEX_FIELDS: tuple[str, ...] = (FIELD_TITLE, FIELD_KWD, FIELD_ENTITY, FIELD_CONTENT)  # 建索引字段


def sparse_vector_name(field: str) -> str:  # 字段 → Qdrant 命名稀疏向量名
    """``content`` → ``s_content``；与集合 sparse_vectors_config 的键一致。"""
    return f"s_{str(field)}"  # 加前缀避免与 dense 撞名

#: 每字段权重（检索加权合并用；正文为 1x 基准）。
FIELD_WEIGHTS: dict[str, float] = {
    FIELD_TITLE: 5.0,  # 标题 5x
    FIELD_KWD: 3.0,  # 重要关键词 3x
    FIELD_ENTITY: 2.5,  # 实体/标签 2.5x
    FIELD_CONTENT: 1.0,  # 正文 1x（基准）
    FIELD_CODE: 1.0,  # 代码 1x（可选，暂与正文同权）
}  # 权重结束

#: 每字段 k1（词频饱和；各字段统一 1.2）。
FIELD_K1: dict[str, float] = {name: 1.2 for name in FIELDS}  # 统一

#: 每字段 b（长度归一；短字段压低避免过度惩罚）。
FIELD_B: dict[str, float] = {
    FIELD_TITLE: 0.4,  # 标题短 → b 低
    FIELD_KWD: 0.5,  # 关键词字段短 → b 低
    FIELD_ENTITY: 0.3,  # 实体/标签极短 → b 最低
    FIELD_CONTENT: 0.75,  # 正文标准
    FIELD_CODE: 0.75,  # 代码/正文同标准
}  # b 结束


def field_weight(name: str) -> float:  # 取字段权重（未知字段按 1.0 基准）
    """取某字段权重；未知字段返回 1.0（不因拼错字段而炸）。"""
    return float(FIELD_WEIGHTS.get(str(name), 1.0))  # 查表


def weight_ratio_ok(max_ratio: float = 10.0) -> bool:  # 权重比例不超上限
    """校验最高权/最低权之比 ≤ ``max_ratio``（避免高权重字段无差别碾压）。"""
    values = [v for k, v in FIELD_WEIGHTS.items() if k != FIELD_CODE]  # 正文/标题/关键词/实体
    if not values:
        return True  # 空视为通过
    lo, hi = min(values), max(values)  # 极值
    return hi / lo <= max_ratio if lo else True  # 比例校验


def per_field_params() -> dict[str, dict[str, float]]:  # 每字段 (k1, b)
    """给 bm25.encode_document 用的每字段参数表。"""
    return {  # 表
        name: {"k1": FIELD_K1[name], "b": FIELD_B[name]}  # 字段参数
        for name in FIELDS  # 逐字段
    }  # 返回


def combine_field_scores(  # 多字段加权合并（检索侧打分）
    scores: dict[str, float],  # {字段名: 该字段对当前 chunk 的 BM25 分}
    *,
    unknown_weight: float = 1.0,  # 未知字段默认权重
) -> float:  # 加权和
    """``final = Σ field_weight × score_field``（未知字段按默认权重）。"""
    total = 0.0  # 累加
    for field, score in scores.items():  # 逐字段
        total += field_weight(str(field)) * float(score)  # 加权累加
    del unknown_weight  # 占位：field_weight 已带未知字段兜底（1.0）
    return total  # 返回