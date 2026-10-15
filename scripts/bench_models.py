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
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = threads
    return ort.InferenceSession(
        str(MODELS / name), sess_options=options, providers=["CPUExecutionProvider"]
    )


def bench_embed(result: BenchResult, quick: bool) -> None:
    """模型加载（冷/热）+ 嵌入单条 vs 批量。"""
    if _module("onnxruntime") is None:
        result.add("模型", "嵌入加载 / 精排加载", _missing("onnxruntime"))
        result.mark_degraded("嵌入：未装 onnxruntime")
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
    args = parser.parse_args()

    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    result = run(quick=args.quick)

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
