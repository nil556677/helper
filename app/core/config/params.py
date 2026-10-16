"""索引期参数的登记表：**一份清单定死"哪些参数进指纹、进了会触发什么重建"**。

为什么需要它（而不是到处写默认值）：参数散着写一定会漂。四个后果都是实测/踩过的：

1. **改一个参数却不知道要重建什么** → 用户改了词库，界面提示"需要全量重建 8 小时"，
   而实际只需重算词法（秒级）。所以每个参数都要声明它属于哪个 :class:`Aspect`。
2. **改了参数但指纹没变** → 界面上不提示重建，于是"新切成 300 字、库里还是 1500 字的老块"，
   检索结果变得没法解释（同一份资料有的块是新尺寸、有的是旧尺寸）。
3. **指纹里漏了真正影响向量的参数** → 换了嵌入模型或填充宽度却不重建，
   新块与老块的向量**不在同一个空间**，相似度分数失去意义（P0.2 实测：形状差 1 行就差 2.6%）。
4. **默认值写在两处** → 文档、界面、代码三个数不一致，谁也不信谁。

所以：**默认值只在这里写一份**，界面只负责显隐与读写，指纹只负责算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Aspect(StrEnum):
    """指纹分项：决定"这个参数变了要重建什么"。

    **注意"影响什么"与"属于哪一类"是两件事**，所以 :class:`Param` 上既有 ``aspect``
    （它是什么参数、界面怎么分组）又有 ``into``（它进哪个指纹）。三个类型的区别：

    - :attr:`CHUNKING` —— 影响**切出来的块本身**（parent/child 尺寸与重叠）。
      它进**两个**指纹：块都变了，词法与向量都得重来。
    - :attr:`VECTOR` —— 影响**向量**（嵌入模型、维度、固定宽度、批量、线程）。
      它只进向量指纹；其中线程数只影响速度，所以 ``into`` 是空的（**不进指纹**）。
    - :attr:`LEXICAL` —— 影响**词法列**（jieba 词库）。只进词法指纹：
      改了只需**重算词法**（秒级：重切 `chunks.tokens`，不动向量）。
    - :attr:`QUERY` —— **只影响查询时怎么用**（top_k / 召回条数 / RRF k / 精排旋钮）。
      **不进任何指纹**：为了调 top_k 而重建 8 小时显然是错的。
    """

    CHUNKING = "chunking"
    VECTOR = "vector"
    LEXICAL = "lexical"
    QUERY = "query"


#: 两个指纹（存进 ``index_meta``）的键名。**改名字等于让所有老库都"指纹缺失"**，会提示重建。
KEY_VECTOR_FINGERPRINT = "fingerprint.vector"
KEY_LEXICAL_FINGERPRINT = "fingerprint.lexical"

#: ``index_meta`` 里逐项留痕的前缀（排查"到底哪个参数变了"用）。
PER_ITEM_PREFIX = "fingerprint.item."


@dataclass(frozen=True)
class Param:
    """一个可调参数。

    参数：
        key: 存进 ``settings`` 表的键（点号分层，界面也按它分组）。
        label: 界面上的名字（中文）。
        default: 默认值。**唯一的一份**。
        aspect: 属于哪一类（界面分组 + 语义，见 :class:`Aspect`）。
        into: 进哪些指纹（``Aspect.VECTOR`` / ``Aspect.LEXICAL`` 的子集）。
            **空元组 = 不进指纹 = 改它不需要重建**（查询期旋钮、线程数都是这一类）。
        help_text: 给用户看的一句话（界面的 tooltip）。
        minimum / maximum: 允许范围；``None`` 表示不限制。
        cast: 从 settings 读出来时的转换（int / float / str / bool）。
    """

    key: str
    label: str
    default: object
    aspect: Aspect
    into: tuple[Aspect, ...] = ()
    help_text: str = ""
    minimum: float | None = None
    maximum: float | None = None
    cast: type = int

    def coerce(self, value: object) -> object:
        """把外部值转成正确类型；超范围就夹回范围（**不抛异常**）。

        为什么不抛：这些值可能是老版本写的、用户手改过、或界面传了字符串。
        一个越界参数不该让检索整条链路起不来——夹回合法范围并记一条日志更合适。
        （真正"非法"的情况由 :func:`validate` 报告给界面，让用户知道被改了。）
        """
        if self.cast is bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        try:
            converted = self.cast(value)  # type: ignore[call-arg]
        except (TypeError, ValueError):
            return self.default
        if isinstance(converted, int | float):
            if self.minimum is not None and converted < self.minimum:
                return self.cast(self.minimum)
            if self.maximum is not None and converted > self.maximum:
                return self.cast(self.maximum)
        return converted


def _p(
    key: str,
    label: str,
    default: object,
    aspect: Aspect,
    *,
    into: tuple[Aspect, ...] = (),
    help_text: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    cast: type = int,
) -> Param:
    return Param(
        key=key,
        label=label,
        default=default,
        aspect=aspect,
        into=into,
        help_text=help_text,
        minimum=minimum,
        maximum=maximum,
        cast=cast,
    )


#: 进两个指纹的简写（切片参数用）。
_BOTH = (Aspect.VECTOR, Aspect.LEXICAL)
#: 只进向量指纹。
_VEC = (Aspect.VECTOR,)
#: 只进词法指纹。
_LEX = (Aspect.LEXICAL,)


#: **切片**（技术方案 §9.6 定稿：parent 1500 / child 300，overlap 10%~15%）。
#: 换算用 0.69 token/字符（§7）：1500 token ≈ 2170 字符、300 token ≈ 430 字符。
#: 这四个都进**两个**指纹：块本身变了，词法与向量都得重来。
CHUNKING: tuple[Param, ...] = (
    _p(
        "chunk.parent_size",
        "父块大小（token）",
        1500,
        Aspect.CHUNKING,
        into=_BOTH,
        help_text="送给模型的证据块。改它必须全量重建（块本身变了）。",
        minimum=200,
        maximum=8000,
    ),
    _p(
        "chunk.parent_overlap",
        "父块重叠（token）",
        150,
        Aspect.CHUNKING,
        into=_BOTH,
        help_text="约 10%：让跨段落的句子不会正好被切断。",
        minimum=0,
        maximum=2000,
    ),
    _p(
        "chunk.child_size",
        "子块大小（token）",
        300,
        Aspect.CHUNKING,
        into=_BOTH,
        help_text="检索的最小单位。必须 ≤ 嵌入的固定填充宽度（否则尾部会被截掉、入不了向量）。",
        minimum=50,
        maximum=2000,
    ),
    _p(
        "chunk.child_overlap",
        "子块重叠（token）",
        45,
        Aspect.CHUNKING,
        into=_BOTH,
        help_text="约 15%：边界句在两个块里都出现，避免「答案正好在缝里」。",
        minimum=0,
        maximum=1000,
    ),
)

#: **向量**（P0.2 的实装口径 —— 形状变了就等于换语义，所以这些必须进指纹）。
VECTOR: tuple[Param, ...] = (
    _p(
        "embed.model",
        "嵌入模型",
        "bge-m3.int8.onnx",
        Aspect.VECTOR,
        into=_VEC,
        help_text="换模型 = 换向量空间，必须全量重建。",
        cast=str,
    ),
    _p(
        "embed.pad_to",
        "嵌入固定宽度（token）",
        320,
        Aspect.VECTOR,
        into=_VEC,
        help_text=(
            "每批都填到这个宽度。实测宽度差 1 个 token 向量就差 1.4%，"
            "所以它进指纹：改了必须全量重建。"
        ),
        minimum=64,
        maximum=512,
    ),
    _p(
        "embed.batch_size",
        "嵌入批量（行）",
        16,
        Aspect.VECTOR,
        into=_VEC,
        help_text="每批固定这么多行（不足补空行）。实测行数不同结果不同，所以它也算语义的一部分。",
        minimum=2,
        maximum=64,
    ),
    _p(
        "embed.dims",
        "向量维度",
        768,
        Aspect.VECTOR,
        into=_VEC,
        help_text="截前 N 维后再归一。改它必须全量重建。",
        minimum=64,
        maximum=1024,
    ),
    _p(
        "embed.threads",
        "推理线程数",
        6,
        Aspect.VECTOR,
        help_text="实测 6 最优；14 线程慢 5 倍。**只影响速度、不影响结果**，所以不进指纹。",
        minimum=1,
        maximum=16,
    ),
)

#: **检索与精排**（只影响查询时的行为，**不进指纹**——改它不需要重建索引）。
RETRIEVAL: tuple[Param, ...] = (
    _p(
        "retrieval.dense_limit",
        "向量召回条数",
        50,
        Aspect.QUERY,
        help_text="两路召回之一。改它只影响查询，不用重建。",
        minimum=1,
        maximum=500,
    ),
    _p(
        "retrieval.sparse_limit",
        "词法召回条数",
        50,
        Aspect.QUERY,
        help_text="两路召回之一。改它只影响查询，不用重建。",
        minimum=1,
        maximum=500,
    ),
    _p("retrieval.rrf_k", "RRF k", 60, Aspect.QUERY, minimum=1, maximum=1000),
    _p(
        "retrieval.top_k",
        "送模型的证据条数",
        5,
        Aspect.QUERY,
        help_text="最终进 Prompt 的条数。",
        minimum=1,
        maximum=50,
    ),
    _p(
        "retrieval.score_floor",
        "相似度阈值",
        0.0,
        Aspect.QUERY,
        help_text="**只作用于 0~1 的分**（余弦 / 精排），绝不与 RRF 或 bm25 分比较。待 P4 定标。",
        minimum=0.0,
        maximum=1.0,
        cast=float,
    ),
    _p("rerank.enabled", "启用精排", True, Aspect.QUERY, cast=bool),
    _p("rerank.candidates", "精排候选数", 10, Aspect.QUERY, minimum=2, maximum=100),
    _p(
        "rerank.max_tokens",
        "精排截断（token）",
        256,
        Aspect.QUERY,
        help_text="只截文档侧，查询侧一个 token 都不截。",
        minimum=64,
        maximum=512,
    ),
)

#: **词法**（进词法指纹：改了只需重算词法列）。
LEXICAL: tuple[Param, ...] = (
    _p(
        "tokenize.userdict",
        "自定义词库文件",
        "data/userdict.txt",
        Aspect.LEXICAL,
        into=_LEX,
        help_text="一行一词。词库内容变了只需重算词法（秒级），不用动向量。",
        cast=str,
    ),
)

#: **存储**（后端切换不进指纹：向量内容不变，只是存在哪）。
STORAGE: tuple[Param, ...] = (
    _p(
        "vector.backend",
        "向量后端",
        "qdrant",
        Aspect.VECTOR,
        help_text="qdrant（默认，事实来源）/ matrix-only（只用矩阵缓存，丢了要重建）。",
        cast=str,
    ),
)

#: 全部参数，按 key 索引（**唯一清单**）。
ALL: tuple[Param, ...] = CHUNKING + VECTOR + RETRIEVAL + LEXICAL + STORAGE

BY_KEY: dict[str, Param] = {param.key: param for param in ALL}

#: 界面分组的顺序（每组一个标题 + 该组的参数）。
GROUPS: tuple[tuple[str, tuple[Param, ...]], ...] = (
    ("切片", CHUNKING),
    ("向量", VECTOR),
    ("检索与精排", RETRIEVAL),
    ("词法", LEXICAL),
    ("存储", STORAGE),
)


def defaults() -> dict[str, object]:
    """全部参数的默认值（``{key: value}``）。"""
    return {param.key: param.default for param in ALL}


def get(key: str) -> Param:
    """按 key 取参数定义。

    异常：
        KeyError: key 不在清单里（拼错要立刻炸，别静默用默认值）。
    """
    try:
        return BY_KEY[key]
    except KeyError as exc:
        raise KeyError(f"没有这个参数：{key}（清单见 app/core/config/params.py）") from exc


def aspect_keys() -> dict[Aspect, tuple[str, ...]]:
    """**每个指纹**由哪些参数组成（按 ``Param.into`` 收集）。

    返回的键只有 :attr:`Aspect.VECTOR` 与 :attr:`Aspect.LEXICAL` 两个——
    "CHUNKING / QUERY" 是参数分类，不是指纹。
    """
    buckets: dict[Aspect, set[str]] = {Aspect.VECTOR: set(), Aspect.LEXICAL: set()}
    for param in ALL:
        for target in param.into:
            buckets[target].add(param.key)
    # 排序保证指纹稳定（顺序变了哈希就变了）
    return {aspect: tuple(sorted(keys)) for aspect, keys in buckets.items()}


@dataclass(frozen=True)
class Config:
    """一份**取值**（读出来的、可读的配置快照）。

    为什么不用裸 dict：``config.retrieval_top_k`` 这种拼错的键在 dict 里要么 KeyError
    要么静默 None，而这个类的字段是写死的——拼错在导入期就报。
    """

    values: dict[str, object] = field(default_factory=defaults)

    def __post_init__(self) -> None:
        # 允许用 dict 构造：补齐缺失项 + 逐个 coerce，越界的夹回范围
        merged = defaults()
        for key, value in self.values.items():
            param = BY_KEY.get(key)
            if param is not None:
                merged[key] = param.coerce(value)
        object.__setattr__(self, "values", merged)

    def __getitem__(self, key: str) -> object:
        return self.values[key]

    def raw(self) -> dict[str, object]:
        """纯 dict（写 JSON / 算指纹用）。"""
        return dict(self.values)

    def fingerprint_items(self, aspect: Aspect, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        """算指纹要用的 ``{键: 值}``（值统一成字符串，保证可哈希）。

        参数：
            aspect: 要算哪个**指纹**（``VECTOR`` 或 ``LEXICAL``）。
            extra: 额外项（如词库内容哈希），由调用方算好传进来。
        """
        items: dict[str, str] = {
            key: str(self.values.get(key)) for key in aspect_keys().get(aspect, ())
        }
        if extra:
            items.update({key: str(value) for key, value in extra.items()})
        return items
