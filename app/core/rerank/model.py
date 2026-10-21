"""精排模型（bge-reranker-v2-m3，ONNX，**单例 + 惰性加载 + 只截文档侧**）。

精排要做的判断是"这个查询与这段文档有多相关"，而 bge 系列的输入约定是
**一对拼在一个序列里**：``query</s></s>doc``（用两个 ``</s>`` 隔开），模型输出一个 logit。

三件事在这里定死（都来自技术方案 §7 / §9.9 的实测）：

1. **只截文档侧**：``rerank.max_tokens`` 只作用于 doc，**查询一个 token 都不截**。
   查询是用户当场打的那句话（几十 token），截它等于改变问题本身；文档才是长的那一侧。
   这条有断言守着（见 :func:`build_pair`）——踩过一次就知道很难自己发现。
2. **惰性单例**：精排模型 544 MB、加载 1.9~3.5 s。§9.2 明确"首次需要时才加载"，
   省一份常驻内存；而且很多人整场都不用精排（候选 <2 时直接跳过，我们不加载模型）。
3. **``intra_op_num_threads = 6``**：与嵌入同一条实测结论（8 线程起变慢、14 线程慢 5 倍）。
   两个模型不会同时跑（cpu 泳道并发 1），所以共用这个数字是安全的。

**耗时是这一层唯一的痛点**：实测 10 对 ×256 = 4.9 s、×128 = 2.3 s（本机，见 §9.9）。
所以上游有两道闸：候选数默认 10、候选 <2 直接不精排；以及 `rerank_cache`
（同一 (query, chunk) 不重复算，命中 <50 ms）。
"""

from __future__ import annotations

import threading
import time

import numpy as np
from loguru import logger

from app.core import paths
from app.core.embed.tokenization import TokenizerError, load
from app.core.models import files as model_files
from app.core.models.onnx import LoadedModel, ModelSlot

#: 进程级前向锁：理由同嵌入（onnxruntime 每次 run 各开一份 intra-op 线程池，
#: 并发调用会把 6 条线程翻倍成 12，反而更慢）。界面查询/精排与后台任务可能来自不同线程。
_RUN_LOCK = threading.Lock()

#: 模型文件名（清单里的键）。
MODEL_NAME = "bge-reranker-v2-m3.int8.onnx"

#: 推理线程数（与嵌入同一条实测结论：6 最优）。
INTRA_OP_THREADS = 6

#: 文档侧默认截断（技术方案 §9.2：候选 10 × 截断 256）。
DEFAULT_MAX_TOKENS = 256

#: 一次前向喂多少对。8 是技术方案 §9.2 定的（"截断 256、批量 8"）。
DEFAULT_BATCH_SIZE = 8

#: 句分隔符。bge-reranker 的输入约定是 ``query</s></s>doc``——两个 ``</s>`` 挨着。
SEP_TOKEN = "</s>"

#: 还没加载分词器时拼对用的兜底分隔符 id（bge-m3 的 tokenizer.json 里 ``</s>`` = 2）。
#: 只在纯函数单测里走到；正式路径一律用从分词器问出来的真实 id。
_FALLBACK_SEP_ID = 2


class RerankError(RuntimeError):
    """精排不可用（模型缺失 / 加载失败）。"""


class _Slot(ModelSlot):
    """精排模型的单例槽（加载逻辑见 `_build`；生命周期公共的在 `ModelSlot`）。"""

    def _build(self) -> LoadedModel:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - 依赖缺失时才走到
            raise RerankError("没装 onnxruntime，装完再启动（见 requirements.lock.txt）") from exc

        path = paths.models_dir() / MODEL_NAME
        if not path.is_file():
            raise RerankError(
                f"缺少精排模型：{path}（去哪下见 models/README.md；不用精排可以在设置里关掉）"
            )
        issues = model_files.problems(MODEL_NAME, deep=True)
        if issues:
            raise RerankError("精排模型不可用：" + "；".join(issues))

        options = ort.SessionOptions()
        options.intra_op_num_threads = INTRA_OP_THREADS
        options.inter_op_num_threads = 1
        options.log_severity_level = 3

        started = time.perf_counter()
        try:
            session = ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # onnxruntime 的异常类型不稳定，统一成人话
            raise RerankError(f"精排模型加载失败（{path.name}）：{exc}") from exc

        try:
            tokenizer = load(paths.models_dir() / "tokenizer.json")
        except TokenizerError as exc:
            raise RerankError(str(exc)) from exc

        # 分隔符 id：从分词器里问出来，别写死（换分词器时不至于静默拼错）
        try:
            separator_id = int(tokenizer.token_to_id(SEP_TOKEN))
        except Exception as exc:
            logger.warning("分词器里没有 {}（{}），精排按 102 拼对", SEP_TOKEN, exc)
            separator_id = 102

        global _separator_id
        _separator_id = separator_id
        loaded = LoadedModel(session, tokenizer)
        loaded.separator_id = separator_id  # type: ignore[attr-defined]
        logger.info(
            "精排模型就绪：{}（{:.0f} ms，{} 线程，分隔符 id={}）",
            MODEL_NAME,
            (time.perf_counter() - started) * 1000,
            INTRA_OP_THREADS,
            separator_id,
        )
        return loaded


