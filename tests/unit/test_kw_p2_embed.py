"""关键词多字段：P2 每字段 avgdl 与多字段 BM25 编码。"""

from __future__ import annotations

from app.core.db import schema_v1  # 建表
from app.core.db.connection import get_connection  # 连接
from app.core.sparse import fields as kw_fields  # 字段常量
from app.core.sparse.bm25 import idf  # idf
from app.integrations.sparse import JiebaBm25SparseEmbeddings, SparseTermStore  # store/编码


def _store(fake_root) -> SparseTermStore:  # 建 store（建 sparse 表并清空）
    del fake_root  # 仅保证 DB 就绪
    schema_v1.initialize(get_connection())  # 建 sparse_terms/sparse_meta 表
    store = SparseTermStore()  # 实例
    store.reset()  # 清空（每用例干净）
    return store  # 返回


def test_embed_multi_field_per_field_b(fake_root) -> None:  # 每字段 avgdl/b 生效
    emb = JiebaBm25SparseEmbeddings(_store(fake_root))  # 实例
    vectors = emb.embed_multi_field(  # 多字段编码
        {
            kw_fields.FIELD_TITLE: "售后政策",  # 标题（短）
            kw_fields.FIELD_CONTENT: "订单 HT-2026-0043 支持极速退货退款处理流程说明",  # 正文（长）
        }
    )  # 结束
    assert set(vectors) == {kw_fields.FIELD_TITLE, kw_fields.FIELD_CONTENT}  # 两个字段
    store = emb._store  # store
    assert store.field_avgdl(kw_fields.FIELD_TITLE) < store.field_avgdl(kw_fields.FIELD_CONTENT)  # 标题 avgdl 远小于正文


def test_embed_multi_field_df_counted_once(fake_root) -> None:  # df 不因跨字段重复计数
    emb = JiebaBm25SparseEmbeddings(_store(fake_root))  # 实例
    # chunk1：词"售后"出现在标题和正文各一次 → df 应为 1（不是 2）
    emb.embed_multi_field(
        {
            kw_fields.FIELD_TITLE: "售后",  # 标题含
            kw_fields.FIELD_CONTENT: "本页说明售后政策",  # 正文也含
        }
    )
    stats = emb._store.term_stats(["售后"])  # 取统计
    assert stats.get("售后") is not None  # 词已登记
    assert stats["售后"][1] == 1  # df=1（一份 chunk，不是 2）
    # chunk2：正文又含"售后" → 现在是第 2 份文档含它 → df=2
    emb.embed_multi_field({kw_fields.FIELD_CONTENT: "售后流程见下"})  # 第二份
    stats2 = emb._store.term_stats(["售后"])  # 再取
    assert stats2["售后"][1] == 2  # df=2（两份 chunk，非 3）


def test_idf_uses_n_over_all_fields(fake_root) -> None:  # idf 用全局 N（跨字段一致）
    emb = JiebaBm25SparseEmbeddings(_store(fake_root))  # 实例
    emb.embed_multi_field({kw_fields.FIELD_TITLE: "售后", kw_fields.FIELD_CONTENT: "政策手册"})  # 一个 chunk
    n = emb._store.n()  # 全局文档数
    df = emb._store.term_stats(["售后"])["售后"][1]  # 该词 df
    score = idf(n, df)  # idf
    assert score == idf(1, 1)  # N=1, df=1 → 可复现