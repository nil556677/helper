"""关键词多字段 BM25：字段抽取（纯函数；输入 chunk 文本/元数据，输出各字段文本）。

设计（关键词索引设计 §字段）：
- title：直接用文档标题（信号最强）；
- content：正文原文（基准）；
- entity：结构化 tags + 从正文识别的编号/代码（复用 `tokenize` 的原子识别，精确术语是 BM25 杀手锏）；
- kwd：从正文命中、且**文档频率不高**的领域词（低频高区分度才进，避免高频词的噪声）。
"""

from __future__ import annotations  # 延迟值注解

import re  # 编号形状

from app.core.sparse.fields import FIELD_CONTENT, FIELD_ENTITY, FIELD_KWD, FIELD_TITLE  # 字段名

#: 编号/代码形状（entity 字段捕获的"精确术语"）：含数字/拉丁/连接符的类标识符 token。
_CODE_LIKE = re.compile(r"\d|[/\-_:.]|[A-Za-z]{2,}|[A-Za-z]\d")  # 类标识符

#: 每字段最多进几个领域词（防字段膨胀）。
MAX_KWD = 10  # 上限


def _code_like_tokens(content: str) -> list[str]:  # 抽出编号/代码类 token
    """复用 indexe 分词（原子编号已整体保留），只留"像标识符"的（实体字段）。"""
    from app.core.retrieval.tokenize import cut  # 分词（编号原子开保护）

    return [  # 过滤
        tok  # token
        for tok in cut(str(content or ""))  # 全切
        if _CODE_LIKE.search(tok)  # 像编号/代码/实体
    ]  # 返回


def extract_important_kwd(  # 重要关键词（落点二）
    content: str,  # 正文
    domain_terms: list[str],  # 领域词表
    *,
    high_df_words: frozenset[str] = frozenset(),  # 高频词（退出，勿进字段）
    max_kwd: int = MAX_KWD,  # 最多几个
) -> list[str]:  # 关键词列表
    """命中且非高频的领域词（≤max_kwd），用于 kwd 字段享受高权重。"""
    text = str(content or "")  # 正文
    out: list[str] = []  # 结果
    for word in domain_terms or ():  # 逐个领域词
        if not word or word in high_df_words:  # 空/高频（无区分度）
            continue  # 跳过
        if word in text and word not in out:  # 命中且不重复
            out.append(word)  # 记
        if len(out) >= max_kwd:  # 满了
            break  # 停
    return out  # 返回


def chunk_field_texts(  # 主入口：chunk → 每字段文本
    *,
    content: str,  # 正文
    title: str = "",  # 标题
    tags: list[str] | None = None,  # 结构化标签（直接映射进实体字段）
    domain_terms: list[str] | None = None,  # 领域词表（kwd 字段来源）
    high_df_words: frozenset[str] = frozenset(),  # 高频领域词（不进 kwd）
    max_kwd: int = MAX_KWD,  # kwd 上限
) -> dict[str, str]:  # {字段名: 该字段文本}
    """把 chunk 拆成各字段的**文本**（由调用方再统一分词/编码）。"""
    title = str(title or "")  # 标题
    content = str(content or "")  # 正文
    entities = list(tags or []) + _code_like_tokens(content)  # 实体 = 标签 + 编号/代码
    kwd = extract_important_kwd(  # 重要关键词
        content, list(domain_terms or ()),  # 正文 + 领域词
        high_df_words=high_df_words,  # 高频排除
        max_kwd=max_kwd,  # 上限
    )  # 结束
    return {  # 各字段文本
        FIELD_TITLE: title,  # 标题
        FIELD_ENTITY: " ".join(list(dict.fromkeys(entities))[:32]),  # 实体（去重、上限）
        FIELD_KWD: " ".join(kwd),  # 重要关键词
        FIELD_CONTENT: content,  # 正文
    }  # 返回