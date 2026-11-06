"""L5 校验：引用角标 / 覆盖率 / PII 脱敏 / 节点。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.nodes.verify.node import verify_node  # 节点
from app.agent.routing.verify import mask_pii, strip_invalid, verify  # 纯函数


def _evidence() -> list[dict]:  # 两条证据
    return [{"index": 1, "text": "a"}, {"index": 2, "text": "b"}]  # 证据


def test_verify_pass() -> None:  # 通过
    result = verify("答案 [1] 补充 [2]", _evidence())  # 校验
    assert result.passed is True  # 通过
    assert result.valid == [1, 2]  # 合法
    assert result.coverage == 1.0  # 全覆盖


def test_verify_invalid_citation() -> None:  # 越界
    result = verify("答案 [5]", _evidence())  # 校验
    assert result.passed is False  # 不通过
    assert result.invalid == [5]  # 越界记录
    assert "invalid_citation" in result.reasons  # 原因


def test_verify_empty_and_no_citation() -> None:  # 空/无引用
    assert verify("", _evidence()).passed is False  # 空答案
    result = verify("没有引用的答案", _evidence())  # 无角标
    assert result.coverage == 0.0 and "no_citation" in result.reasons  # 无引用


def test_strip_invalid() -> None:  # 删越界
    assert strip_invalid("a [1] b [9]", 2) == "a [1] b "  # 9 删掉


def test_mask_pii() -> None:  # 脱敏
    text = "邮箱 a@b.com 手机 13800138000"  # 含 PII
    masked = mask_pii(text)  # 脱敏
    assert "a@b.com" not in masked and "13800138000" not in masked  # 都替换


def test_verify_node() -> None:  # 节点
    out = verify_node({"answer": "答案 [1] [9]", "fragments": _evidence()})  # 跑
    assert "[9]" not in out["answer"]  # 越界删
    assert out["verify"]["invalid"] == [9]  # 记录
    assert out["note"]  # 未通过 → 说明