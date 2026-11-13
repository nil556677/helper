"""M3 前半：审批策略 + 限额纯函数。"""

from __future__ import annotations  # 延迟值注解

from types import SimpleNamespace  # 假描述符

from app.agent.contracts.tools import Mutation  # 变更画像
from app.agent.react import MAX_PER_TOOL, RECURSION_LIMIT, TOKEN_BUDGET_PER_REQUEST, should_require_approval  # 被测


def _desc(approval="none", mutation=Mutation.READ_ONLY):  # 假描述符
    return SimpleNamespace(approval=approval, mutation=mutation)  # 描述符


def test_manual_requires() -> None:  # manual 必经用户确认
    assert should_require_approval(_desc(approval="manual")) == (True, "需用户确认后执行")  # 需确认


def test_none_readonly_free() -> None:  # 无审批且只读 → 放行
    assert should_require_approval(_desc()) == (False, "")  # 不审


def test_none_irreversible_write() -> None:  # 不可逆写即使 approval=none 也审
    assert should_require_approval(_desc(approval="none", mutation=Mutation.NON_IDEMPOTENT_WRITE))[0] is True  # 审


def test_auto_with_threshold() -> None:  # auto：超阈值才审
    desc = _desc(approval="auto")  # auto
    assert should_require_approval(desc, args={"amount": 100}, threshold=50)[0] is True  # 超 → 审
    assert should_require_approval(desc, args={"amount": 10}, threshold=50)[0] is False  # 未超 → 放行


def test_limits_positive() -> None:  # 限额为正
    assert MAX_PER_TOOL >= 1 and RECURSION_LIMIT >= 1 and TOKEN_BUDGET_PER_REQUEST > 0  # 显式配置