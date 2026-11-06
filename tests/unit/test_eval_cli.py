"""评估闭环：金标集加载 + 多策略对比 + CLI 出报告。"""

from __future__ import annotations  # 延迟求值注解

from pathlib import Path  # 临时目录

from app.agent.contracts.routing import RouteSpec  # 路由
from app.agent.routing.embeddings import load_or_build  # 索引
from app.agent.routing.eval import compare_policies, load_golden  # 评估
from app.agent.routing.eval_cli import main, run  # CLI
from app.agent.routing.policy import CANDIDATE_POLICY, DEFAULT_POLICY  # 策略
from app.agent.routing.rules.loader import load_rules  # 规则


def _embed(text: str) -> list[float]:  # 假编码
    if "时间" in text or "几点" in text:  # 时间
        return [1.0, 0.0]
    if "天气" in text:  # 天气
        return [0.0, 1.0]
    return [0.0, 0.0]  # 无关


_ROUTES = (RouteSpec(route_id="time", description="查询当前时间"), RouteSpec(route_id="weather", description="查询天气"))  # 路由


def test_load_golden_default() -> None:  # 默认金标集
    golden = load_golden()  # 载入
    assert len(golden) >= 6  # 有样本
    assert golden.get("现在几点") == "time"  # 期望
    assert golden.get("随便聊两句废话") is None  # 应未命中


def test_compare_policies(tmp_path: Path) -> None:  # 多策略对比
    rules = load_rules([Path(__file__).parent])  # 空规则
    index = load_or_build(_ROUTES, _embed, store_path=tmp_path / "r.sqlite", model_version="ev:3")  # 索引
    golden = {"现在几点": "time", "北京天气怎么样": "weather", "完全无关": None}  # 金标
    results = compare_policies(rules, index, golden, _embed, (DEFAULT_POLICY, CANDIDATE_POLICY))  # 对比
    assert set(results) == {"v1", "v2"}  # 两版
    assert all(result.total == 3 for result in results.values())  # 样本一致


def test_cli_runs_and_writes_report(tmp_path: Path) -> None:  # CLI 冒烟
    out = tmp_path / "report.json"  # 输出
    report = run(fake=True, out=out, store=tmp_path / "r.sqlite")  # 跑
    assert report["golden_size"] >= 6  # 样本
    assert set(report["policies"]) == {"v1", "v2"}  # 两版
    assert out.exists()  # 报告落盘
    assert main(["--fake", "--out", str(tmp_path / "r2.json"), "--store", str(tmp_path / "r2.sqlite")]) == 0  # CLI 返回码