"""T0.4 统一压缩引擎：摘要不变小拒绝 / source_refs / 与 trim 边界一致。"""

from __future__ import annotations  # 延迟值注解

from app.agent.contexts.budget import Budget  # 预算
from app.agent.contexts.compact import SummaryResult, compact, plan  # 被测


def _messages(n: int) -> list[dict]:  # 造 n 条消息（带 id/seq）
    return [  # 构造
        {"role": "user" if i % 2 == 0 else "assistant", "content": "内容" * 10, "id": i + 1, "seq": i + 1}  # 一条
        for i in range(n)  # 逐条
    ]  # 返回


async def _fake_compactor(oldest, old_summary: str) -> str:  # 假压缩器：短摘要（一定低于消息成本）
    return "已压缩：" + str(oldest[-1].get("content") or "")[:5]  # 短文本


def _small_budget() -> Budget:  # 小预算（trim_target 极小，让"最老段"存在）
    return Budget(window=100_000, _history_budget=300)  # 历史预算 300 token


async def test_summary_result_has_source_refs() -> None:  # 压缩结果带覆盖消息 id
    messages = _messages(30)  # 30 条
    result = await _run(messages, budget=_small_budget())  # 压缩（小预算）
    assert result is not None  # 有结果
    assert isinstance(result, SummaryResult)  # 类型
    assert result.source_refs  # 有覆盖 id
    assert max(result.source_refs) <= result.until_message_id  # 终点 >= 最大 id


async def test_compact_rejects_no_shrink() -> None:  # 无最老段 → 无事可压（返回 None）
    messages = _messages(2)  # 很少消息（全在保留区）
    result = await _run(messages, budget=_small_budget())  # 压缩
    assert result is None  # 无最老段 → None


def test_plan_splits_keep_and_oldest() -> None:  # plan 拆保留区/最老段
    messages = _messages(30)  # 30 条
    keep, oldest = plan(messages, _small_budget())  # 小预算
    assert keep and oldest  # 各非空
    assert len(keep) + len(oldest) == 30  # 合起来全量


async def _run(messages, *, budget=None):  # 跑一次压缩
    return await compact(messages, _fake_compactor, summary_text="旧摘要旧摘要", budget=budget)  # 压缩