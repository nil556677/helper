"""语义兜底切分（P1.3）：**没有标点可断**的长段落，靠"语义边界"找断点。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
本包是切分器 ``semantic`` 兜底分支的实现，**不是主路径**：正常文档靠标题/标点切分，
只有"无结构 + 单句超预算"才会进来。

## 输入 / 输出

- 输入：一段超预算长文本 + 目标块大小；可注入嵌入/计数函数（测试用）。
- 输出：按原文顺序的块文本列表；失败返回 ``None``（调用方退回标点/硬切）。

## 模块分工

- ``units``：细粒度单元 + 词面/嵌入相似度；
- ``dp``：平滑 + DP 最优切点；
- ``ladder``：五级阶梯与递归；
- ``main``：对外入口、预算总闸与"能拼回原文"校验；
- ``_shared``：常量与惰性嵌入加载。

## 什么时候用（触发条件很窄）

只对**无结构文档**（没有标题树）里**单句超预算**的段落启用——有标题的文档不缺边界。
触发判断在 :class:`~app.core.chunk.Splitter` 里。

## 阶梯（选项 3B：**廉价信号优先**，嵌入是最后一级）

| 级 | 信号 | 为什么在这一级 |
| --- | --- | --- |
| 1 | **编号/条款号**（`第X条`、`（一）`、`1.1`） | 作者**显式**写的边界，比"算出来的"更准，且几乎免费 |
| 2 | **转折连接词**（综上/但是/因此…） | 话题转折的语用标记，词典就能判 |
| 3 | **词面重叠**（相邻单元的字组 Jaccard） | 免费、无模型依赖；同义改写识别不了 |
| 4 | **嵌入相似度** | 最强（认得同义改写），但要加载模型、要算一遍 |
| 5 | **按预算均匀切** | 兜到底：信号平坦时的确定性收尾 |

**关键点：前几级命中就不用加载模型**——既省一次嵌入 pass，也少一个"模型没装"的失败面。

## 断点到底怎么算（三级结构）

```
① 打分：细粒度单元（≈20% 预算）→ 相邻单元相似度序列 s₁..s_{n-1}
② 平滑：窗口 3 的移动平均（不平滑的话噪声会造出假谷）
③ 选点：**DP 最优分割**——不是在"某个阈值"上切，而是在
   "每块 ≤ 上限、≥ 下限"的**所有可行切法**里，最小化 Σ(切点处的相似度)
```

为什么不用"固定阈值"或"百分位阈值"：

- **固定阈值**（如 `< 0.7`）换文档就失效：合同类相似度普遍 0.95+、混合主题可能 0.5，
  同一个阈值在一份上切碎、在另一份上一个断点都没有；
- **百分位阈值**（把最低的 5% 当边界）**永远切 5%**——不管文本有没有话题，而且**它不管预算**，
  切不够就会出现"超预算的块"→ 进向量被截尾。我们的预算约束是硬的，
  所以正确的问题不是"阈值多少"，而是"**在必须切够刀数的前提下，断点放哪总代价最小**"。

**递归**：某一级切出的片段如果**仍然超预算**，就对它继续用下一级
（例如"第一条…"里某一款特别长 → 编号切完再用词面/嵌入切它）。
哪一级都搞不定时，那一段退回"按预算均匀切"——**只牺牲这一段，不牵连其他边界**。

## 代价与安全

- **惰性加载**：只有真走到第 4 级才加载模型（``embedder()`` 只解析函数，不建 ONNX 会话）；
- **失败降级**：模型缺失/嵌入报错 → 第 4 级跳过、落到预算切；单元太多/拼不回原文 →
  整体放弃并返回 ``None``（调用方退回标点/硬切）——**绝不让导入失败**；
- **确定性**：单元划分、批组成、DP 都是确定的（同一输入切出同样结果）；
- **可观测**：返回时带上用了哪一级（日志可见），不假装"这都是语义切出来的"。
"""

from __future__ import annotations

from app.core.chunk.semantic._shared import (
    _EMBED_FAILED,
    _EMBEDDER,
    _FLAT_SPREAD,
    _MIN_CHUNK_RATIO,
    _SMOOTH_WINDOW,
    _UNIT_RATIO,
    MAX_UNITS,
    embedder,
)
from app.core.chunk.semantic.dp import is_flat, optimal_cuts, smooth
from app.core.chunk.semantic.ladder import (
    _NUMBERED_BREAK,
    _TURN_WORDS,
    LEVEL_BUDGET,
    LEVEL_EMBEDDING,
    LEVEL_LEXICAL,
    LEVEL_NUMBERED,
    LEVEL_TURN_WORD,
    _merge_units,
    _score_level,
    _split_by_pattern,
    _split_by_turn_words,
    _split_piece,
    budget_split,
    enforce_budget,
)
from app.core.chunk.semantic.main import _default_count, split_semantically
from app.core.chunk.semantic.units import (
    _BREAK_BACK_RATIO,
    _BREAK_CHARS,
    _fit_length,
    _jaccard,
    _ngrams,
    _units,
    embedding_similarities,
    lexical_similarities,
)
from app.core.parse.clean import compact_ws

__all__ = [
    "LEVEL_BUDGET",
    "LEVEL_EMBEDDING",
    "LEVEL_LEXICAL",
    "LEVEL_NUMBERED",
    "LEVEL_TURN_WORD",
    "MAX_UNITS",
    "_BREAK_BACK_RATIO",
    "_BREAK_CHARS",
    "_EMBEDDER",
    "_EMBED_FAILED",
    "_FLAT_SPREAD",
    "_MIN_CHUNK_RATIO",
    "_NUMBERED_BREAK",
    "_SMOOTH_WINDOW",
    "_TURN_WORDS",
    "_UNIT_RATIO",
    "_default_count",
    "_fit_length",
    "_jaccard",
    "_merge_units",
    "_ngrams",
    "_score_level",
    "_split_by_pattern",
    "_split_by_turn_words",
    "_split_piece",
    "_units",
    "budget_split",
    "compact_ws",
    "embedder",
    "embedding_similarities",
    "enforce_budget",
    "is_flat",
    "lexical_similarities",
    "optimal_cuts",
    "smooth",
    "split_semantically",
]
