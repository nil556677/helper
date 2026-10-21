"""性能基线脚本：把「一次性探针」固化成可复跑的门禁。

执行计划 T4.1（原 P0.8）。设计原则：**快速、只读、可手动跑**。

- 只读：完全不写 `app/`，只创建输出文件（默认 `.tmp/bench/`）；
- 优雅降级：某个依赖（onnxruntime / tokenizers / numpy / pymupdf）没装，
  对应项报告 ``n/a（未装 X）``，脚本绝不崩；
- `--quick`：只跑嵌入单条 + 精排默认（10×256），几秒出结果；
- `--json`：全程机器可读，输出一个 JSON 对象；
- 全量跑：把上面几类全测一遍，并把一条汇总行**追加**到
  `docs/技术方案.md §9.9` 的实测表（增量，保留原有行）。

用法：

    .venv\\Scripts\\python.exe scripts\\bench_models.py            # 全量
    .venv\\Scripts\\python.exe scripts\\bench_models.py --quick     # 只跑核心两项
    .venv\\Scripts\\python.exe scripts\\bench_models.py --json       # 机器读
    .venv\\Scripts\\python.exe scripts\\bench_models.py --output-dir .tmp\\bench

退出码：成功 0；缺核心依赖或模型文件导致跑不了 1。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
VOCAB = 250_000  # tokenizer.json 词表（探针实测 25 万）
EMBED_NAME = "bge-m3.int8.onnx"
RERANK_NAME = "bge-reranker-v2-m3.int8.onnx"

# **把仓库根放进 sys.path**：P0 门禁要 import `app.core.embed/rerank/vector`
# （它们才是"实装口径"），而直接 `python scripts/bench_models.py` 时仓库根不在路径里
# ——实测漏了这一步，三项门禁全报 "No module named 'app'"。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 中文样例段（探针沿用），用来构造可控 token 长度的输入
SAMPLE = (
    "第二条 乙方应当在合同签订之日起三十日内交付全部设备，并保证设备为全新原厂正品。"
    "若因乙方原因导致交付延期，每延期一日，乙方应当按照合同总价款的万分之五向甲方支付违约金。"
    "违约金累计不超过合同总价款的百分之十。甲方有权在乙方延期超过十五日时单方解除合同，"
    "并要求乙方赔偿因此造成的实际损失。设备的验收标准按照附件一《技术规格书》执行，"
    "双方应当在设备到场后七个工作日内共同完成开箱验收并签署验收单。"
)

#: 中文字符→token 比例（探针实测 0.69）
CHAR_PER_TOKEN = 0.69

#: numpy 打分测试的几个规模
MATRIX_SIZES = (10_000, 50_000, 100_000)


# --------------------------------------------------------------------------- #
# 依赖探测（优雅降级的核心）
# --------------------------------------------------------------------------- #
def _module(name: str) -> object | None:
    """延迟导入一个模块；失败返回 None（调用方决定怎么降级）。"""
    try:
        return __import__(name)
    except ImportError:
        return None


def _missing(lib: str) -> str:
    return f"n/a（未装 {lib}）"


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    """实测表里的一行：类别 / 项 / 数字。"""

    category: str
    item: str
    value: str

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.category, self.item, self.value)


@dataclass
class BenchResult:
    """一次跑的所有结果。"""

    mode: str
    timestamp: str
    rows: list[Row] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    def add(self, category: str, item: str, value: object) -> None:
        self.rows.append(Row(category, item, str(value)))

    def mark_degraded(self, message: str) -> None:
        self.degraded.append(message)

    def table(self) -> list[tuple[str, str, str]]:
        return [row.as_tuple() for row in self.rows]


def _render_markdown(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| 类别 | 项 | 数字 |", "| --- | --- | --- |"]
    lines += [f"| {c} | {i} | {v} |" for c, i, v in rows]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 计时辅助
# --------------------------------------------------------------------------- #
def _micro(dt: float) -> float:
    return dt * 1_000_000.0


def _milli(dt: float) -> float:
    return dt * 1_000.0


def _best(fn, repeat: int = 3) -> float:
    """跑 repeat 次，取最快的耗时（秒）——排除首次分配与抖动。"""
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - started)
    return best


# --------------------------------------------------------------------------- #
# 各测试项
# --------------------------------------------------------------------------- #
def bench_tokenizer(result: BenchResult, quick: bool) -> None:
    """tokenizer 加载（冷/热）与词表。"""
    tokenizers = _module("tokenizers")
    if tokenizers is None:
        result.add("模型", "tokenizer 加载 / 词表", _missing("tokenizers"))
        result.mark_degraded("tokenizer：未装 tokenizers")
        return
    path = MODELS / "tokenizer.json"
    if not path.is_file():
        result.add("模型", "tokenizer 加载 / 词表", "n/a（缺 tokenizer.json）")
        result.mark_degraded("tokenizer：缺 tokenizer.json")
        return

    def load() -> object:
        return tokenizers.Tokenizer.from_file(str(path))

    cold = time.perf_counter()
    load()
    cold_ms = _milli(time.perf_counter() - cold)
    hot = _best(load, repeat=3)
    result.add("模型", "tokenizer 加载（冷/热）/ 词表", f"{cold_ms:.0f} ms / {_milli(hot):.0f} ms / {VOCAB}")

    # 中文 token 比（Quick 也量，因为便宜）
    if not quick:
        tokenizer = load()
        ids = tokenizer.encode(SAMPLE).ids
        result.add("模型", "中文 token 比", f"{len(ids) / len(SAMPLE):.2f} token/字符")


def _make_text(target_tokens: int) -> str:
    """构造一个「目标 token 数」的中文文本。"""
    ratio = int(target_tokens / CHAR_PER_TOKEN)
    text = SAMPLE
    while len(text) < ratio:
        text += SAMPLE
    return text[:ratio]


def _encode(tokenizer, texts: list[str], limit: int) -> dict[str, object]:
    """编码成 onnx 需要的 input_ids / attention_mask（定长到 limit）。"""
    try:
        encoded = tokenizer.encode_batch(texts)
        width = min(limit, max(len(item.ids) for item in encoded))
    except (AttributeError, TypeError):
        # 早期 tokenizers 接口差异：逐条 encode
        encoded = [tokenizer.encode(t) for t in texts]
        width = min(limit, max(len(item.ids) for item in encoded))
    import numpy as np

    ids = np.zeros((len(encoded), width), dtype=np.int64)
    mask = np.zeros((len(encoded), width), dtype=np.int64)
    for row, item in enumerate(encoded):
        piece_ids, piece_mask = item.ids[:width], item.attention_mask[:width]
        ids[row, : len(piece_ids)] = piece_ids
        mask[row, : len(piece_mask)] = piece_mask
    return {"input_ids": ids, "attention_mask": mask}


def _session(name: str, threads: int):
    ort = _module("onnxruntime")
    path = MODELS / name
    if not path.is_file():
        raise FileNotFoundError(f"模型文件缺失：{path}")
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = threads
    return ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def _graceful_embed(result: BenchResult) -> bool:
    """嵌入跑分缺前置（模型文件 / 依赖）时统一降级，返回 False 表示本次跳过。"""
    if _module("onnxruntime") is None:
        result.add("模型", "嵌入跑分", _missing("onnxruntime"))
        result.mark_degraded("嵌入：未装 onnxruntime")
        return False
    for name in (EMBED_NAME, "tokenizer.json"):
        if not (MODELS / name).is_file():
            result.add("模型", "嵌入跑分", f"n/a（缺 {name}）")
            result.mark_degraded(f"嵌入：缺模型文件 {name}")
            return False
    return True


def bench_embed(result: BenchResult, quick: bool) -> None:
    """模型加载（冷/热）+ 嵌入单条 vs 批量。"""
    if not _graceful_embed(result):
        return
    # 嵌入加载（冷/热）
    cold = time.perf_counter()
    _session(EMBED_NAME, 6)
    cold_ms = _milli(time.perf_counter() - cold)
    hot = _best(lambda: _session(EMBED_NAME, 6), repeat=2)
    result.add("模型", "嵌入加载（冷/热） 6线程", f"{cold_ms:.0f} ms / {_milli(hot):.0f} ms")

    tokenizers = _module("tokenizers")
    np = _module("numpy")
    if tokenizers is None or np is None:
        result.add("模型", "嵌入 单条/批量", _missing("tokenizers" if tokenizers is None else "numpy"))
        result.mark_degraded("嵌入跑分：缺 tokenizers 或 numpy")
        return
    tokenizer = tokenizers.Tokenizer.from_file(str(MODELS / "tokenizer.json"))
    embedder = _session(EMBED_NAME, 6)

    def hidden_of(texts: list[str]) -> None:
        feeds = _encode(tokenizer, texts, 512)
        embedder.run(None, feeds)

    # 单条（默认 6 线程），到 300 token 以内验证存活
    sample_256 = _make_text(264)
    hidden_of([sample_256])  # 预热
    single = _best(lambda: hidden_of([sample_256]), repeat=5)
    result.add(
        "模型", "嵌入 单条（264 token，6 线程）",
        f"{_milli(single):.0f} ms",
    )

    if quick:
        return

    # 批量 1 / 8 / 32（都定到 264 token 基数）
    a = [_make_text(264)] * 1
    b = [_make_text(264)] * 8
    c = [_make_text(264)] * 32
    for label, texts in (("batch=1", a), ("batch=8", b), ("batch=32", c)):
        hidden_of(texts[:1])  # 预热
        dt = _best(lambda t=texts: hidden_of(t), repeat=3)
        per = dt / len(texts)
        result.add(
            "模型", f"嵌入 {label}（264 token，6 线程）",
            f"{_milli(dt):.0f} ms（{_micro(per):.0f} µs/条）",
        )

    # 线程 1 / 6 / 14（264 token）
    for threads in (1, 6, 14):
        sess = _session(EMBED_NAME, threads)
        feeds = _encode(tokenizer, [sample_256], 512)
        sess.run(None, feeds)  # 预热
        dt = _best(lambda ss=sess, ff=feeds: ss.run(None, ff), repeat=3)
        result.add("模型", f"嵌入 单条 264 token 线程{threads}", f"{_milli(dt):.0f} ms")


def bench_rerank(result: BenchResult, quick: bool) -> None:
    """精排：10×256（默认，Quick 也跑）/ 20×256 / 50×512。"""
    if _module("onnxruntime") is None:
        result.add("精排", "加载 或 跑分", _missing("onnxruntime"))
        result.mark_degraded("精排：未装 onnxruntime")
        return
    for name in (RERANK_NAME, "tokenizer.json"):
        if not (MODELS / name).is_file():
            result.add("精排", "跑分", f"n/a（缺 {name}）")
            result.mark_degraded(f"精排：缺模型文件 {name}")
            return
    tokenizers = _module("tokenizers")
    if tokenizers is None:
        result.add("精排", "跑分", _missing("tokenizers"))
        result.mark_degraded("精排跑分：缺 tokenizers")
        return
    tokenizer = tokenizers.Tokenizer.from_file(str(MODELS / "tokenizer.json"))
    reranker = _session(RERANK_NAME, 8)
    query = SAMPLE[:40]
    doc = _make_text(512)

    def score(count: int, limit: int) -> float:
        pairs = [f"{query}</s></s>{doc}" for _ in range(count)]
        feeds = _encode(tokenizer, pairs, limit)
        reranker.run(None, feeds)  # 预热
        return _best(lambda ff=feeds: reranker.run(None, ff), repeat=3)

    for count, limit, label in (
        (10, 256, "10 对 ×256（默认）"),
        (20, 256, "20 对 ×256"),
        (50, 512, "50 对 ×512"),
    ):
        if quick and count != 10:
            continue
        dt = score(count, limit)
        result.add(
            "精排", label,
            f"{_milli(dt):.0f} ms（{_milli(dt) / count:.1f} ms/对）",
        )


def bench_search(result: BenchResult) -> None:
    """检索侧：FTS5 查询、numpy 矩阵打分。"""
    # ---- FTS5 ----
    sqlite = _module("sqlite3")
    if sqlite is None:
        result.add("词法", "FTS5 查询（5 万条）", _missing("sqlite3"))
        result.mark_degraded("词法：缺 sqlite3")
    else:
        np = _module("numpy")

        def fts(count: int) -> tuple[float, float]:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(tokens)")
            words = [f"词{index}" for index in range(4000)]
            rows = [(" ".join(np.random.choice(words, size=40)),) for _ in range(count)]
            started = time.perf_counter()
            conn.executemany("INSERT INTO t(tokens) VALUES (?)", rows)
            insert = time.perf_counter() - started
            query = _best(
                lambda: conn.execute(
                    "SELECT rowid, bm25(t) AS s FROM t WHERE t MATCH ? ORDER BY s LIMIT 50",
                    ("词7 OR 词128 OR 词999",),
                ).fetchall(),
                repeat=5,
            )
            conn.close()
            return insert, query

        insert, query = fts(50_000)
        result.add(
            "词法", "FTS5 入库 / 查询（5 万条）",
            f"{insert * 1000:.0f} ms（{50_000 / insert:,.0f} 条/秒）/ {_milli(query):.2f} ms",
        )

    # ---- numpy 矩阵打分 ----
    np = _module("numpy")
    if np is None:
        result.add("向量", "矩阵打分", _missing("numpy"))
        result.mark_degraded("向量：未装 numpy")
        return
    rng = np.random.default_rng(20261014)
    query = rng.standard_normal(768).astype(np.float32)
    query /= np.linalg.norm(query)
    for count in MATRIX_SIZES:
        matrix = rng.standard_normal((count, 768)).astype(np.float32)
        matrix /= np.linalg.norm(matrix, axis=-1, keepdims=True)
        score_t = _best(lambda m=matrix: m @ query, repeat=5)
        matmul_ms = _milli(score_t)
        result.add(
            "向量", f"矩阵打分（{count:,} 条 ×768）",
            f"{matmul_ms:.1f} ms（独占矩阵 {matrix.nbytes / 1048576:.0f} MB）",
        )
        del matrix


def bench_p0_gates(result: BenchResult, quick: bool) -> None:
    """**P0 交付门禁**：把"验收数字"变成可复跑的检查（P0.2 / P0.3 / P0.4）。

    为什么这些门禁放在**脚本**里而不是单元测试里：同一句 ``score_pairs`` 在空载机器上
    268 ms、在跑满测试的机器上 4.9 s（实测）。把墙钟时间写进测试只会得到"随机红"，
    而随机红的门禁最后一定被人删掉——那就等于没有门禁。

    判定口径：
        - **嵌入**：查"固定宽度 + 至少 2 行"这条形状约束是否生效（P0.2 的根因发现）；
        - **精排**：10 对 ×256（交付计划目标 <2.5 s）；
        - **矩阵**：10 万条查询含掩码（目标 <15 ms）、载入（目标 <1.5 s）、内存（<350 MB）；
        - **Qdrant**：写入吞吐（10 万条目标 <10 分钟）——**这一项默认不跑**，
          它要写几万个点到磁盘（几十秒到几分钟）。加 ``--qdrant`` 才跑。
    """
    np = _module("numpy")
    if np is None:
        result.add("P0", "交付门禁", _missing("numpy"))
        result.mark_degraded("P0 门禁：未装 numpy")
        return

    # ---- 嵌入：可复现性（**口径要准**，别写成"单条与批量必须相同"）----
    try:
        from app.core.embed import embed_text, embed_texts
    except ImportError as exc:
        result.add("P0", "嵌入可复现性", f"n/a（{exc}）")
    else:
        try:
            text = "违约金怎么算：逾期交货的，每逾期一日按合同总额的千分之三支付违约金。"
            # ① **同一批组成重复调用**必须逐位相同（这是能承诺的口径，也是入库的前提）
            first = embed_texts([text, "另一条完全不同的内容。"])
            second = embed_texts([text, "另一条完全不同的内容。"])
            repeatable = bool(np.array_equal(first, second))
            result.add(
                "P0",
                "嵌入可复现（同批重复）",
                f"{'逐位相同 ✅' if repeatable else '不一致 ❌'}",
            )

            # ② 单条与"1 条批"要走同一条路（都是 2 行）——这是查询与语料同形状的保证
            alone = embed_text(text)
            single_batch = embed_texts([text])[0]
            same_path = bool(np.array_equal(alone, single_batch))
            result.add(
                "P0",
                "嵌入形状（单条 == 1 条批）",
                f"{'逐位相同 ✅' if same_path else '不一致 ❌'}",
            )

            # ③ 跨批组成本来就会差一点（int8 量化的形状敏感性）——**只报数字，不判定**：
            # 实测余弦 ~0.988。它说明"批组成影响分数"，所以真实路径必须每查询一个批次。
            pair = embed_texts([text, "另一条完全不同的内容。"])[0]
            result.add(
                "P0",
                "嵌入跨批组成差（仅记录，不作判定）",
                f"余弦 {float(np.dot(alone, pair)):.4f}（预期 ~0.988）",
            )

            vector = embed_texts([text])[0]
            result.add("P0", "嵌入范数（单位向量）", f"{float(np.linalg.norm(vector)):.6f}")
        except Exception as exc:  # 模型缺失等
            result.add("P0", "嵌入可复现性", f"n/a（{type(exc).__name__}: {exc}）")
            result.mark_degraded("P0：嵌入不可用")

    # ---- 精排：10 对 ×256（目标 <2.5 s）----
    try:
        from app.core.rerank import score_pairs
    except ImportError as exc:
        result.add("P0", "精排 10 对 ×256", f"n/a（{exc}）")
    else:
        docs = [
            "第七条 违约责任：乙方逾期交货的，每逾期一日按合同总额的千分之三向甲方支付违约金。",
            "付款节奏分三段：预付款 30%、到货款 60%、质保金 10%，验收合格后 30 天内退还质保金。",
            "设备参数对照表：A-100 交付周期 30 天，质保期 12 个月，单价 12800 元。",
            "甲方应在收到发票后 15 个工作日内完成付款，逾期付款按同期贷款利率计息。",
            "因不可抗力导致不能履行的，根据不可抗力的影响，部分或者全部免除责任。",
            "本合同自双方签字盖章之日起生效，有效期为三年。",
            "争议解决方式：提交合同签订地有管辖权的人民法院诉讼解决。",
            "验收标准：按附件一的技术规格逐项核对，全部合格后方可签署验收单。",
            "保密条款：双方对合同内容及履行过程中知悉的商业秘密负有保密义务。",
            "本次验收共 3 个批次，其中第 2 批次延迟 6 天到货。",
        ]
        try:
            elapsed = _best(lambda: score_pairs("违约金怎么算", docs, max_tokens=256), repeat=3)
            ms = _milli(elapsed)
            verdict = "达标 ✅" if ms < 2500 else "超标 ❌"
            result.add("P0", "精排 10 对 ×256（目标 2500 ms）", f"{ms:.0f} ms {verdict}")

            # 质量：无歧义问题要把对的那条排第一（精排存在的全部理由）
            scores = score_pairs("保密条款要不要保密", docs)
            top = int(np.argmax(scores))
            result.add(
                "P0",
                "精排质量（无歧义问题 top1）",
                f"第 {top} 条（期望 8）{'✅' if top == 8 else '❌'}"
                f" · 分差 {float(scores.max() - scores.min()):.1f}",
            )
        except Exception as exc:
            result.add("P0", "精排 10 对 ×256", f"n/a（{type(exc).__name__}: {exc}）")
            result.mark_degraded("P0：精排不可用")

    # ---- 矩阵：10 万条查询 / 载入 / 内存（P0.4 验收）----
    try:
        from app.core.vector import matrix as matrices
    except ImportError as exc:
        result.add("P0", "矩阵打分", f"n/a（{exc}）")
    else:
        rows_count, dims = (20_000, 768) if quick else (100_000, 768)
        rng = np.random.default_rng(20261015)
        vectors = rng.standard_normal((rows_count, dims)).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=-1, keepdims=True)
        keys = [f"{i // 10}:1:{i}" for i in range(rows_count)]
        payloads = [{"status": "active", "doc_id": i // 10} for i in range(rows_count)]
        matrix = matrices.Matrix("bench", "bench", dims, vectors, keys, payloads)

        mask = np.ones(rows_count, dtype=bool)
        mask[::10] = False
        query = rng.standard_normal(dims).astype(np.float32)
        query /= np.linalg.norm(query)
        matrices.search(matrix, query, top_k=50, mask=mask)  # 预热
        score_ms = _milli(_best(lambda: matrices.search(matrix, query, top_k=50, mask=mask), repeat=5))
        budget = 3.0 if quick else 15.0  # 2 万条按 1/5 线性折
        result.add(
            "P0",
            f"矩阵查询 {rows_count:,} 条 ×768（含掩码）",
            f"{score_ms:.1f} ms（目标 <{budget:.0f}）{'✅' if score_ms < budget else '❌'}"
            f" · 内存 {matrix.nbytes / 1048576:.0f} MB",
        )
        if not quick:
            memory_ok = matrix.nbytes < 350 * 1048576
            result.add(
                "P0",
                "矩阵内存（目标 <350 MB）",
                f"{matrix.nbytes / 1048576:.0f} MB {'✅' if memory_ok else '❌'}",
            )
        del matrix, vectors

    # ---- Qdrant 写入吞吐（可选，见 docstring）----
    if _qdrant_enabled() and not quick:
        try:
            _bench_qdrant_write(result)
        except Exception as exc:
            result.add("P0", "Qdrant 写入", f"n/a（{type(exc).__name__}: {exc}）")
            result.mark_degraded("P0：Qdrant 不可用")


def _qdrant_enabled() -> bool:
    """`--qdrant` 才跑写入吞吐（它要真写几万个点到磁盘）。"""
    import sys

    return "--qdrant" in sys.argv


def _bench_qdrant_write(result: BenchResult) -> None:
    """Qdrant 本地模式的写入吞吐（10 万条目标 <10 分钟）。

    **单独脚本进程**跑：它要在一个临时目录里建集合、写几万条点，
    与"当前数据目录"隔开（不然会动到真实向量库）。
    """
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(dir=str(root / ".tmp")) as folder:
        script = (
            "import json,sys,time,numpy as np\n"
            "from qdrant_client import QdrantClient, models\n"
            f"client = QdrantClient(path=r'{folder}')\n"
            "client.create_collection('bench', vectors_config=models.VectorParams(size=768,"
            " distance=models.Distance.COSINE))\n"
            "rng = np.random.default_rng(1)\n"
            "total, batch = 20000, 5000\n"
            "started = time.perf_counter()\n"
            "for start in range(0, total, batch):\n"
            "    raw = rng.standard_normal((batch, 768)).astype(np.float32)\n"
            "    raw /= np.linalg.norm(raw, axis=1, keepdims=True)\n"
            "    client.upload_collection(collection_name='bench', vectors=raw,\n"
            "        payload=[{'i': start + i} for i in range(batch)],\n"
            "        ids=list(range(start, start + batch)))\n"
            "elapsed = time.perf_counter() - started\n"
            "print(json.dumps({'elapsed': elapsed, 'total': total}))\n"
            "client.close()\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(root),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr else "子进程失败")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        elapsed = float(payload["elapsed"])
        total = int(payload["total"])
        rate = total / elapsed
        projected = 100_000 / rate
        verdict = "达标 ✅" if projected < 600 else "超标 ❌"
        result.add(
            "P0",
            "Qdrant 写入吞吐（10 万条目标 <10 分钟）",
            f"{rate:.0f} 条/秒 → 10 万约 {projected / 60:.1f} 分钟 {verdict}",
        )


def bench_pdf(result: BenchResult) -> None:
    """PDF 解析（PyMuPDF，页/秒）。"""
    fitz = _module("fitz")
    if fitz is None:
        result.add("解析", "PyMuPDF 取文本", _missing("pymupdf"))
        result.mark_degraded("解析：未装 pymupdf")
        return
    try:
        document = fitz.open()
        for index in range(200):
            page = document.new_page()
            page.insert_textbox(
                fitz.Rect(72, 72, 523, 700),
                f"第 {index + 1} 页\n\n" + SAMPLE * 12,
                fontsize=11,
                fontname="china-s",
            )
        data = document.tobytes()
        document.close()

        def extract() -> int:
            opened = fitz.open(stream=data, filetype="pdf")
            total = sum(len(page.get_text().strip()) for page in opened)
            opened.close()
            return total

        dt = _best(extract, repeat=3)
        pages = 200
        per_ms = _milli(dt) / pages
        result.add(
            "解析", "PyMuPDF 取文本（200 页）",
            f"{per_ms:.2f} ms/页（{pages / dt:,.0f} 页/秒）",
        )
    except Exception as exc:  # —— 探针直接报人话
        result.add("解析", "PyMuPDF 取文本", f"n/a（{exc}）")
        result.mark_degraded(f"解析：PyMuPDF 构造/抽取失败 {exc}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(quick: bool) -> BenchResult:
    result = BenchResult(mode="quick" if quick else "full", timestamp=time.strftime("%Y-%m-%d %H:%M:%S"))
    bench_tokenizer(result, quick)
    bench_embed(result, quick)
    bench_rerank(result, quick)
    # **P0 交付门禁**：走的是实装代码（app/core/embed、rerank、vector），
    # 与上面几项"探针式"测量的区别是——它检查"验收数字还在不在"。
    bench_p0_gates(result, quick)
    if not quick:
        bench_search(result)
        bench_pdf(result)
    return result


def _summary_value(result: BenchResult) -> str:
    """从结果里挑几项拼成 append 到 §9.9 的一行数字。"""
    def get(category: str, item: str) -> str | None:
        for row in result.rows:
            if row.category == category and row.item.startswith(item):
                return row.value
        return None

    embed_single = get("模型", "嵌入 单条") or "?"
    rerank_def = get("精排", "10 对 ×256") or "?"
    parts = [f"嵌入单条 {embed_single}", f"精排默认 {rerank_def}"]
    for cat, prefix in (("向量", "矩阵打分"), ("解析", "PyMuPDF")):
        for row in result.rows:
            if row.category == cat and row.item.startswith(prefix):
                parts.append(f"{row.item}={row.value}")
    return "；".join(parts)


def append_to_docs(result: BenchResult) -> Path | None:
    """把一条汇总行追加到 §9.9 实测表；返回改后的 docs 路径（没改返回 None）。"""
    doc = ROOT / "docs" / "技术方案.md"
    if not doc.is_file():
        return None
    text = doc.read_text(encoding="utf-8")
    marker = "### 9.9"
    if marker not in text:
        return None
    head, tail = text.split(marker, 1)
    # 取 §9.9 内的最后一条表格行（`| ... |` 开头），在其后插入新行
    lines = tail.splitlines()
    insert_at = None
    for i, line in enumerate(lines):
        if line.startswith("|") and line.endswith("|"):
            insert_at = i
        elif insert_at is not None and not line.startswith("|"):
            break
    if insert_at is None:
        return None
    row = (
        f"| bench | {result.mode}（{result.timestamp}） | "
        f"{_summary_value(result)} |"
    )
    lines.insert(insert_at + 1, row)
    doc.write_text(head + marker + "\n".join(lines), encoding="utf-8")
    return doc


def _json_output(result: BenchResult) -> str:
    return json.dumps(
        {
            "mode": result.mode,
            "timestamp": result.timestamp,
            "python": sys.version.split()[0],
            "cpus": os.cpu_count(),
            "rows": result.table(),
            "degraded": result.degraded,
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="性能基线：把 §9.9 的数字固化成可复跑门禁")
    parser.add_argument("--quick", action="store_true", help="只跑嵌入单条 + 精排默认")
    parser.add_argument("--json", action="store_true", help="机器读：输出 JSON")
    parser.add_argument(
        "--output-dir", default=None, help="把 markdown 表写到这里（默认 .tmp/bench/）"
    )
    parser.add_argument(
        "--no-append-doc", action="store_true", help="全量跑也不追加到 docs §9.9"
    )
    parser.add_argument(
        "--qdrant",
        action="store_true",
        help="加跑 Qdrant 写入吞吐（要真写 2 万个点到临时目录，几十秒）",
    )
    args = parser.parse_args()

    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        result = run(quick=args.quick)
    except FileNotFoundError as exc:
        # 模型文件缺失（一次性工具不该因为缺文件就裸 traceback）
        print(f"无法跑（缺前置）：{exc}")
        return 1
    except Exception as exc:  # 一次性工具，报清楚即可
        print(f"无法跑（异常）：{type(exc).__name__}: {exc}")
        return 1

    if args.json:
        print(_json_output(result))
    else:
        print(f"# 性能基线（{result.mode}） {result.timestamp}  Python {sys.version.split()[0]}  "
              f"CPU {os.cpu_count()} 核")
        print()
        print(_render_markdown(result.table()))

    if result.degraded:
        print("降级项：")
        for message in result.degraded:
            print(f"  - {message}")

    # 写 markdown 到输出目录（默认 .tmp/bench/，不破坏 docs）
    out_dir = Path(args.output_dir) if args.output_dir else ROOT / ".tmp" / "bench"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"bench_{result.mode}_{result.timestamp.replace(':', '-').replace(' ', '_')}.md"
        out_file.write_text(_render_markdown(result.table()), encoding="utf-8")
        if not args.json:
            print(f"\n表格已写：{out_file}")
    except OSError as exc:
        if not args.json:
            print(f"（写表格失败，忽略：{exc}）")

    # 全量跑：追加一行到 docs §9.9（除非 --no-append-doc）
    if not args.quick and not args.no_append_doc:
        changed = append_to_docs(result)
        if changed is not None:
            print(f"已追加一行到 docs/技术方案.md §9.9：{changed.name}")
        elif not args.json:
            print("docs/技术方案.md 没有 §9.9，未追加（首次可手动补个小节）")

    # 有降级项（缺依赖/缺模型文件）说明这次基准不完整 → 退出码 1，与文档契约一致
    return 1 if result.degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