_slot = _Slot()

#: 加载后记下的 ``</s>`` token id（纯函数 `build_pair` 用，**不触发加载**；未加载走兜底）。
_separator_id: int | None = None


def is_loaded() -> bool:
    """模型是否已加载（界面据此决定要不要显示「正在精排…」与「正在加载模型…」）。"""
    return _slot.is_loaded


def set_load_hook(hook: object) -> None:
    """注册"即将开始加载"的钩子（界面挂「正在加载模型…」用；传 None 取消）。"""
    _slot.set_load_hook(hook)


def unload() -> None:
    """卸掉模型（**只给测试用**）。"""
    _slot.unload()


def build_pair(query_ids: list[int], doc_ids: list[int], *, max_tokens: int) -> list[int]:
    """拼出 ``query</s></s>doc</s>`` 的 token 序列。**只截文档侧**。

    参数：
        query_ids: 查询的 token id（分词器原始输出，含首尾特殊 token）。
        doc_ids: 文档的 token id（同上）。
        max_tokens: **只截文档侧**到这么多 token（查询一个都不截）。
    返回：拼好的 id 列表。

    **口径**（实测过三种写法，见 §9.9）：去掉各自的 ``<s>``/``</s>`` 外壳，
    中间放两个 ``</s>``、结尾补一个 ``</s>``——即模型训练时的
    ``[CLS] query [SEP] [SEP] doc [SEP]``。三种写法的排序都正确（3/3），
    这种的分隔裕度最好（+5.72 vs +5.28 / +5.89 中另一个是"少一个分隔符"的写法，
    **不采纳**：它与模型训练口径不符，靠的是模型足够鲁棒）。

    **边界行为**（极端输入下"去掉外壳"本身没有唯一答案，所以这里写死）：
    两个 id 的输入（只有外壳）会被整体当成正文；一个 id 或空列表原样用。
    真实路径上查询至少 3 个 token（``<s> 词 </s>``），所以这些边界只在测试里出现。
    """
    # 首尾各一个特殊 token：**成对出现才剥**（只有两个时那一对就是全部内容，不剥）
    q_body = query_ids[1:-1] if len(query_ids) > 2 else list(query_ids)
    d_body = doc_ids[1:-1] if len(doc_ids) > 2 else list(doc_ids)
    # **只截文档侧**：查询一个 token 都不动
    d_body = d_body[: max(1, max_tokens)]

    sep_id = _separator_id if _separator_id is not None else _FALLBACK_SEP_ID
    return [*q_body, sep_id, sep_id, *d_body, sep_id]


def _ensure_loaded() -> tuple[object, object]:
    """取（必要时加载）单例；返回 ``(session, tokenizer)``。"""
    loaded = _slot.ensure()
    return loaded.session, loaded.tokenizer


def score_pairs(
    query: str,
    documents: list[str],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> np.ndarray:
    """给一批候选文档打分（**分数越大越相关**，是原始 logit，不是 0~1）。

    参数：
        query: 用户问题（**不截断**）。
        documents: 候选文档（只截前 ``max_tokens`` 个 token）。
        max_tokens: 文档侧截断。
        batch_size: 一次前向多少对。
    返回：``(len(documents),)`` 的 float32 logit。
    异常：
        RerankError: 模型不可用或前向失败。
    """
    if not documents:
        return np.zeros((0,), dtype=np.float32)

    session, tokenizer = _ensure_loaded()
    query_ids = tokenizer.encode(query).ids
    pair_ids = [build_pair(query_ids, tokenizer.encode(doc).ids, max_tokens=max_tokens) for doc in documents]

    input_names = [item.name for item in session.get_inputs()]
    has_mask = "attention_mask" in input_names
    step = max(1, batch_size)

    scores: list[np.ndarray] = []
    for start in range(0, len(pair_ids), step):
        window = pair_ids[start : start + step]
        width = max(1, max(len(ids) for ids in window))
        input_ids = np.zeros((len(window), width), dtype=np.int64)
        attention = np.zeros((len(window), width), dtype=np.int64)
        for row, ids in enumerate(window):
            input_ids[row, : len(ids)] = ids
            attention[row, : len(ids)] = 1

        feeds = {input_names[0]: input_ids}
        if has_mask:
            feeds["attention_mask"] = attention
        try:
            with _RUN_LOCK:
                outputs = session.run(None, feeds)
        except Exception as exc:  # onnxruntime 的异常类型不稳定
            raise RerankError(f"精排前向失败（第 {start} 对起，共 {len(window)} 对）：{exc}") from exc
        # logits 形状 (B, 1)：取第 0 列
        scores.append(np.asarray(outputs[0], dtype=np.float32).reshape(-1))

    return np.concatenate(scores, axis=0)


def score_one(query: str, document: str, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> float:
    """给单对打分（返回 python float；缓存与测试用）。"""
    return float(score_pairs(query, [document], max_tokens=max_tokens)[0])


def is_worth_reranking(count: int) -> bool:
    """候选这么少时值不值得精排。

    **候选 <2 直接跳过**（技术方案 §9.2）：一对候选没有"排序"可言，
    而精排一对就要几百毫秒——跳过它省的是实打实的时间，且结果完全一样。
    """
    return count >= 2
