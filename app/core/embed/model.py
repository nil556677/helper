"""嵌入模型（bge-m3，ONNX，**单例 + 惰性加载 + 线程安全**）。

三件事在这里定死，理由都来自实测（技术方案 §7 / §9.9）：

1. **惰性单例**：加载一次实测 1.8 s、常驻 542 MB。启动时加载会让"打开窗口"白等两秒，
   而用户可能整场都不问一句。所以第一次向量化时才加载，加载时给界面一个钩子
   （「正在加载模型…」，技术方案 §12 的"别让用户猜"）。
2. **``intra_op_num_threads = 6``**：本机 12 逻辑核上的实测——2/4/6/8/14 线程的中位耗时是
   825 / 747 / **482** / 1214 / 2526 ms。**超过 6 就开始互相抢核，14 线程是灾难**（慢 5 倍）。
   所以线程数是写死的常量，不是"越大越快"。
3. **进程内一把锁**：任务系统的 cpu 泳道已经是 1，但界面线程、检索路径也可能来调它。
   onnxruntime 的 ``run()`` 本身不是可重入的，而"两个线程同时喂 6 线程的 session"
   会让实际线程数翻倍、直接掉进上面那个坑。所以这里串行化。
4. **固定填充宽度（:data:`PAD_TO`＝320）**：这份 int8 模型的输出**会随输入宽度漂移**——
   实测同一句话喂成宽度 20 与 21，向量最大差 1.4%、余弦 0.992。按批内最长对齐的话，
   同一段文本"入库时批量算"与"检索时单独算"就会得到不同向量，分数随之飘。
   填到固定宽度后，宽度这条变量被消掉了。代价是短文本也按 320 算，见 :data:`PAD_TO`。
5. **一批至少 :data:`MIN_BATCH_ROWS`（＝2）行**：宽度只是第一个变量。实测**行数**也会影响结果，
   而且不是渐变——**1 行与 2 行差 ~2%（余弦 0.978），2 行到 32 行之间则完全一致**。
   1 行 是特殊形状（走另一条量化路径），而"单条查询"正好是 1 行。
   所以：**每批都补到至少 2 行**，查询与语料这才落在同一个形状区间里。
   不补的后果是实测过的：8 条资料 ×4 个查询，"1 行算的查询"与"2 行算的语料"比，
   4 个查询里 **2 个** top-5 顺序不同。

   **可复现性的准确口径**（别记成"绝对确定"）：**同一批组成 ⇒ 逐位相同**。
   实证见 `tests/unit/test_embed_model.py`：同一段文本、同一批文本，重复编码逐位相同；
   而"3 条原样"与"3 条补到 16 行"、或"16 行批里的一条"与"2 行批里的一条"**会不一样**
   （补的行内容变了，量化尺度就变了）。所以不能承诺"任何批次组成下都一样"——
   能承诺的是：**入库路径（固定 16 行批、末尾补齐）与查询路径（2 行）各自稳定**，
   而这两条路径彼此也一致（都是 ≥2 行的同一区间）。

**不做空闲卸载**（§9.2 明确不做）：卸载后再用时又要等 1.8 s，而内存换来的稳定性更值。
"""

from __future__ import annotations

import threading
import time

import numpy as np
from loguru import logger

from app.core import paths
from app.core.embed.dense import EMBED_DIMS, to_dense
from app.core.embed.tokenization import TokenizerError, encode, load
from app.core.models import files as model_files
from app.core.models.onnx import LoadedModel, ModelSlot

#: 模型文件名（清单里的键，`--selfcheck` 校验的就是它）。
MODEL_NAME = "bge-m3.int8.onnx"

#: 推理线程数。**不要"顺手调大"**：实测 8 线程就开始变慢，14 线程慢 5 倍（见模块说明）。
INTRA_OP_THREADS = 6

#: **固定填充宽度（token）**，也是单条上限。为什么必须有它——见下面「宽度」一段。
PAD_TO = 320

#: 一次前向最多喂多少条（入库路径）。**这个数字是"结果可复现"的一部分**：
#: 改成别的值会让已有语料的向量与新算的不一致（等同换了模型，要全量重建）。
DEFAULT_BATCH_SIZE = 16

#: **进程级前向锁**（模块说明第 3 条）：onnxruntime 的 ``run()`` 虽可被多线程调用，但每次
#: 都会各自开一份 intra-op 线程池；两个线程同时跑就是 6→12 条线程抢 12 个核，正好落进
#: "超过 6 线程反而更慢"的实测区间。任务系统的资源锁只串行化"任务"，界面查询走事件循环
#: 线程、不受它管——所以这里必须自己串行化。
_RUN_LOCK = threading.Lock()

