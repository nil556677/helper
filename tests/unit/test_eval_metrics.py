"""P5 memory 评测：行为通道金标判分 + 报告汇总。"""

from __future__ import annotations  # 延迟值注解

from app.memory.eval_metrics import default_cases, run, score_verbosity_cases, summary  # 被测


def test_default_cases_all_pass() -> None:  # 内置金标样例全过
    results = score_verbosity_cases(default_cases())  # 判分
    assert all(ok for ok, _r in results)  # 全过
    s = summary(results)  # 汇总
    assert s["total"] == "4"  # 4 例
    assert s["failed"] == "0"  # 无失败


def test_expect_none_matches() -> None:  # 样本不足 → expect None 判对
    results = score_verbosity_cases([{"question": "样本不足", "size": 60, "count": 3, "expect": None}])  # 单例
    ok, reason = results[0]  # 拆解
    assert ok  # 通过
    assert "None" in reason  # 期望/得到都是 None


def test_run_report_shape() -> None:  # run 报告形状
    report = run(fake=True)  # 跑
    assert "summary" in report  # 摘要
    assert report["summary"]["passed"] == "4"  # 全过
