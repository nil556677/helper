"""聊天底栏的统计口径（**UI-free**，测试可直接调）。

DSH 在输入区下面有两条 composer 统计（轮次/速度 + token/缓存命中，见
dsh-client-ui-chat 的 ``conversation.composer.dock``）。本应用不接生成式 LLM，
按自己的事实口径显示：**会话统计**（轮数/引用条数）与**上下文占用**
（把当前会话的用户问题、回答、引用证据各自估成 token，对比参考窗口给出
占用比例与各部分占比）。

**为什么是"估算"**：还没有真实的 LLM 槽位，也就没有它的分词器与上下文窗口。
这里用启发式（CJK/全角 ≈ 1 token/字，其余 ≈ 4 字符/token），口径写死在界面上
（"按 1M 窗口估算"，与上下文层 `contexts.budget.WINDOW` 同源）；将来接真实模型时，
把 :data:`CONTEXT_LIMIT` 换成槽位配置的窗口、把 :func:`estimate_tokens` 换成本地
tokenizer 即可，界面不用改。
"""

from __future__ import annotations

import math

#: 参考上下文窗口（token）。与上下文层的 `contexts.budget.WINDOW` 对齐（写死 1M）。
#: 路径：这里只是"分母"，不参与任何分支；改它只影响显示的占用百分比。
CONTEXT_LIMIT = 1_000_000

#: 按 1 token/字 估算的字符区段：CJK 部首~统一表意、兼容表意、全角形式、CJK 标点。
#: 路径：:func:`estimate_tokens` 用它判断"这个字符算 1 token 还是算 1/4 token"。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x9FFF),  # CJK 部首扩展 ~ 统一表意文字（绝大多数汉字）
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0xFF00, 0xFFEF),  # 全角/半角形式（全角标点、全角字母数字）
    (0x3000, 0x303F),  # CJK 标点（。，、；：等）
)


def estimate_tokens(text: str) -> int:
    """把一段文本估成 token 数（启发式，非精确）。

    - CJK/全角字符 ≈ 1 token/字；
    - 其余（拉丁字母、数字、空白等）≈ 4 字符/token，向上取整。
    """
    # 路径 1：空文本 / None → 0，避免下面做 len 与除法时还要特判。
    if not text:
        return 0
    # 路径 2：逐个字符判断是否落在 CJK/全角区段；是则按 1 token/字计。
    cjk = sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))
    # 路径 3：剩下的按 4 字符/token 估；ceil 保证"零头"也占 1 token（宁可高估）。
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def format_tokens(count: int) -> str:
    """千位缩写：``999 → "999"``、``1234 → "1.2k"``、``128000 → "128k"``。"""
    # 路径 1：不足 1000 → 原样显示，不加 k（避免 "0.9k" 这种反直觉写法）。
    if count < 1000:
        return str(count)
    # 路径 2：>= 1000 → 保留一位小数（1234 → "1.2k"）。
    text = f"{count / 1000:.1f}"
    # 路径 3：小数位恰好是 .0 时去掉（128000 → "128.0k" 要变成 "128k"）。
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}k"


def context_share(count: int) -> float:
    """token 数占参考窗口的比例（0~1，可能超过 1）。"""
    # 路径：正常返回比例；>1 不截断，由调用方决定怎么显示（底栏会按 % 展示）。
    return count / CONTEXT_LIMIT