#: 一批**至少**几行。1 行是个特殊形状：实测它会走到模型里另一条量化路径，
#: 结果与 ≥2 行差 ~2%（而且会翻排序）。所以连单条查询也按 2 行喂（补一个空行），
#: 让它与语料落在同一个形状区间里。见模块说明第 5 条。
MIN_BATCH_ROWS = 2

#: 宽度 320 的由来（实测，本机 12 逻辑核）：
#: - 技术方案 §7 定的是 child 块 300 token / parent 1500 token。**固定宽度必须 ≥ child 上限**，
#:   否则"入库时算的向量"与"原文能表达的内容"对不上（截断掉的内容等于没入向量）。
#:   320 > 300，正好留一点余量，所以 300 token 的块不会被截。
#: - 单条查询宽度 320 实测 ≈240 ms、16 条一批 ≈3.8 s（238 ms/块）→ 10 万块约 6.6 小时，
#:   与交付计划估的 5.5 小时同档（计划那条是另一台机器的数）。
#: - 512（模型上限）单条就要 ≈0.9~1.3 s，10 万块约 12 小时，明显不值。
#: 想省时间就调小（会截断更长的块），想更保真就调大——两边都会如实反映在指纹里。


class EmbedError(RuntimeError):
    """嵌入不可用（模型缺失 / 加载失败 / 输入为空）。"""


