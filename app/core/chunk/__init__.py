"""切分（P1.3）：**块 → parent/child 记录树**（带结构与定位）。

这一层是"数据精准"的最后一公里：P1.1 解析出带定位的块、P1.2 标出标题树，
切分必须把它们**完整带进库**（旧版把块压平成一段纯文本，定位与结构全丢了）。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。

## 输入 / 输出

- 输入：按文档顺序排好的 ``Block`` 列表（``feed`` 可多次，逐单元喂）。
- 输出：``ChunkGroup``（parent + children），每个 child 都带结构与定位。

## 模块分工

| 模块 | 职责 |
| --- | --- |
| ``core`` | 流式 ``Splitter``：消费块、开/闭 parent、聚合 child |
| ``sentences`` / ``discourse`` | 硬句边界 / 软边界（标点 + 连接词） |
| ``policy`` | 块类型 → 切分策略（唯一出处） |
| ``derived`` / ``estimate`` | 尺寸推导 / token 计数 |
| ``pieces`` / ``records`` / ``loc`` | 中间结构 / 写库记录 / 定位映射 |
| ``diagnostics`` | 降级与异常聚合（不静默） |
| ``semantic`` | 无标点长段落的语义兜底阶梯 |

## 两级结构

- **parent**（送模型的证据，`parent_size` token）：由**标题开启**——每个 H1–H4 起一个新 parent；
  表格/代码各自成 parent；超长节按 `parent_overlap` 再分。
- **child**（检索的最小单位，`child_size` token）：parent 内按句聚合，带 `child_overlap` 重叠。

## 硬约束（违反就是"搜不到"或"引用错位"）

1. **child 不跨块、不跨段落**：块是 P1.1 的语义单元（含跨页续接块），空行是作者给的语义边界；
2. **child ≤ child_size ≤ PAD_TO(320) token**：超了嵌入时尾部被静默截掉（P0.2 实测），
   所以**超预算的原子块（表格/代码）按整行切开并重复表头**，绝不切半行；
3. **标点切分有优先级退让**：句末标点 → 续句标点 → 硬切（并打 ``hard_split`` 留痕）。

## 定位继承

child 通过**字符区间 → 行号**映射拿到自己的行区间；PDF 再用 ``Block.line_coords``
算出只覆盖自己那几行的 bbox（高亮不会把整段框住）。
"""

from __future__ import annotations

from app.core.chunk._shared import (
    _CLAUSE_END,
    _MIN_CHILD_RATIO,
    _PAIRS,
    _QUOTES,
    _SENTENCE_END,
    _TRAILING,
    TOKENS_PER_CHAR,
)
from app.core.chunk.core import Splitter, _semantic_runner
from app.core.chunk.estimate import (
    _REAL_COUNTERS,
    estimate_tokens,
    real_token_counter,
)
from app.core.chunk.loc import _coords_span, _line_span
from app.core.chunk.pieces import _OpenParent, _Piece
from app.core.chunk.records import ChunkGroup, ChunkRecord
from app.core.chunk.sentences import (
    _clause_spans,
    _inside_number,
    _sentence_spans,
    _trim,
    split_sentences,
)

__all__ = [
    "TOKENS_PER_CHAR",
    "_CLAUSE_END",
    "_MIN_CHILD_RATIO",
    "_PAIRS",
    "_QUOTES",
    "_REAL_COUNTERS",
    "_SENTENCE_END",
    "_TRAILING",
    "ChunkGroup",
    "ChunkRecord",
    "Splitter",
    "_OpenParent",
    "_Piece",
    "_clause_spans",
    "_coords_span",
    "_inside_number",
    "_line_span",
    "_semantic_runner",
    "_sentence_spans",
    "_trim",
    "estimate_tokens",
    "real_token_counter",
    "split_sentences",
]
