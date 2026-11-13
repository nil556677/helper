"""P5 画像 LLM 抽取：profile_candidates 收敛 + extract_profile 降级安全。"""

from __future__ import annotations  # 延迟值注解

from app.agent.memory.llm_profile import ProfileExtraction, profile_candidates  # 被测


def test_profile_candidates_scalar_fields() -> None:  # 标量字段收敛
    data = ProfileExtraction(verbosity="short", language="zh")  # 两个非空
    cands = profile_candidates(data)  # 收敛
    keys = {c["key"] for c in cands}  # 键
    assert "verbosity" in keys  # 回答长度
    assert "language" in keys  # 语言
    assert all(c["source"] == "inferred" for c in cands)  # 来源为推断


def test_profile_candidates_list_fields() -> None:  # 列表字段收敛
    data = ProfileExtraction(banned_phrases=["别叫亲"], terms=["单子=订单"])  # 列表
    cands = profile_candidates(data)  # 收敛
    keys = {c["key"] for c in cands}  # 键
    assert "banned_phrases" in keys  # 禁用表达
    assert "terms" in keys  # 术语
    assert any(c["key"] == "banned_phrases" and c["value"] == ["别叫亲"] for c in cands)  # 值保留


def test_profile_candidates_empty() -> None:  # 空数据 → 空候选
    assert profile_candidates(ProfileExtraction()) == []  # 全部缺省


def test_extract_profile_returns_list(monkeypatch) -> None:  # 模型不可用 → 安全空
    import app.agent.memory.llm_profile as ext  # 被测

    monkeypatch.setattr(ext, "_avail", lambda *a, **k: False)  # 禁用模型
    assert ext.extract_profile("用户说一句") == []  # 降级空
    assert ext.extract_profile("", slot="llm") == []  # 空输入