class _Slot(ModelSlot):
    """嵌入模型的单例槽（加载逻辑见 `_build`，生命周期公共的在 `ModelSlot`）。"""

    def _build(self) -> LoadedModel:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - 依赖缺失时才走到
            raise EmbedError("没装 onnxruntime，装完再启动（见 requirements.lock.txt）") from exc

        path = paths.models_dir() / MODEL_NAME
        if not path.is_file():
            raise EmbedError(
                f"缺少嵌入模型：{path}（去哪下见 models/README.md，或用 --selfcheck 看完整性）"
            )
        # 完整性校验：大小对不上就是截断/下错文件，宁可现在报"模型有问题"，
        # 也别在几万次前向之后才发现算出来的是垃圾。哈希比对放在这一步（只做一次）。
        issues = model_files.problems(MODEL_NAME, deep=True)
        if issues:
            raise EmbedError("嵌入模型不可用：" + "；".join(issues))

        options = ort.SessionOptions()
        options.intra_op_num_threads = INTRA_OP_THREADS
        # 只允许一条算子并行链：批内多条样本由 intra_op 并行，inter_op 再并行会重复占核。
        options.inter_op_num_threads = 1
        # 模型自身的告警（int8 量化图里常见）不进我们的日志：用户看不懂，也没有可行动作。
        options.log_severity_level = 3

        started = time.perf_counter()
        try:
            session = ort.InferenceSession(
                str(path), sess_options=options, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # onnxruntime 的异常类型不稳定，统一成人话
            raise EmbedError(f"嵌入模型加载失败（{path.name}）：{exc}") from exc

        try:
            tokenizer = load(paths.models_dir() / "tokenizer.json")
        except TokenizerError as exc:
            raise EmbedError(str(exc)) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        # 首次加载约 1.8 s（清单里记的是这台机器的实测值），超过 5 s 说明磁盘在忙或文件被换过
        logger.info(
            "嵌入模型就绪：{}（{:.0f} ms，{} 线程）", MODEL_NAME, elapsed_ms, INTRA_OP_THREADS
        )
        return LoadedModel(session, tokenizer)


_slot = _Slot()


def is_loaded() -> bool:
    """模型是否已经加载（界面据此决定要不要显示「正在加载模型…」）。"""
    return _slot.is_loaded


def set_load_hook(hook: object) -> None:
    """注册"即将开始加载"的钩子（加载在锁内、只调一次；传 None 取消）。"""
    _slot.set_load_hook(hook)


def unload() -> None:
    """卸掉模型（**只给测试用**：生产路径不卸载，见模块说明）。"""
    _slot.unload()


def _ensure_loaded() -> tuple[object, object]:
    """取（必要时加载）单例；返回 ``(session, tokenizer)``。"""
    loaded = _slot.ensure()
    return loaded.session, loaded.tokenizer


def embed_texts(
    texts: list[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = PAD_TO,
    pad_to: int = PAD_TO,
) -> np.ndarray:
    """把一批文本编码成 768 维单位向量。

    参数：
        texts: 文本列表。空列表 → 返回 ``(0, 768)``（调用方不用特判）。
        batch_size: 每次前向喂多少条。
        max_tokens: 单条上限（默认与 :data:`PAD_TO` 相同）。
        pad_to: 固定填充宽度。**默认值就是正式口径**；只有基准脚本才会传别的值。
    返回：``(len(texts), 768)`` 的 float32，**每行范数 1.0±1e-3**。
    异常：
        EmbedError: 模型不可用，或某条前向失败。
        ValueError: ``max_tokens > pad_to``（那个组合会算出自相矛盾的向量，见 tokenization）。
    """
    if not texts:
        return np.zeros((0, EMBED_DIMS), dtype=np.float32)

    session, tokenizer = _ensure_loaded()
    input_names = [item.name for item in session.get_inputs()]  # type: ignore[attr-defined]
    # 按名字找，不按下标：模型换一个导出顺序就不该崩
    has_mask = "attention_mask" in input_names

    chunks: list[np.ndarray] = []
    step = max(1, batch_size)
    for start in range(0, len(texts), step):
        window = texts[start : start + step]
        input_ids, attention = encode(
            tokenizer, window, max_tokens=max_tokens, pad_to=pad_to
        )
        # **最后一批补到至少 MIN_BATCH_ROWS 行**（空文本 = attention 全 0，不参与计算）：
        # 1 行是特殊形状，算出来与 ≥2 行差 ~2%，而单条查询正好是 1 行。
        # 补成 2 行之后"单条查询"和"批量入库"就是同一个形状区间，跨批可比才成立。
        # **不要顺手改成"补满 16 行"**：实测那会让每次查询从 ~300 ms 变成 ~4 s，
        # 而且补的行内容一变结果还是会变（补 16 行并不比补 2 行更稳）。
        target_rows = max(input_ids.shape[0], min(step, MIN_BATCH_ROWS))
        missing = target_rows - input_ids.shape[0]
        if missing > 0:
            pad_ids, pad_mask = encode(
                tokenizer, [""] * missing, max_tokens=max_tokens, pad_to=pad_to
            )
            input_ids = np.concatenate([input_ids, pad_ids], axis=0)
            attention = np.concatenate([attention, pad_mask], axis=0)

        feeds = {input_names[0]: input_ids}
        if has_mask:
            feeds["attention_mask"] = attention
        try:
            # 串行化前向（见 _RUN_LOCK 的说明）；锁只包 run()，不包分词与后处理
            with _RUN_LOCK:
                outputs = session.run(None, feeds)  # type: ignore[attr-defined]
        except Exception as exc:  # onnxruntime 异常类型不稳定
            raise EmbedError(f"嵌入前向失败（第 {start} 条起，共 {len(window)} 条）：{exc}") from exc
        # 只取真实那几行，补行丢掉
        chunks.append(to_dense(outputs[0])[: len(window)])

    return np.concatenate(chunks, axis=0) if len(chunks) > 1 else chunks[0]


def embed_text(text: str) -> np.ndarray:
    """单条文本的向量（形状 ``(768,)``）。

    **实现上就是"16 行批里的第 0 行"**（与 :func:`embed_texts` 完全同一条路径），
    所以同一文本用它算和在批量里算**逐位相同**——这条性质是刻意的：
    查询与语料必须落在同一个向量空间里，否则排序会变（见模块说明第 5 条）。
    """
    matrix = embed_texts([text])
    return np.asarray(matrix[0], dtype=np.float32)


def embed_for_comparison(texts: list[str], *, same_shape_as: str = "") -> np.ndarray:
    """编码**要与已有语料比较**的文本，把"必须同形状"这条约束变成显式参数。

    为什么需要它：向量只有在**同一次前向的形状**下才逐位可比（见模块说明第 4、5 条）。
    在检索之外还有几处"找相似的"（P2.9 行内相似、记忆召回），调用方很容易顺手用
    :func:`embed_text` 去编一批语料，然后在"1 行形状"与"16 行形状"之间比分数——
    实测那样会翻 top-5 顺序。这个函数只做一件事：把"我要和谁比"写进调用点。

    参数：
        texts: 要编码的文本。
        same_shape_as: 一个**说明性**标签（进日志与指纹，便于排查"这批向量是谁算的"）。
            留空表示"就是给检索用的语料/查询"（走标准 :func:`embed_texts`）。
    返回：``(len(texts), 768)``。
    """
    if same_shape_as:
        from loguru import logger

        logger.debug(
            "按与 {} 相同的形状编码 {} 条文本（{} 行批）",
            same_shape_as,
            len(texts),
            DEFAULT_BATCH_SIZE,
        )
    return embed_texts(texts)
