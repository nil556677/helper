"""证据门限：多信号 → PASS / WEAK / REJECT（**不调用任何大模型**）。

为什么不是"一个分数比一个阈值"：
- RRF 分只能排序，没有相关性语义；
- 精排分是量化模型的 logit，跨版本不可比；
- 所以判决靠**一组信号**：硬要素、精排分、QA 方向性、条数。

执行顺序按"便宜的先做"，明确的 REJECT 不进入后面的推理：

```
硬要素 → 精排分粗筛 → QA 二次打分（只对活下来的 3~6 条）→ 三态判决
```

阈值全部走环境变量（与 `retrieval.py` 的 `HELPER_RRF_*` 同一套写法），
标定前先用保守默认；首发用 `HELPER_GATE_MODE=record` 只记录不拦截。
"""

from __future__ import annotations  # 类型注解延迟求值

import os  # 读阈值
from collections.abc import Callable  # 打分器签名
from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

from app.rag import hard  # 硬要素

#: 三态。
PASS = "pass"  # 可以生成
WEAK = "weak"  # 生成 + 提示"证据较弱"
REJECT = "reject"  # 不生成，拒答


def _env_float(name: str, default: float) -> float:  # 读浮点
    """坏值退回默认。"""
    try:  # 尝试
        return float(os.environ.get(name, default))  # 解析
    except (TypeError, ValueError):  # 坏值
        return default  # 默认


def _env_int(name: str, default: int) -> int:  # 读整数
    """坏值退回默认。"""
    try:  # 尝试
        return int(os.environ.get(name, default))  # 解析
    except (TypeError, ValueError):  # 坏值
        return default  # 默认


#: 精排 logit 的拒答边界（与 reranker 的 MIN_SCORE 同口径：低于它视为"无命中"）。
TOP1_REJECT = _env_float("HELPER_GATE_TOP1_REJECT", -1.0)  # 低于 → REJECT

#: 精排 logit 的"弱证据"边界（本机实测相关查询 top 在 +2.6~+6.1，先取保守的 1.0）。
TOP1_WEAK = _env_float("HELPER_GATE_TOP1_WEAK", 1.0)  # 低于 → WEAK

#: QA 方向性分（同模型、换输入）的两条边界。
QA_REJECT = _env_float("HELPER_GATE_QA_REJECT", -0.5)  # 低于 → REJECT
QA_WEAK = _env_float("HELPER_GATE_QA_WEAK", 0.5)  # 低于 → WEAK

#: 最少证据条数（少于它说明"只捞到一条"，风险高）。
MIN_COUNT = _env_int("HELPER_GATE_MIN_COUNT", 2)  # 默认 2 条

#: QA 打分最多跑几条（成本上限）。
QA_MAX_CANDIDATES = _env_int("HELPER_GATE_QA_MAX", 6)  # 条数

#: QA 打分的文档侧截断（snippet 很短，96 够用；越小越快）。
QA_MAX_TOKENS = _env_int("HELPER_GATE_QA_TOKENS", 96)  # token

#: 模式：`on` 真的拦；`record` 只记录（首发用这个灰度）。
MODE = os.environ.get("HELPER_GATE_MODE", "on").strip().lower()  # 模式

#: 打分器签名：问题 + 候选文本 -> 分数列表（失败返回 None）。
QAScorer = Callable[[str, list[str]], "list[float] | None"]  # 可注入


@dataclass(frozen=True)
class GateDecision:
    """一次门限判决；字段都可 JSON 化。"""

    decision: str  # pass / weak / reject
    kept: list[dict[str, Any]] = field(default_factory=list)  # 通过粗筛的证据
    top1: float | None = None  # 最高精排分
    top3_max: float | None = None  # 前三最高
    qa: float | None = None  # QA 方向性最高分
    hard_missing: list[str] = field(default_factory=list)  # 缺的条款/日期
    soft_missing: list[str] = field(default_factory=list)  # 缺的金额
    count: int = 0  # 条数
    reasons: list[str] = field(default_factory=list)  # 原因码
    ms: int = 0  # 耗时

    @property
    def blocked(self) -> bool:  # 是否拦下
        """是否应当拒答（`record` 模式下永远 False）。"""
        return self.decision == REJECT and MODE == "on"  # 只有 on 才真的拦

    def as_dict(self) -> dict[str, Any]:  # 转字典
        """给事件/轨迹/状态用。"""
        return {  # 载荷
            "decision": self.decision,  # 判决
            "top1": self.top1,  # 最高分
            "top3_max": self.top3_max,  # 前三
            "qa": self.qa,  # 方向性
            "hard_missing": list(self.hard_missing),  # 硬缺
            "soft_missing": list(self.soft_missing),  # 软缺
            "count": self.count,  # 条数
            "reasons": list(self.reasons),  # 原因
            "ms": self.ms,  # 耗时
        }  # 结束


def _score_of(hit: dict[str, Any]) -> float | None:  # 取精排分
    """优先 rerank_score；没有（候选太少跳过精排）返回 None。"""
    meta = hit.get("metadata") or {}  # metadata
    value = meta.get("rerank_score")  # 精排分
    if value is None:  # 没精排
        return None  # 未知
    try:  # 转换
        return float(value)  # 浮点
    except (TypeError, ValueError):  # 坏值
        return None  # 未知


def _snippet_of(hit: dict[str, Any]) -> str:  # 取短文本
    """QA 打分用 snippet（命中段），没有就退回正文前 200 字。"""
    meta = hit.get("metadata") or {}  # metadata
    snippet = str(meta.get("snippet") or "")  # 命中段
    if snippet:  # 有
        return snippet  # 用它
    return str(hit.get("text") or "")[:200]  # 退回正文


