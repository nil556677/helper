"""M5 评测集：装载、正反判分、以及"必须同时有正反例"的护栏。"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 造临时评测集
from pathlib import Path  # 路径

import pytest  # 测试框架

from app.rag import evalset  # 被测


def test_default_cases_load_and_have_both_sides() -> None:  # 正
    """默认评测集能装载，且正反例都有（只有正例等于没测拒答）。"""
    cases = evalset.load_cases()  # 装载
    assert len(cases) >= 10  # 够用
    assert any(case["expect"] == evalset.ANSWER for case in cases)  # 有该答
    assert any(case["expect"] == evalset.REJECT for case in cases)  # 有该拒
    assert len({case["id"] for case in cases}) == len(cases)  # id 不重复


def test_load_cases_skips_bad_entries(tmp_path: Path) -> None:  # 反
    """缺 question 的条目被跳过，而不是让整份评测集炸掉。"""
    path = tmp_path / "cases.json"  # 临时文件
    path.write_text(json.dumps([{"id": "x"}, {"id": "y", "question": "问题"}]), encoding="utf-8")  # 一条坏
    cases = evalset.load_cases(path)  # 装载
    assert len(cases) == 1  # 只剩好的
    assert cases[0]["expect"] == evalset.ANSWER  # 默认"该答"


def test_score_answer_case_passes() -> None:  # 正
    """该答 + 判决 PASS + 有证据 → 通过。"""
    case = {"question": "违约金上限", "expect": "answer", "must_contain": ["违约金"]}  # 用例
    ok, reason = evalset.score_case(case, {"decision": "pass"}, [{"text": "违约金上限三十万"}])  # 判分
    assert ok is True  # 通过
    assert reason == "answered"  # 原因


def test_score_answer_case_rejects_when_gate_says_reject() -> None:  # 反
    """该答却被门限拒了 → 失败（误拒）。"""
    case = {"question": "违约金上限", "expect": "answer"}  # 用例
    ok, reason = evalset.score_case(case, {"decision": "reject"}, [])  # 判分
    assert ok is False  # 失败
    assert "应答未答" in reason  # 原因


def test_score_answer_case_fails_without_evidence() -> None:  # 反
    """判决通过但证据为空 → 失败（不能只看判决）。"""
    case = {"question": "违约金上限", "expect": "answer"}  # 用例
    ok, reason = evalset.score_case(case, {"decision": "pass"}, [])  # 判分
    assert ok is False  # 失败
    assert "没有证据" in reason  # 原因


def test_score_answer_case_fails_on_missing_content() -> None:  # 反
    """证据里没有关键内容 → 失败（防止"答错资料"也通过）。"""
    case = {"question": "违约金上限", "expect": "answer", "must_contain": ["三十万"]}  # 用例
    ok, reason = evalset.score_case(case, {"decision": "pass"}, [{"text": "服务费按月支付"}])  # 判分
    assert ok is False  # 失败
    assert "三十万" in reason  # 原因


def test_score_reject_case_passes() -> None:  # 正
    """该拒 + 判决 REJECT → 通过。"""
    ok, reason = evalset.score_case({"question": "股票代码", "expect": "reject"}, {"decision": "reject"}, [])  # 判分
    assert ok is True  # 通过
    assert reason == "rejected"  # 原因


def test_score_reject_case_fails_when_answered() -> None:  # 反
    """该拒却答了 → 失败（误答，这是硬指标）。"""
    ok, reason = evalset.score_case({"question": "股票代码", "expect": "reject"}, {"decision": "pass"}, [{"text": "x"}])  # 判分
    assert ok is False  # 失败
    assert "应拒未拒" in reason  # 原因


def test_summarize() -> None:  # 汇总
    """总数/通过/失败。"""
    assert evalset.summarize([(True, ""), (False, "")]) == {"total": 2, "passed": 1, "failed": 1}  # 汇总


def test_default_fixture_is_valid_json() -> None:  # 格式
    """评测集必须是合法 JSON 数组（脚本 --dry-run 的前提）。"""
    raw = json.loads(evalset.DEFAULT_CASES.read_text(encoding="utf-8"))  # 解析
    assert isinstance(raw, list)  # 数组
    assert all(isinstance(item, dict) for item in raw)  # 元素是对象


@pytest.mark.parametrize("case_id", ["A-01", "R-01"])  # 抽查两条
def test_default_cases_have_notes(case_id: str) -> None:  # 正
    """每条都写清楚"为什么该答/该拒"，否则以后没人敢改。"""
    cases = {case["id"]: case for case in evalset.load_cases()}  # 建索引
    assert cases[case_id].get("note")  # 有说明
