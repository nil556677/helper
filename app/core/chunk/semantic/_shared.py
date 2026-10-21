"""语义切分包的**共享私有件**：常量 + 惰性嵌入加载器。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
本模块只属于切分里的"语义兜底"分支（``semantic/``）；正常有标点的文档根本不会走到这里。

## 输入 / 输出

- 常量：无输入；被 ``dp`` / ``ladder`` / ``main`` / ``units`` 共用。
- :func:`embedder`：输入无，输出嵌入函数或 ``None``（拿不到模型时降级）。

## 关键约定

1. **惰性**：模型只在真正需要第 4 级（嵌入）时才加载；失败后**不再重试**
   （``_EMBED_FAILED``），避免每次切分都等一次注定失败的加载；
2. ``_UNIT_RATIO`` / ``_MIN_CHUNK_RATIO`` / ``_SMOOTH_WINDOW`` / ``_FLAT_SPREAD``
   是 DP 的输入口径，改它们等于改"切多细 / 切几刀"；
3. ``compact_ws`` 真身在外层 :mod:`app.core.parse.clean`，需要处直接从这里 import 即可。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from loguru import logger

#: 打分单元的相对大小（占 child 预算）：**细粒度打分、粗粒度成块**。
#: 太大（如半个预算）会把边界"平均"掉，太小则相似度噪声大。
_UNIT_RATIO = 0.2
#: 最小块长（占预算比例）：低于它不算一个合法块（除非是最后一块）
_MIN_CHUNK_RATIO = 0.4
#: 平滑窗口（移动平均）：TextTiling 一族都做这一步
_SMOOTH_WINDOW = 3
#: 相似度极差小于它 → 信号"平坦"（整段一个主题），这一级不用（升级或退预算切）
_FLAT_SPREAD = 0.04
#: 单元数上限：超长无标点文本不该把一次导入拖成几分钟（DP 是 O(n²)）
MAX_UNITS = 400

#: 嵌入函数缓存（惰性；失败就不再重试）
_EMBEDDER: Callable[[Sequence[str]], object] | None = None
_EMBED_FAILED = False


def embedder() -> Callable[[Sequence[str]], object] | None:
    """拿到嵌入函数（``embed_texts``）；拿不到返回 ``None``（调用方降级）。

    返回：``embed_texts`` 或 ``None``；同一进程内只解析/失败一次。
    说明：这是语义阶梯第 4 级的入口。失败（缺依赖/模型加载失败）只告警，
        并置 ``_EMBED_FAILED`` 让后续调用直接返回 ``None``——第 4 级会被跳过、
        落到预算切，导入流程不能因此失败。
    """
    global _EMBEDDER, _EMBED_FAILED
    if _EMBEDDER is not None or _EMBED_FAILED:
        # 已解析过（成功或失败）：直接返回缓存状态，失败不反复重试
        return _EMBEDDER
    try:
        from app.core.embed import embed_texts

        _EMBEDDER = embed_texts
    except Exception as exc:  # 缺依赖/模型加载失败都不该让导入失败
        logger.warning("语义兜底拿不到嵌入模型（会退到词面/预算切）：{}", exc)
        _EMBED_FAILED = True
        _EMBEDDER = None
    return _EMBEDDER