def default_qa_scorer(question: str, snippets: list[str]) -> list[float] | None:  # 真打分
    """用本地 reranker 打 `(原问题, snippet)` 的方向性分；不可用返回 None。"""
    try:  # 模型缺失/前向失败都不该让回答失败
        from app.core.rerank import score_pairs  # 延迟导入（避免无模型环境 import 就炸）

        values = score_pairs(question, snippets, max_tokens=QA_MAX_TOKENS)  # 一次批量
        return [float(value) for value in values]  # 转 list
    except Exception:  # 模型不在/超时
        return None  # 视为不可用


def gate_hits(  # 判决
    question: str,
    ranked: list[dict[str, Any]],
    *,
    qa_scorer: QAScorer | None = None,  # 可注入（测试用）
    min_count: int | None = None,  # 覆盖条数门槛
) -> GateDecision:
    """对精排结果做门限判决，返回"留下来的证据 + 判决"。"""
    import time  # 局部导入，避免模块级依赖

    started = time.perf_counter()  # 计时
    need = MIN_COUNT if min_count is None else int(min_count)  # 条数门槛
    if not ranked:  # 一条都没有
        return GateDecision(REJECT, reasons=["no_evidence"], ms=_ms(started))  # 直接拒
    scores = [_score_of(hit) for hit in ranked]  # 每条的分
    known = [value for value in scores if value is not None]  # 有分的
    top1 = max(known) if known else None  # 最高
    top3 = sorted(known, reverse=True)[:3]  # 前三
    top3_max = top3[0] if top3 else None  # 前三最高（没有就用 None）
    hard_missing, soft_missing = hard.missing_elements(  # 硬要素
        question, [_snippet_of(hit) + str((hit.get("metadata") or {}).get("evidence_text") or "") for hit in ranked]
    )  # 结束
    kept = [  # 粗筛：低于拒答线的不要
        hit for hit, score in zip(ranked, scores, strict=False) if score is None or score >= TOP1_REJECT
    ]  # 粗筛结束
    reasons: list[str] = []  # 原因
    if hard_missing:  # 条款/日期没命中
        return GateDecision(  # 硬失败
            REJECT, kept=[], top1=top1, top3_max=top3_max, qa=None,  # 关键字段
            hard_missing=hard_missing, soft_missing=soft_missing,  # 缺项
            count=len(kept), reasons=["hard_missing"], ms=_ms(started),  # 原因
        )  # 结束
    if top1 is not None and top1 < TOP1_REJECT:  # 最高分都低于拒答线
        return GateDecision(  # 低分拒答
            REJECT, kept=[], top1=top1, top3_max=top3_max,  # 关键字段
            hard_missing=[], soft_missing=soft_missing,  # 缺项
            count=0, reasons=["low_score"], ms=_ms(started),  # 原因
        )  # 结束
    if not kept:  # 粗筛后为空
        return GateDecision(REJECT, top1=top1, top3_max=top3_max, count=0, reasons=["low_score"], ms=_ms(started))  # 拒
    qa: float | None = None  # 方向性分
    scorer = qa_scorer if qa_scorer is not None else default_qa_scorer  # 用注入或默认
    snippets = [_snippet_of(hit) for hit in kept[: max(1, QA_MAX_CANDIDATES)]]  # 取前几条
    values = scorer(question, snippets)  # 打分
    if values:  # 有结果
        qa = max(float(value) for value in values)  # 最高分
    if qa is None:  # 模型不可用
        reasons.append("qa_unavailable")  # 记一笔（不因此拒绝）
    elif qa < QA_REJECT:  # 方向性太差
        return GateDecision(  # 答非所问 → 拒
            REJECT, kept=[], top1=top1, top3_max=top3_max, qa=qa,  # 关键字段
            hard_missing=[], soft_missing=soft_missing,  # 缺项
            count=len(kept), reasons=["qa_low"], ms=_ms(started),  # 原因
        )  # 结束
    if len(kept) < need:  # 条数不够
        return GateDecision(  # 拒绝
            REJECT, kept=kept, top1=top1, top3_max=top3_max, qa=qa,  # 关键字段
            hard_missing=[], soft_missing=soft_missing,  # 缺项
            count=len(kept), reasons=["too_few"], ms=_ms(started),  # 原因
        )  # 结束
    weak_reasons: list[str] = []  # 只影响"降级为 WEAK"的信号
    if soft_missing:  # 金额没命中（可能是中文数字）
        weak_reasons.append("amount_missing")  # 降级原因
    if qa is not None and qa < QA_WEAK:  # 方向性偏弱
        weak_reasons.append("qa_weak")  # 记
    if top1 is not None and top1 < TOP1_WEAK:  # 精排偏弱
        weak_reasons.append("score_weak")  # 记
    decision = WEAK if weak_reasons else PASS  # 有任一弱信号就 WEAK
    return GateDecision(  # 通过
        decision, kept=kept, top1=top1, top3_max=top3_max, qa=qa,  # 关键字段
        hard_missing=[], soft_missing=soft_missing,  # 缺项
        count=len(kept), reasons=reasons + weak_reasons, ms=_ms(started),  # 原因
    )  # 结束


def _ms(started: float) -> int:  # 毫秒
    """相对开始的耗时。"""
    import time  # 局部导入

    return int((time.perf_counter() - started) * 1000)  # 毫秒
