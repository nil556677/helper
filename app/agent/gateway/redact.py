"""敏感字段脱敏：落台账/日志前的字段级打码（名实相符的 `args_redacted`）。

当前最相关：`query_order`/`query_logistics` 的参数含手机号/运单号等 PII，审批台账
`approval_requests.args_redacted` 必须存脱敏后的值，而不是原样参数。通用按「字段名」
判定敏感，并递归处理嵌套 dict/list；非敏感字段原样保留。
"""

from __future__ import annotations  # 延迟值注解

from typing import Any  # 任意值

__all__ = ["SENSITIVE_NAMES", "redact_args"]  # 导出

#: 敏感字段名（按名命中即打码）；覆盖手机号/邮箱/卡号/证件/凭据类。
SENSITIVE_NAMES: frozenset[str] = frozenset(  # 敏感名集合
    {
        "phone", "mobile", "mobilephone", "tel", "telephone",  # 手机号/电话
        "email", "mail",  # 邮箱
        "credit_card", "card_no", "card_number", "pan",  # 卡号
        "id_card", "idcard", "id_no", "id_number",  # 证件号
        "password", "pwd", "passwd",  # 口令
        "token", "authorization", "api_key", "apikey", "secret", "access_key", "access_key_id",  # 凭据
        "tracking_no", "waybill_no", "logistics_no",  # 运单号（PII 性物流标识）
    }  # 集合元素
)  # 结束


def _mask(value: str) -> str:  # 单个字符串打码
    """保留头部一部分，其余星号：较长保前3后2，较短全星，控制信息量同时可部分对账。"""
    text = str(value)  # 转串
    if len(text) >= 8:  # 够长
        return f"{text[:3]}****{text[-2:]}"  # 前3后2
    if len(text) >= 4:  # 中等
        return f"{text[0]}***"  # 首字符 + 星
    return "*" if text else ""  # 短/空（空串不制造假信息）


def _looks_sensitive(name: str) -> bool:  # 字段名是否敏感
    """精确名命中，或小写后包含敏感子串（如 user_phone）。"""
    key = str(name or "").lower()  # 小写
    if key in SENSITIVE_NAMES:  # 精确命中
        return True  # 敏感
    return any(word in key for word in ("phone", "email", "card", "password", "token", "secret", "key"))  # 子串命中


def _redact_value(name: str, value: Any) -> Any:  # 递归脱敏一个值
    """dict/list 递归处理；敏感字段名的字符串值打码；其余原样返回。"""
    if isinstance(value, dict):  # 嵌套对象
        return {  # 逐键
            key: _redact_value(key, sub)  # 递归
            for key, sub in value.items()  # 每个键
        }  # 返回新 dict
    if isinstance(value, (list, tuple)):  # 嵌套列表
        return [_redact_value(name, item) for item in value]  # 逐元素（沿用父字段名）
    if isinstance(value, str) and _looks_sensitive(name):  # 字符串且字段敏感
        return _mask(value)  # 打码
    return value  # 原样


def redact_args(args: dict[str, Any] | None, *, tool_name: str = "") -> dict[str, Any]:  # 参数脱敏入口
    """返回脱敏后的参数 dict（不修改入参）；非敏感字段原样保留。

    `tool_name` 仅供将来按工具定制规则（当前不需要，保留占位）。
    """
    del tool_name  # 占位，避免未使用参数告警
    if isinstance(args, dict):  # 是 dict
        return {  # 逐键
            key: _redact_value(key, value)  # 递归脱敏
            for key, value in args.items()  # 每个键
        }  # 返回
    return {}  # 非 dict（None/异常）返回空