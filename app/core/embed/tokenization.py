"""文本 → token id 的薄封装（只依赖 ``tokenizers``，25 万词表单独一个文件就够）。

**为什么不引 transformers**：我们只需要"文本 → input_ids + attention_mask"这一步，
而 ``tokenizer.json`` 自带完整词表与后处理，``tokenizers`` 能直接加载它。
引 transformers 会连带 torch，那是几百 MB 与几秒启动（技术方案 §7 已定）。

**填充宽度必须固定（这条是本模块最容易踩的坑）**：这份 int8 量化模型的输出**会随输入宽度变化**——
同一句话，喂成宽度 20 与宽度 21，向量最大差 1.4%、余弦 0.992（实测，见
`docs/技术方案.md` §9.9）。按批内最长对齐的话，同一段文本在"单独编码"与"和别人一批编码"
时会得到**不同的向量**：入库时批量算、检索时单独算，同一份资料的分数就会飘，
"跨批可比"直接不成立（这正是 keepdims 那条硬约束要防的同一类问题）。

所以正式路径一律用 :func:`encode` 的 ``pad_to``（固定宽度），
**不允许**用"按批内最长对齐"那条路（``pad_to=None`` 只给基准脚本做对照）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from loguru import logger

#: 单条最多多少 token（bge-m3 的上下文上限是 512，我们按固定宽度取 320，见 `model.PAD_TO`）。
DEFAULT_MAX_TOKENS = 512

#: 被截断的条数（进程级累计）。截断=有内容没进向量——属于"数据精准"问题，必须可观测。
_truncated = 0
_truncated_logged_at = 0


def _note_truncation(excess: int) -> None:
    """记一次截断：首条立刻告警，之后每 100 条汇总一次（避免刷屏）。"""
    global _truncated, _truncated_logged_at
    _truncated += 1
    if _truncated == 1 or _truncated - _truncated_logged_at >= 100:
        _truncated_logged_at = _truncated
        logger.warning(
            "有文本超过单条上限、被截断（累计 {} 条，本次超出 {} token）——"
            "超出的内容不会进向量，切分预算应 ≤ 模型固定宽度",
            _truncated,
            excess,
        )


def truncation_count() -> int:
    """累计被截断的条数（测试/诊断用）。"""
    return _truncated


def reset_truncation_count() -> None:
    """清零累计（测试用：避免用例之间互相影响）。"""
    global _truncated, _truncated_logged_at
    _truncated = 0
    _truncated_logged_at = 0


class TokenizerError(RuntimeError):
    """分词器加载失败（文件缺失或格式不对）。"""


def load(path: Path):
    """加载 ``tokenizer.json``。

    参数：
        path: tokenizer.json 的路径。
    返回：``tokenizers.Tokenizer``。
    异常：
        TokenizerError: 文件不在 / 装不了（把第三方异常包成人话，启动路径要能显示）。
    """
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:  # pragma: no cover - 依赖缺失时才走到
        raise TokenizerError("没装 tokenizers，装完再启动（见 requirements.lock.txt）") from exc

    if not Path(path).is_file():
        raise TokenizerError(f"找不到分词器文件：{path}")

    try:
        tokenizer = Tokenizer.from_file(str(path))
    except Exception as exc:  # tokenizers 抛的是 Rust 异常，类型不稳定
        raise TokenizerError(f"分词器文件读不了（{path.name}）：{exc}") from exc

    tokenizer.no_padding()
    tokenizer.no_truncation()
    return tokenizer


def encode(
    tokenizer, texts: list[str], *, max_tokens: int = DEFAULT_MAX_TOKENS, pad_to: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """把若干文本编码成 ``(input_ids, attention_mask)``。

    参数：
        tokenizer: :func:`load` 的返回值。
        texts: 文本列表（空列表返回两个空数组，调用方不用特判）。
        max_tokens: 单条上限；超了从**尾部**截断（保留开头的主题信息）。
        pad_to: 固定填充宽度。给定值时，无论这批文本多长，输出宽度恒为 ``pad_to``。
            **这个参数是"结果可复现"的前提**，见模块与 `model.py` 的说明：这份 int8
            模型的输出会随输入宽度变化（同一句话，宽度 20 与宽度 21 的向量差 1.4%）。
            ``None`` 表示按本批最长对齐（**只用于基准对照，别用在正式入库上**）。
    返回：
        ``(input_ids, attention_mask)``，形状都是 ``(len(texts), L)``，``int64``。
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.int64), np.zeros((0, 0), dtype=np.int64)

    encoded = [tokenizer.encode(text) for text in texts]
    # **截断必须可观测**（P7 审查 H3）：ids[:max_tokens] 是静默丢内容——超出的部分
    # 完全不会进向量、也搜不到。这里逐条记账（首条立刻告警，之后每 100 条汇总）。
    ids_list: list[list[int]] = []
    for item in encoded:
        if len(item.ids) > max_tokens:
            _note_truncation(len(item.ids) - max_tokens)
        ids_list.append(item.ids[:max_tokens])

    if pad_to is not None:
        width = max(int(pad_to), 1)
        # 单条就比 pad_to 还长：说明调用方给的 max_tokens 比 pad_to 大，那个组合是自相矛盾的
        # （会被截断且算出来的向量与"按 pad_to 入库"的不一致），所以直接拒绝。
        if any(len(ids) > width for ids in ids_list):
            raise ValueError(
                f"有文本超过固定宽度 {width} token：max_tokens({max_tokens}) 不能大于 pad_to"
            )
    else:
        width = max(1, max(len(ids) for ids in ids_list))

    batch = len(ids_list)
    input_ids = np.zeros((batch, width), dtype=np.int64)
    attention = np.zeros((batch, width), dtype=np.int64)
    for row, ids in enumerate(ids_list):
        count = len(ids)
        if count:
            input_ids[row, :count] = ids
            attention[row, :count] = 1
    return input_ids, attention
