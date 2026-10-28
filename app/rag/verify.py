"""引用校验（L1 编号 + L3 溯源）：纯函数，**不调用任何大模型**。

分工：
- **L1**（便宜，正则）：角标越界 / 为 0 / 重复 / 全角写法 / 代码块里的假角标；
- **L3**（要读库，一次批量 SQL）：这条引用对应的 chunk、文档、版本、内容指纹、源文件
  现在还在不在。

`lookup` 是唯一的外部依赖：单测注入假快照，线上走 :func:`store_lookup`。
这样 L3 的判定逻辑可以完全脱离数据库测试。
"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 正则
from collections.abc import Callable  # 查询签名
from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

#: 角标：半角 `[1]` 与全角 `【1】` 都认（模型两种都会写）。
_CITE = re.compile(r"[\[【]\s*(\d+)\s*[\]】]")  # 引用编号

#: 代码块：里面的 `[1]` 是示例，不是引用。
_FENCE = re.compile(r"```[^\n]*\n.*?(?:```|\Z)", re.S)  # 围栏代码块

#: 句子切分：中文句末、换行、分号。
_SENTENCE = re.compile(r"[。！？!?；;\n]+")  # 句末

#: 校验结果里出现过的原因码（给轨迹与界面用）。
REASON_NO_CITATION = "no_citation"  # 一个有效引用都没有
REASON_MISSING = "missing"  # 引用的资料已不可用
REASON_INVALID = "invalid"  # 编号非法
REASON_UNSUPPORTED = "unsupported"  # 有句子没被证据支撑

#: L2 字面覆盖阈值（2-gram）。低于它再考虑语义层。
L2_LITERAL = 0.35  # 覆盖比例

#: L2 语义阈值（需要注入 embedder；先不接，留口）。
L2_SEMANTIC = 0.60  # 余弦

#: 否定词：数量奇偶不同就认为"极性反了"。
_NEGATIONS = ("不", "无需", "禁止", "不得", "没有", "未")  # 否定

#: 算 2-gram 前先去掉的标点与空白。
_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】《》]")  # 标点


@dataclass(frozen=True)
class VerifyResult:
    """一次校验的结果；字段都可 JSON 化，直接进事件与轨迹。"""

    text: str  # 去掉非法/失效角标后的正文
    valid: list[int] = field(default_factory=list)  # 有效且能溯源的编号
    invalid: list[int] = field(default_factory=list)  # 编号非法（越界/为 0）
    missing: list[int] = field(default_factory=list)  # 编号合法但资料已不可用
    coverage: float = 0.0  # 带有效引用的句子占比
    unsupported: list[int] = field(default_factory=list)  # 没被证据支撑的句子序号（0 起）
    reasons: list[str] = field(default_factory=list)  # 结构化原因

    @property
    def passed(self) -> bool:  # 是否通过
        """有有效引用，且没有失效引用。"""
        return bool(self.valid) and not self.missing  # 判定


@dataclass(frozen=True)
class TraceSnapshot:
    """L3 需要的事实：批量查出来的一次快照。"""

    documents: dict[int, dict] = field(default_factory=dict)  # doc_id -> 文档行
    chunks: dict[int, dict] = field(default_factory=dict)  # chunk_id -> 片段行
    active: dict[int, int] = field(default_factory=dict)  # doc_id -> 当前 active 版本
    files: dict[int, bool] = field(default_factory=dict)  # doc_id -> 源文件是否存在


#: 查询签名：证据列表 -> 快照。
Lookup = Callable[[list[dict[str, Any]]], TraceSnapshot]  # 调用方注入


def _mask_code(text: str) -> str:  # 屏蔽代码块
    """把围栏代码块换成等长空格，避免"示例里的 [1]"被当成引用。

    等长是为了让匹配出来的下标能直接作用回原文。
    """
    return _FENCE.sub(lambda match: " " * len(match.group(0)), text)  # 等长替换


def verify_l1(text: str, count: int) -> tuple[str, list[int], list[int]]:  # L1
    """校验角标编号：返回 (清理后的文本, 有效编号, 非法编号)。"""
    masked = _mask_code(text)  # 代码块不参与
    valid: list[int] = []  # 有效
    invalid: list[int] = []  # 非法
    cuts: list[tuple[int, int]] = []  # 要删掉的片段
    for match in _CITE.finditer(masked):  # 逐个角标
        number = int(match.group(1))  # 编号
        if 1 <= number <= count:  # 在范围内
            if number in valid:  # 重复引用
                cuts.append((match.start(), match.end()))  # 删掉重复的那个
            else:  # 第一次出现
                valid.append(number)  # 记有效
        else:  # 越界或 0
            if number not in invalid:  # 只记一次
                invalid.append(number)  # 记非法
            cuts.append((match.start(), match.end()))  # 删掉
    cleaned = text  # 从原文开始删
    for start, end in sorted(set(cuts), reverse=True):  # 从后往前删，下标不串
        cleaned = cleaned[:start] + cleaned[end:]  # 删除
    return cleaned, valid, invalid  # 返回


def verify_l3(  # L3
    evidence: list[dict[str, Any]],
    numbers: list[int],
    lookup: Lookup,
) -> tuple[list[int], list[int], list[str]]:  # (有效, 失效, 原因)
    """按编号回查资料是否还在：返回 (still_ok, missing, reasons)。"""
    if not numbers:  # 没有要查的
        return [], [], []  # 直接空
    try:  # 查询失败不该让回答失败
        snapshot = lookup(evidence)  # 批量快照
    except Exception:  # 库异常
        return list(numbers), [], ["lookup_failed"]  # 认作"暂时查不到"，不判失效
    ok: list[int] = []  # 通过
    missing: list[int] = []  # 失效
    reasons: list[str] = []  # 原因
    for number in numbers:  # 逐条
        item = evidence[number - 1] if 0 < number <= len(evidence) else {}  # 对应证据
        if _trace_ok(item, snapshot):  # 可溯源
            ok.append(number)  # 通过
        else:  # 不可溯源
            missing.append(number)  # 记失效
    if missing:  # 有失效
        reasons.append(REASON_MISSING)  # 记原因
    return ok, missing, reasons  # 返回


def _trace_ok(item: dict[str, Any], snapshot: TraceSnapshot) -> bool:  # 单条溯源
    """五个条件全过才算"这条资料还在"。"""
    doc_id = int(item.get("doc_id") or 0)  # 文档
    chunk_id = int(item.get("chunk_id") or 0)  # 命中片段
    version = int(item.get("version") or 0)  # 版本
    expected_hash = str(item.get("text_hash") or "")  # 内容指纹
    if not doc_id:  # 老数据没存 id
        return False  # 无法溯源 → 判失效（界面提示"无法校验"）
    if chunk_id and chunk_id not in snapshot.chunks:  # 片段被删
        return False  # 失效
    row = snapshot.documents.get(doc_id)  # 文档行
    if row is None:  # 文档被删
        return False  # 失效
    if version and snapshot.active.get(doc_id) != version:  # 版本已不是 active
        return False  # 失效
    if expected_hash and str(row.get("text_hash") or "") != expected_hash:  # 内容被换
        return False  # 失效
    return snapshot.files.get(doc_id) is not False  # 源文件在（未记录时也算在）


def coverage(text: str, valid: list[int]) -> float:  # 引用覆盖率
    """带有效引用的句子 / 总句子；用来判断"是不是整篇都在堆角标"。"""
    body = _mask_code(text)  # 代码块不算句子
    sentences = [part for part in _SENTENCE.split(body) if part.strip()]  # 切句
    if not sentences:  # 空文本
        return 0.0  # 0
    hit = 0  # 命中的句子数
    for sentence in sentences:  # 逐句
        numbers = {int(match.group(1)) for match in _CITE.finditer(sentence)}  # 句内角标
        if any(number in valid for number in numbers):  # 有有效引用
            hit += 1  # 计数
    return round(hit / len(sentences), 3)  # 比例


def verify(  # 总入口
    text: str,
    evidence: list[dict[str, Any]],
    *,
    lookup: Lookup | None = None,
    embedder: Callable[[str, str], float] | None = None,
    l2: bool = True,
) -> VerifyResult:
    """L1 → L3 → L2 顺序校验；`lookup` 为 None 时跳过 L3，`embedder` 为 None 时只做字面。"""
    cleaned, valid, invalid = verify_l1(text, len(evidence))  # L1
    reasons: list[str] = []  # 原因
    if invalid:  # 有非法编号
        reasons.append(REASON_INVALID)  # 记
    missing: list[int] = []  # 失效
    if lookup is not None and valid:  # 需要且能查库
        valid, missing, more = verify_l3(evidence, valid, lookup)  # L3
        reasons.extend(more)  # 合并原因
        if missing:  # 失效的要从句子里删掉
            cleaned = _drop_marks(cleaned, missing)  # 删角标
    unsupported: list[int] = []  # 没被支撑的句子
    if l2 and valid:  # L2：逐句看有没有被证据支撑
        unsupported = support(cleaned, evidence, embedder=embedder)  # 只标注，不拦截
        if unsupported:  # 有没支撑的
            reasons.append(REASON_UNSUPPORTED)  # 记原因
    if not valid:  # 一个有效引用都没有
        reasons.append(REASON_NO_CITATION)  # 记
    return VerifyResult(  # 返回
        text=cleaned,  # 清理后正文
        valid=valid,  # 有效
        invalid=invalid,  # 非法
        missing=missing,  # 失效
        coverage=coverage(cleaned, valid),  # 覆盖率
        unsupported=unsupported,  # 未支撑句
        reasons=sorted(set(reasons)),  # 去重原因
    )  # 结束


def _bigrams(text: str) -> set[str]:  # 2-gram
    """去标点后的字符二元组集合（中文不需要分词）。"""
    body = _PUNCT.sub("", str(text or ""))  # 去标点
    if len(body) < 2:  # 太短
        return {body} if body else set()  # 原样
    return {body[index : index + 2] for index in range(len(body) - 1)}  # 二元组


def _literal_ok(sentence: str, source: str) -> bool:  # 字面支撑
    """句子里的 2-gram 有多少能在证据里找到。"""
    grams = _bigrams(sentence)  # 句子
    if not grams:  # 空
        return False  # 不算支撑
    return len(grams & _bigrams(source)) / len(grams) >= L2_LITERAL  # 覆盖率


def _negation_mismatch(sentence: str, source: str) -> bool:  # 否定反转
    """一句话被否定了、另一句没否定，且两句在说同一件事 → 极性反转。

    只看"否定词后面紧跟的那 2 个字"：`不予支持` → 看 `支持` 在对方句子里有没有。
    这样"只是引用数字、没提这件事"的句子不会被误判。
    """
    for left, right in ((sentence, source), (source, sentence)):  # 两个方向都查
        for word in _NEGATIONS:  # 逐个否定词
            start = 0  # 搜索起点
            while True:  # 查完所有出现位置
                position = left.find(word, start)  # 位置
                if position < 0:  # 没有了
                    break  # 退出
                core = left[position + len(word) + 1 :][:2]  # 否定词后第 2 个字符起取 2 个字
                if len(core) == 2 and core in right and word not in right:  # 对方说了肯定版
                    return True  # 反转
                start = position + len(word)  # 继续往后找
    return False  # 没发现反转


def _numbers_conflict(sentence: str, source: str) -> bool:  # 数字冲突
    """句子里出现、证据里没有的数字（3 万 vs 5 万）。

    先做中文数字归一（`五十万` → 500000），否则中文金额永远抓不到。
    """
    from app.rag.normalize import normalize  # 延迟导入（规则模块，无模型）

    mine = set(re.findall(r"\d+(?:\.\d+)?", normalize(sentence)))  # 句子
    if not mine:  # 没数字
        return False  # 不算冲突
    theirs = set(re.findall(r"\d+(?:\.\d+)?", normalize(source)))  # 证据
    return bool(mine - theirs)  # 有对不上的


def support(  # L2
    text: str,
    evidence: list[dict[str, Any]],
    *,
    embedder: Callable[[str, str], float] | None = None,
) -> list[int]:
    """返回"带引用但没被证据支撑"的句子序号（0 起）。

    只标注、不删句：误杀（把对的判错）比漏杀更伤用户。
    """
    body = _mask_code(text)  # 代码块不算
    sentences = [part for part in _SENTENCE.split(body) if part.strip()]  # 切句
    bad: list[int] = []  # 结果
    for index, sentence in enumerate(sentences):  # 逐句
        numbers = [int(match.group(1)) for match in _CITE.finditer(sentence)]  # 句内角标
        if not numbers:  # 没引用
            continue  # 不归 L2 管（L1/覆盖率管）
        plain = _CITE.sub("", sentence)  # 去掉角标：`[1]` 里的 1 不是事实数字
        ok = False  # 是否被支撑
        for number in numbers:  # 逐个引用
            if not 0 < number <= len(evidence):  # 越界
                continue  # 跳过
            source = str(evidence[number - 1].get("text") or "")  # 证据正文
            if _numbers_conflict(plain, source):  # 数字对不上
                continue  # 换个引用试试
            if _negation_mismatch(plain, source):  # 极性相反
                continue  # 换
            if _literal_ok(plain, source):  # 字面够
                ok = True  # 支撑
                break  # 不用再看
            if embedder is not None:  # 有语义层
                try:  # 失败就当没有
                    if float(embedder(plain, source)) >= L2_SEMANTIC:  # 够像
                        ok = True  # 支撑
                        break  # 停
                except Exception:  # 嵌入不可用
                    continue  # 换下一个
        if not ok:  # 一个引用都撑不住
            bad.append(index)  # 记
    return bad  # 返回


def _drop_marks(text: str, numbers: list[int]) -> str:  # 删指定角标
    """把指定编号的角标从正文里去掉（代码块里的不动）。"""
    masked = _mask_code(text)  # 代码块不参与
    cuts: list[tuple[int, int]] = []  # 删除区间
    for match in _CITE.finditer(masked):  # 逐个
        if int(match.group(1)) in numbers:  # 命中要删的
            cuts.append((match.start(), match.end()))  # 记录
    cleaned = text  # 原文
    for start, end in sorted(set(cuts), reverse=True):  # 从后往前
        cleaned = cleaned[:start] + cleaned[end:]  # 删除
    return cleaned  # 返回


def store_lookup(evidence: list[dict[str, Any]]) -> TraceSnapshot:  # 线上查询
    """从 SQLite 批量取快照：3 条 SQL 覆盖任意条引用。"""
    from app.core import paths  # 延迟导入（避免 rag 顶层依赖 core.paths）
    from app.rag import kb_store  # 延迟导入（避免循环）

    doc_ids = [int(item.get("doc_id") or 0) for item in evidence]  # 文档
    chunk_ids = [int(item.get("chunk_id") or 0) for item in evidence]  # 片段
    documents = kb_store.documents_by_ids([value for value in doc_ids if value])  # 批一
    chunks = kb_store.chunks_by_ids([value for value in chunk_ids if value])  # 批二
    active = kb_store.active_versions([value for value in doc_ids if value])  # 批三
    files: dict[int, bool] = {}  # 源文件存在性
    for doc_id, row in documents.items():  # 逐文档
        stored = str(row.get("stored_path") or "")  # 相对路径
        if not stored:  # 还没落副本
            files[doc_id] = True  # 不因这个判失效
            continue  # 下一条
        resolved = paths.resolve_stored_path(stored)  # 解析
        files[doc_id] = bool(resolved and resolved.is_file())  # 是否存在
    return TraceSnapshot(documents=documents, chunks=chunks, active=active, files=files)  # 快照
