"""对外入口：语义阶梯切分（``split_semantically``）与默认计数/嵌入装配。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
切分器的 ``_semantic_runner`` 惰性调用本模块；只有"无结构 + 单句超预算"的段落
才会真正进来（有标题、或标点够断句的文档不触发）。

## 输入 / 输出

- 输入：超预算长文本 + 目标块大小 + 可注入的嵌入/计数函数。
- 输出：按原文顺序、可直接定位的块列表；无法可靠兜底时返回 ``None``
  （调用方退回标点/硬切）。

## 关键约定

1. **确定性**：同样输入切出同样结果（单元划分/DP 都是确定的）；
2. **不超预算**：最后有 :func:`enforce_budget` 总闸按真实计数收口；
3. **切分结果必须能拼回原文**（忽略空白差异）：拼不回就放弃，绝不返回错位片段；
4. **失败只降级**：模型缺失/单元过多/校验失败都返回 ``None``，不抛异常。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from loguru import logger

from app.core.chunk.semantic._shared import _UNIT_RATIO, MAX_UNITS, embedder
from app.core.chunk.semantic.ladder import _split_piece, enforce_budget
from app.core.chunk.semantic.units import _units
from app.core.parse.clean import compact_ws


def _default_count(text: str) -> int:
    """默认用切分器那套估算（**延迟导入**：避免与 splitter 形成循环 import）。

    参数：
        text: 待计数文本。
    返回：token 数（字符估算；至少 1）。
    """
    from app.core.chunk import estimate_tokens

    return estimate_tokens(text)


def split_semantically(
    text: str,
    target_tokens: int,
    *,
    embed: Callable[[Sequence[str]], object] | None = None,
    count_tokens: Callable[[str], int] | None = None,
) -> list[str] | None:
    """按**语义阶梯**把一段长文本切成若干块。

    参数：
        text: 待切的长文本（调用方保证它超预算、且没有标题结构）。
        target_tokens: 目标块大小（child 预算）。
        embed: 嵌入函数（测试可注入假的；不传则惰性取真模型）。
        count_tokens: 数 token 的函数（默认用切分器那套估算）。
    返回：切好的块（**按原文顺序、可直接在原文里定位**）；实在没法兜底时返回 ``None``。
    说明：返回 ``None`` 是正常降级路径（调用方会退回标点/硬切），不是异常。
    """
    count = count_tokens or _default_count
    if not text.strip() or target_tokens <= 0:
        return None
    # 先用与打分一致的细单元估算规模：单元太多说明文本极长，DP（O(n²)）不值得做
    unit_budget = max(1, int(target_tokens * _UNIT_RATIO))
    if len(_units(text, unit_budget, count)) > MAX_UNITS:
        logger.info("语义兜底放弃：候选单元太多（> {}）", MAX_UNITS)
        return None

    used: list[str] = []
    # embed 为 None 时用 embedder() 惰性取；拿不到则第 4 级自动跳过。
    # 模型本身在第 4 级真正调用嵌入时才加载（前几级命中就不会走到那一步）。
    pieces = _split_piece(text, target_tokens, count, embed or embedder(), used=used)
    pieces = [piece for piece in pieces if piece.strip()]
    if len(pieces) < 2:
        # 只切出一段 = 没切开：不算兜底成功，交给调用方硬切
        return None
    # **总闸**：DP 用前缀和判可行性（计数不可加）→ 这里按真实计数收口，保证每块都不超预算
    pieces = enforce_budget(pieces, target_tokens, count)
    # 校验"内容一字不差、顺序不变"（**忽略空白**：各段 strip 掉首尾空白是允许的，
    # 但少字/多字/乱序一律放弃——引用跳转要靠每段能在原文里定位）。
    if compact_ws("".join(pieces)) != compact_ws(text):
        logger.warning("语义兜底放弃：切分结果拼不回原文")
        return None
    logger.debug("语义兜底用了这些级别：{}", sorted(set(used)))
    return pieces
