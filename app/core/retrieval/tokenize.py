"""中文分词：入库与查询必须用**同一套**切法（全局领域词 + 编号原子）。

两件事一起做（都为了"精确查找"能成立）：

1. **全局领域词（自定义关键词）**：存在 ``token_dict`` 表（后台 CRUD 见
   :mod:`app.core.retrieval.terms`），一行一词。分词器构建时按 ``add_word(词, 词频)``
   加进去，使领域词保持整词。**不再读 ``data/userdict.txt``**：词条进库后是单一事实来源，
   随库备份、可在界面增删改（见 ``docs/检索器设计.md`` §6.4）。
2. **编号/代码保持原子**：``HT-2026-0043``、``1.2.3``、``GB/T 1234``、``2026-10-16`` 这些
   精确查找的锚点**必须是一个 token**——jieba 默认会把它们切碎（``1.2`` + ``.3``），
   切碎了就"按编号精确查找"永远查不到。

## 为什么"编号"用正则保护，而不是全靠 ``suggest_freq``

``add_word`` / ``suggest_freq`` 都会**改动分词器内部频率表**（全局状态）：
调得多了会互相影响，而且它对"带点带横线的编码"这种形状并不总能把边界钉住。
所以分工：

- **词条表里的词** → ``add_word``（jieba 内部会用 ``suggest_freq`` 算出让它成为整词的频率，
  这正是 ``suggest_freq`` 的标准用法）；
- **编码类形状** → **正则先切出受保护片段**，这些片段整段作为一个 token 交给索引，
  不经过 jieba。确定性好、不污染频率表、也不会与词条表互相打架。

## 其他约定

- **关掉 HMM**（``HMM=False``）：保证入库端与查询端切法稳定一致。开着 HMM 时新词发现
  可能让同一个词在不同上下文里切法不同，表现就是"明明有却搜不到"。
- **分词器按进程级复用**（jieba 首次加载词典约 1 秒），但**按词条版本号换缓存**：
  ``terms`` 每次增删改都会 bump 版本号，下次取分词器时自动重建（不用重启）。
- **已知取舍**：单字查询、未登录词可能命不中（它们在索引里不是一个独立 token）。要治得靠
  单字兜底或换 trigram 分词器——那属于换检索策略（P4 定）。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import jieba
from loguru import logger

from app.core import paths
from app.core.retrieval import terms

#: 编号/编码类形状：**整段当一个 token**（精确查找的锚点，切碎就查不到）
#: 只覆盖"明显是编码"的形状，避免过度保护影响正常中文分词。
#:
#: 两条踩过的坑：
#: 1. **每条 pattern 后面都要有逗号**——少了逗号 Python 会做"隐式字符串拼接"，
#:    整个元组变成一个长字符串，`"|".join()` 会把**每个字符**用 `|` 连起来；
#: 2. **顺序有意义**（交替是从左往右先匹配先赢）：比例（`0.05%`）必须排在版本号
#:    （`\d+\.\d+`）前面，否则 `%` 会被剩下来；日期必须排在高位数字前面。
_ATOMIC_PATTERNS = (
    r"第\s*[一二三四五六七八九十百零〇\d]{1,4}\s*[条款项章节]",  # 条款号：第 3 条 / 第十二条
    r"[A-Za-z]{1,8}(?:[/\-_.]\s?[A-Za-z0-9]{1,12}){1,6}",  # 编码：HT-2026-0043 / GB/T 1234-2026
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?",  # 日期：2026-10-16 / 2026年10月16日
    r"\d+(?:\.\d+)?%",  # 比例：0.05% / 5%
    r"\d{1,4}(?:\.\d{1,4}){1,4}",  # 版本/小节号：1.2.3
    r"\d{1,6}\s*[-–—]\s*\d{1,6}",  # 数字区间/标准号：1234-2026 / 30-60
    r"\d{4,}",  # 长数字：10086 / 2026
)
_ATOMIC = re.compile("|".join(_ATOMIC_PATTERNS))

_lock = threading.Lock()
_tokenizer: jieba.Tokenizer | None = None
#: 分包器缓存键 = 词条版本号（``terms.generation()``）。词条增删改后自动失效。
_cache_key: int | None = None


def reset() -> None:
    """丢掉分词器缓存（词条变了、测试之间隔离用）。下次取分词器会按当前词条重建。"""
    global _tokenizer, _cache_key
    with _lock:
        _tokenizer = None
        _cache_key = None


def _build_tokenizer() -> jieba.Tokenizer:
    """按当前词条表构建分词器（**不缓存**，缓存由 :func:`tokenizer` 管）。"""
    instance = jieba.Tokenizer()
    # 把分词缓存（前缀词典，约 9 MB）钉进 data/。
    # 为什么必须钉：jieba 的缓存路径是 `os.path.join(self.tmp_dir or tempfile.gettempdir(), name)`，
    # 而 **`tempfile.gettempdir()` 在 TEMP/TMPDIR 都不可用时会退回"当前工作目录"** —— 于是仓库根里
    # 会凭空多出一个 `jieba.cache`（和 NiceGUI 测试装置的 `nicegui-test-storage-*` 是同一类坑）。
    # 钉进 data/ 还顺带得到：测试换了安装目录就换缓存，用例之间互不干扰。
    cache_dir = paths.data_dir()
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    instance.tmp_dir = str(cache_dir)
    instance.initialize()
    words = 0
    for word, freq in terms.load_terms():
        try:
            instance.add_word(word, freq)
        except Exception as exc:  # 单条坏掉不该让整份词条失效
            logger.warning("词条加不进分词器（{}）：{}", word, exc)
            continue
        words += 1
    if words:
        logger.debug("全局领域词已加载：{} 个（token_dict）", words)
    return instance


def tokenizer() -> jieba.Tokenizer:
    """取进程级分词器；**词条版本号变了会自动重建**（不用重启）。"""
    global _tokenizer, _cache_key
    key = terms.generation()
    if _tokenizer is not None and _cache_key == key:
        return _tokenizer
    with _lock:
        if _tokenizer is None or _cache_key != key:
            _tokenizer = _build_tokenizer()
            _cache_key = key
    return _tokenizer


def _split_atomic(text: str) -> list[tuple[str, bool]]:
    """把文本切成 ``(片段, 是否受保护)``：受保护的片段整段当一个 token。"""
    pieces: list[tuple[str, bool]] = []
    cursor = 0
    for match in _ATOMIC.finditer(text):
        if match.start() > cursor:
            pieces.append((text[cursor : match.start()], False))
        pieces.append((match.group(0), True))
        cursor = match.end()
    if cursor < len(text):
        pieces.append((text[cursor:], False))
    return pieces


def cut(text: str) -> list[str]:
    """切词（丢掉纯空白片段）；**编号/编码整段保留**。"""
    if not text:
        return []
    words: list[str] = []
    for piece, protected in _split_atomic(text):
        if not piece:
            continue
        if protected:
            words.append(piece)
            continue
        words.extend(word for word in tokenizer().lcut(piece, HMM=False) if word.strip())
    return words


def to_tokens(text: str) -> str:
    """把正文变成索引文本（空格分隔）——写进词法索引的那一列。"""
    return " ".join(cut(text))


def stats() -> dict[str, object]:
    """词条现状（设置页显示用）：词条数、数据源。"""
    try:
        count = len(terms.load_terms(enabled_only=False))
    except Exception:  # 库还没建好也不该让状态接口炸
        count = 0
    return {"words": count, "source": "token_dict"}
