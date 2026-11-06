"""意图路由评估：金标集上跑「规则+语义」，输出命中率与三态混淆。

供阈值校准/灰度对齐用：不改图、不调大模型，只算指标，可离线跑。
"""

from __future__ import annotations  # 延迟求值注解

from dataclasses import dataclass, field  # 结果容器
from pathlib import Path  # 金标集路径
from typing import Any, Callable  # 类型

import yaml  # 金标集

from app.agent.nodes.semantic.node import decide  # 三态（复用节点阈值）
from app.agent.routing.embeddings import EmbedFn, RouteIndex  # 索引
from app.agent.routing.policy import PolicySpec  # 策略
from app.agent.routing.rules._schema import RuleSpec  # 规则
from app.agent.routing.rules.loader import match  # 规则匹配

Embed = EmbedFn  # 别名
Metrics = Callable[[float], bool]  # 预留：阈值切片用

#: 金标集：{问题 -> 期望路由}；期望为 None 表示「应当未命中（回兜底/澄清）」。
GoldenSet = dict[str, str | None]  # 类型别名

#: 默认金标集。
DEFAULT_GOLDEN = Path(__file__).parent / "goldenset.yaml"  # 路径


@dataclass(frozen=True)
class EvalResult:  # 评估结果
    total: int  # 样本数
    correct: int  # 精确命中
    rule_decided: int  # 由规则命中
    semantic_direct: int  # 由语义 direct 命中
    by_decision: dict[str, int]  # {decision: 次数}
    misses: list[tuple[str, str | None, str | None]] = field(default_factory=list)  # (问题, 期望, 预测)


def load_golden(path: str | Path = DEFAULT_GOLDEN) -> GoldenSet:  # 载入金标集
    """支持 `cases: [{question, expected}]`；expected 省略视为 None。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}  # 读
    cases = data.get("cases") if isinstance(data, dict) else data  # 用例列表
    golden: GoldenSet = {}  # 结果
    for case in cases or []:  # 逐条
        if not isinstance(case, dict):  # 非法
            continue  # 跳过
        question = str(case.get("question") or "").strip()  # 问题
        if not question:  # 空
            continue  # 跳过
        expected = case.get("expected")  # 期望
        golden[question] = str(expected) if expected is not None else None  # 记录
    return golden  # 返回


def evaluate(  # 跑一遍金标集
    rules: tuple[RuleSpec, ...],  # 规则
    index: RouteIndex,  # 语义索引
    golden: GoldenSet,  # 金标
    embed: Embed,  # query 编码
    *,
    policy: PolicySpec | None = None,  # 阈值策略（None=模块默认）
) -> EvalResult:  # 结果
    """每条样本：先规则，未命中再语义；预测=路由名（None=未命中）。"""
    total = len(golden)  # 样本
    correct = 0  # 命中
    rule_decided = 0  # 规则决定
    semantic_direct = 0  # 语义决定
    by_decision: dict[str, int] = {"direct": 0, "ambiguous": 0, "miss": 0}  # 分布
    misses: list[tuple[str, str | None, str | None]] = []  # 错例
    for question, expected in golden.items():  # 逐样本
        rule_hit = match(question, rules)  # 规则
        if rule_hit is not None:  # 规则命中
            predicted: str | None = rule_hit.route  # 路由
            decision = "rule"  # 决策源
        else:  # 语义
            recall = decide(index, embed(question), policy=policy)  # 三态（可换阈值）
            decision = recall.decision  # 决策
            if decision == "direct":  # direct
                predicted = recall.candidates[0].route if recall.candidates else None  # 路由
            else:  # 兜底/澄清
                predicted = None  # 未命中
        by_decision[decision] = by_decision.get(decision, 0) + 1  # 记分布
        if decision == "rule":  # 规则
            rule_decided += 1  # 计数
        elif decision == "direct":  # 语义 direct
            semantic_direct += 1  # 计数
        if predicted == expected:  # 与期望一致
            correct += 1  # 命中
        else:  # 错例
            misses.append((question, expected, predicted))  # 记录
    return EvalResult(  # 汇总
        total=total,  # 样本数
        correct=correct,  # 命中
        rule_decided=rule_decided,  # 规则
        semantic_direct=semantic_direct,  # 语义
        by_decision=by_decision,  # 分布
        misses=misses,  # 错例
    )  # 返回


def accuracy(result: EvalResult) -> float:  # 准确率
    return round(result.correct / result.total, 4) if result.total else 0.0  # 比值


def compare_policies(  # 多策略对比
    rules: tuple[RuleSpec, ...],  # 规则
    index: RouteIndex,  # 索引
    golden: GoldenSet,  # 金标
    embed: Embed,  # 编码
    policies: tuple[PolicySpec, ...],  # 待比较策略
) -> dict[str, EvalResult]:  # {版本: 结果}
    """同一金标集分别按不同阈值策略跑，供灰度决策。"""
    return {policy.version: evaluate(rules, index, golden, embed, policy=policy) for policy in policies}  # 逐策略


def summary(result: EvalResult) -> dict[str, Any]:  # 结果摘要
    """可直接落库/打印的字典。"""
    return {  # 摘要
        "total": result.total,  # 样本
        "correct": result.correct,  # 命中
        "accuracy": accuracy(result),  # 准确率
        "rule_decided": result.rule_decided,  # 规则
        "semantic_direct": result.semantic_direct,  # 语义
        "by_decision": dict(result.by_decision),  # 三态分布
        "misses": [list(item) for item in result.misses],  # 错例
    }  # 结束