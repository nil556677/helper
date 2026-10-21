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

    - :attr:`CHUNKING` —— 影响**切出来的块边界**（语义兜底开关、标题前缀开关）。
      它进**两个**指纹：块边界变了，词法与向量都得重来。
      **注意**：parent/child 的**尺寸与重叠不在这里配**——它们由嵌入宽度推导，
      见 ``app/core/features/rag/splitter/derived.py``。
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

#: ``index_meta`` 里记录的**动态字段提取版本**（不是参数，是代码常量；见 app/core/attrs/version.py）。
#: 改字段字典/提取器/归一化 → 版本变 → 界面提示"回填动态字段"（不重嵌入）。
KEY_ATTRS_VERSION = "attrs.version"

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
        secret: 是不是凭证类参数。``True`` 时**落库前用 DPAPI 加密**（与 LLM key 一致），
            读取时自动解密；界面上应当只回显尾号。
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
    secret: bool = False

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
    secret: bool = False,
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
        secret=secret,
    )


#: 进两个指纹的简写（切片参数用）。
_BOTH = (Aspect.VECTOR, Aspect.LEXICAL)
#: 只进向量指纹。
_VEC = (Aspect.VECTOR,)


#: **切片**（只保留"开关"；尺寸与重叠已改为**由嵌入宽度推导**，不再是配置项）。
#:
#: 为什么尺寸不在这里配：parent/child 的尺寸与重叠不是"用户偏好"，而是由向量模型
#: 决定的——子块必须塞得进模型的固定输入宽度（``embed.model.PAD_TO``），其余都是围绕它的固定比例。
#: 让用户手填会出现"``child_size > pad_to`` → 尾部静默截断"这类静默错误。
#: 推导处（唯一）：``app/core/features/rag/splitter/derived.py``。
#:
#: 保留下来的两个开关都**影响块边界**，所以进两个指纹：只改一个开关，块就变了，
#: 词法与向量都得重来。
CHUNKING: tuple[Param, ...] = (
    _p(
        "chunk.semantic_fallback",
        "无结构文档启用语义兜底",
        True,
        Aspect.CHUNKING,
        into=_BOTH,
        cast=bool,
        help_text="没有标题树的文档里、连标点都不够断句的长段落，用嵌入算相邻语义骤降处断开"
        "（自适应百分位，不用固定阈值）。会临时加载嵌入模型；加载失败就退回硬切。"
        "块边界会变，所以进两个指纹。",
    ),
    _p(
        "chunk.prepend_section",
        "嵌入时带上章节标题",
        True,
        Aspect.VECTOR,
        # **进两个指纹**（选项 1B）：标题开销算进 child 预算 → 开关一改，块的边界就变了，
        # 所以既要重切（词法）也要重算向量。切分是秒级的，重算向量才贵。
        into=_BOTH,
        cast=bool,
        help_text="嵌入/精排的**输入**前面拼上最内层章节标题（正文与引用仍是原文）。"
        "解决「向量看不见标题」：正文没写「违约金」三个字、但挂在那一节下的块，"
        "之前只有词法能召回。标题开销会占用 child 预算，所以它进两个指纹。",
    ),
)

#: **向量**（P0.2 的实装口径 —— 形状变了就等于换语义，所以这些必须进指纹）。
#:
#: ⚠ 这里只配 ``embed.dims``（**输出维度**）。**输入宽度 ``pad_to`` 不在这里配**——
#: 它是**模型自己的固定常量**（``app/core/embed/model.py:PAD_TO``），由嵌入实现决定：
#: 每段文本先按 token 填/截到那个宽度再喂模型，超出尾部会被截掉。之所以不让用户改：
#: 实测模型输出会随输入宽度漂移（宽度差 1 token → 向量最大差 1.4%、余弦 0.992），
#: 它属于"模型形状"而不是"偏好"，改了等于换了一个向量空间。
#: 子块尺寸也由它推导（见 ``app/core/features/rag/splitter/derived.py``），
#: 所以它仍会通过 :func:`app.core.config.store.aspect_extra` 进两个指纹。
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
        help_text=(
            "【输出维度】向量的长度（bge-m3 为 768）：取前 N 维后再归一。"
            "与「嵌入固定宽度」无关——那个管输入能塞多少 token；这个管输出是几维。"
            "改它必须全量重建。"
        ),
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

#: **词法**：全局领域词已改为存 ``token_dict`` 表（后台 CRUD 见
#: :mod:`app.core.retrieval.terms`），不再有"词库文件路径"这类可调参数。
#: 留空元组是为了保持 ``Aspect.LEXICAL`` 与指纹分区的结构不变：词条内容哈希由
#: ``config.store.aspect_extra()`` 作为额外项注入词法指纹。
LEXICAL: tuple[Param, ...] = ()

#: **存储**（连接方式不进指纹：向量内容不变，只是存在哪）。
#: 注意：``vector.backend`` 已删除——向量层重构后只有 Qdrant 一个后端（见 `app/core/vector/api.py`），
#: 那个配置项已经没有读者，留着只会让人以为还有 matrix-only 模式。
STORAGE: tuple[Param, ...] = (
    _p(
        "vector.url",
        "Qdrant 服务地址",
        "",
        Aspect.VECTOR,
        help_text="留空＝本地嵌入式（path=data/qdrant）；填如 http://127.0.0.1:6333＝连服务端（HNSW 生效）。",
        cast=str,
    ),
    _p(
        "vector.api_key",
        "Qdrant API Key",
        "",
        Aspect.VECTOR,
        help_text="连服务端需要鉴权时填写；本地模式忽略。**落库前加密**（DPAPI），界面上只回显尾号。",
        cast=str,
        secret=True,
    ),
    _p(
        "vector.timeout",
        "Qdrant 连接超时(秒)",
        30.0,
        Aspect.VECTOR,
        help_text="连服务端时的请求超时；本地模式忽略。",
        cast=float,
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
        # 允许用 dict 构造：补齐缺失项 + 逐个 coerce，越界的夹回范围。
        # **默认值也要过一遍 coerce**：否则 `Config()`（values 就是默认值字典，会被全量 coerce）
        # 与 `Config(values={某一项})`（其余项保持未 coerce 的原值）会得到**类型不同**的同一项
        # ——布尔默认 True 一边是 1、一边是 True，指纹随构造方式漂移（实测踩过：
        # 两个等价配置算出不同指纹 → 界面莫名提示"要重建全库"）。
        merged = defaults()
        for key, param in BY_KEY.items():
            merged[key] = param.coerce(merged[key])
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
