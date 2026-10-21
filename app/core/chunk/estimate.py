"""token 计数：**真分词器优先、字符估算兜底**。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
切分里所有"预算"判断都要数 token（见 ``core.Splitter``），而 ``child_size`` /
``parent_size`` 的定义单位就是 bge-m3 的 token（见 ``derived``），
所以计数口径直接决定块会不会超出嵌入宽度。

## 输入 / 输出

- 输入：一段文本。
- 输出：token 数（``int``，**至少 1**），或"数 token 的函数"（``Callable[[str], int]``）。
  拿不到真分词器时 :func:`real_token_counter` 返回 ``None``，
  由 :class:`Splitter` 退回 :func:`estimate_tokens`。

## 关键约定

1. **真分词器优先**：``tokenizer.json`` 只有 17 MB、不需要加载 ONNX 会话，代价可忽略；
   字符估算在中文上系统性偏小（估算 300 可能实际 330 > ``PAD_TO`` → 尾部静默截断）；
2. **按路径缓存**：见 :func:`real_token_counter`（测试会把模型目录重定向到临时目录）；
3. 失败只告警、**绝不让导入失败**——切分是导入链路上的必经步骤，缺分词器不该阻断整份文档。
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from ._shared import TOKENS_PER_CHAR


def estimate_tokens(text: str) -> int:
    """按字符数估算 token（**兜底口径**；拿得到真分词器就别用这个）。

    参数：
        text: 任意文本（空串也可以）。
    返回：``len(text) × TOKENS_PER_CHAR`` 向下取整，且**至少为 1**。
        下限 1 是为了让"非空但极短"的块也占一个预算单位，
        避免后续用 0 做除法/把预算算成无限。
    说明：中文按 0.69 token/字符只是经验值，会系统性偏小；
        它只用于"模型/分词器不可用"的降级路径，不是准确计数。
    """
    return max(1, int(len(text) * TOKENS_PER_CHAR))


#: 真分词器的缓存（**按路径缓存**，不是"整个进程只试一次"）
_REAL_COUNTERS: dict[str, Callable[[str], int] | None] = {}


def real_token_counter() -> Callable[[str], int] | None:
    """尝试用 bge-m3 的 ``tokenizer.json`` 量**真 token**；拿不到返回 None。

    为什么值得：`child_size` 的定义单位就是 bge-m3 的 token，
    用字符估算会有偏差（估算 300 可能实际 330 > PAD_TO 320 → 尾部静默截断）。
    tokenizer.json 只有 17 MB、不需要加载 ONNX 会话，代价可以忽略。

    **按路径缓存**（而不是"进程内只试一次"）：测试里会把安装目录重定向到临时目录，
    一次性缓存会被"临时目录没有分词器"这件事**永久污染**（实测：真分词器测试变成 skip）；
    按路径缓存既避免了这个问题，也让生产环境"模型目录换了"能重新解析。

    参数：
        无。
    返回：
        数 token 的函数 ``Callable[[str], int]``；拿不到分词器时返回 ``None``。
    缓存：
        同一路径只解析一次；**失败结果（None）也会缓存**，避免每次切分都重试一遍
        加载/解析这个 17 MB 文件。
    """
    try:
        from app.core.models import files as model_files

        path = model_files.path_of("tokenizer.json")
    except Exception as exc:  # 路径都取不到（极端情况）：退回估算
        logger.warning("拿不到分词器路径，切分预算改用字符估算（×0.69）：{}", exc)
        return None
    key = str(path)
    if key in _REAL_COUNTERS:
        return _REAL_COUNTERS[key]

    counter: Callable[[str], int] | None = None
    try:
        from app.core.embed import tokenization

        tokenizer = tokenization.load(path)

        def count(text: str) -> int:
            """数一段文本的真 token（空串也按 1 计，与字符估算同口径）。"""
            return max(1, len(tokenizer.encode(text).ids))

        counter = count
    except Exception as exc:  # 缺分词器不该阻断导入——退回字符估算并告警
        logger.warning("拿不到 bge-m3 分词器，切分预算改用字符估算（×0.69）：{}", exc)
        counter = None
    _REAL_COUNTERS[key] = counter
    return counter
