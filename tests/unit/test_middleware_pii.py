"""中间件清理后的回归：脱敏工具是纯函数、默认链只挂 run 生命周期钩子。

背景：自研 `PIIMiddleware.wrap_model_call` / `MiddlewareChain.model_call` 从没接线
（模型调用走框架 AgentMiddleware），是误导性死代码。清理后：
- 脱敏能力由纯函数 `redact()` 承担（被 `app.agent.memory.manager` 复用）；
- 默认链只保留真正生效的 run 前/后钩子（Cancel）。
这里锁住这两个事实，防止以后又把"不会跑的自动包裹"加回来。
"""

from __future__ import annotations  # 延迟值注解

from app.agent.middleware.pii import redact  # 脱敏纯函数（被 memory 复用）


def test_redact_masks_known_pii() -> None:  # 脱敏纯函数
    """身份证/卡号/手机号/邮箱都被掩码，且统计类型与次数。"""
    text, counts = redact("拨 13800138000，卡 1234567890123456，邮箱 a@b.com")  # 输入
    assert "13800138000" not in text  # 手机号洗掉
    assert "1234567890123456" not in text  # 卡号洗掉（16 位，避免先被 18 位身份证规则吃掉）
    assert "a@b.com" not in text  # 邮箱洗掉
    assert counts["phone"] == 1  # 手机号 1 处
    assert counts["bank_card"] == 1  # 卡号 1 处
    assert counts["email"] == 1  # 邮箱 1 处


def test_default_chain_is_lifecycle_only() -> None:  # 默认链只挂生命周期钩子
    """自研中间件链不再放"不会跑的 PII 包裹"，只剩真正生效的 run 钩子中间件。"""
    from app.agent.middleware import default_chain  # 默认链

    items = default_chain()._items  # 链内容
    assert [type(item).__name__ for item in items] == ["CancelMiddleware"]  # 只此一个
