"""评估：金标集上规则+语义的命中与三态分布。"""

from __future__ import annotations  # 延迟求值注解

from pathlib import Path  # 临时库

from app.agent.contracts.routing import RouteSpec  # 路径
from app.agent.routing.embeddings import load_or_build  # 索引
from app.agent.routing.eval import accuracy, evaluate  # 被测
from app.agent.routing.rules.loader import load_rules  # 规则


def _embed(text: str) -> list[float]:  # 假 embed：按关键词方向
    if "时间" in text or "几点" in text:  # 时间
        return [1.0, 0.0]
    if "天气" in text:  # 天气
        return [0.0, 1.0]
    return [0.0, 0.0]  # 无关


_ROUTES = (  # 路径
    RouteSpec(route_id="time", description="查询当前时间"),  # 时间
    RouteSpec(route_id="weather", description="查询天气"),  # 天气
)


def test_evaluate_on_golden(tmp_path: Path) -> None:  # 金标集评估
    rules = load_rules([Path(__file__).parent])  # 空规则
    index = load_or_build(_ROUTES, _embed, store_path=tmp_path / "r.sqlite", model_version="ev:2")  # 索引
    golden = {  # 金标
        "现在几点": "time",  # 时间
        "北京天气怎么样": "weather",  # 天气
        "完全无关的话题": None,  # 应未命中
    }  # 金标结束
    result = evaluate(rules, index, golden, _embed)  # 评估
    assert result.total == 3  # 样本
    assert result.correct == 3  # 全对
    assert accuracy(result) == 1.0  # 准确率
    assert result.rule_decided == 0  # 无规则命中
    assert result.semantic_direct == 2  # 两个语义 direct


def test_evaluate_reports_miss(tmp_path: Path) -> None:  # 错例被记录
    rules = load_rules([Path(__file__).parent])  # 空规则
    index = load_or_build(_ROUTES, _embed, store_path=tmp_path / "r2.sqlite", model_version="ev:2")  # 索引
    golden = {"现在几点": "weather"}  # 期望错的
    result = evaluate(rules, index, golden, _embed)  # 评估
    assert result.correct == 0  # 全错
    assert len(result.misses) == 1  # 一条错例