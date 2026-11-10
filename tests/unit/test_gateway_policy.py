"""工具网关测试：权限判定 / 租户匹配 / 字段脱敏 / 上下文解析。"""

from __future__ import annotations  # 延迟值注解

from types import SimpleNamespace  # 假描述符

from app.agent.contracts import ErrorCode  # 错误码
from app.agent.gateway.policy import check, enforce_tenant  # 权限/租户判定
from app.agent.gateway.principals import PolicyContext  # 上下文
from app.agent.gateway.redact import redact_args  # 脱敏


def _desc(*permissions: str) -> SimpleNamespace:  # 假描述符
    return SimpleNamespace(permissions=tuple(permissions))  # 带权限标签


def _ctx(*permissions: str) -> PolicyContext:  # 封装上下文
    return PolicyContext(permissions=frozenset(permissions))  # 权限集


def test_empty_permissions_allows() -> None:  # 工具无权限要求 → 放行
    assert check(_desc(), _ctx()) == (True, "")  # 都空放行
    assert check(_desc(), _ctx("read")) == (True, "")  # 调用方有权限也不受工具限制
    assert check(_desc("other"), _ctx()) == (False, str(ErrorCode.PROTOCOL_PERMISSION))  # 声明了权限就必须有


def test_required_permission_must_satisfy() -> None:  # 非空权限须全匹配
    assert check(_desc("order:read"), _ctx("order:read")) == (True, "")  # 满足
    assert check(_desc("order:read"), _ctx()) == (False, str(ErrorCode.PROTOCOL_PERMISSION))  # 缺权限
    assert check(_desc("a", "b"), _ctx("a")) == (False, str(ErrorCode.PROTOCOL_PERMISSION))  # 缺任一


def test_descriptor_no_permissions_attr_allows() -> None:  # 无 permissions 属性 → 放行
    assert check(SimpleNamespace(), _ctx("x")) == (True, "")  # 视为无要求


def test_enforce_tenant() -> None:  # 租户匹配
    assert enforce_tenant("t1", PolicyContext(tenant_id="t1")) == (True, "")  # 一致
    assert enforce_tenant("t1", PolicyContext(tenant_id="t2")) == (False, str(ErrorCode.PROTOCOL_PERMISSION))  # 越权
    assert enforce_tenant("t1", PolicyContext()) == (True, "")  # 未绑定租户放行


def test_redact_sensitive_fields() -> None:  # 手机号/凭据字段打码
    out = redact_args({"phone": "13800138000", "order_id": "123", "note": "ok"})  # 混合参数
    assert out["phone"].startswith("138") and "*" in out["phone"]  # 手机号已打码
    assert out["phone"] != "13800138000"  # 非原文
    assert out["order_id"] == "123"  # 非敏感字段不动
    assert out["note"] == "ok"  # 普通字段不动


def test_redact_recursive_and_nested() -> None:  # 嵌套结构递归脱敏
    out = redact_args({"contact": {"email": "a@b.com"}, "items": [{"tracking_no": "SF1234567890"}]})  # 嵌套
    assert "*" in out["contact"]["email"] and out["contact"]["email"] != "a@b.com"  # 嵌套邮箱打码
    assert "****" in out["items"][0]["tracking_no"]  # 列表内物流单号打码
    assert out["contact"]["email"] != "a@b.com"  # 非原文


def test_redact_non_dict_returns_empty() -> None:  # 非 dict 入参返回空
    assert redact_args(None) == {}  # 空
    assert redact_args("x") == {}  # 空


def test_context_from_state_no_data_is_empty() -> None:  # 无 context → 空上下文
    ctx = PolicyContext.from_state({})  # 空状态
    assert ctx.permissions == frozenset()  # 无权限
    assert ctx.tenant_id == ""  # 无租户


def test_context_from_access_dict() -> None:  # 从 state["access"] 提取
    state = {"access": {"user_id": "u", "tenant_id": "t", "roles": ["r"], "permissions": ["order:read"]}}  # 完整上下文
    ctx = PolicyContext.from_state(state)  # 解析
    assert ctx.tenant_id == "t"  # 租户
    assert ctx.permissions == frozenset({"order:read"})  # 权限
    assert ctx.roles == frozenset({"r"})  # 角色