"""契约层：事件协议、命名、id 唯一性、序列校验、状态合并。"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 快照对比
import types  # UnionType 判断
import typing  # Any 特殊值
from pathlib import Path  # 快照文件路径
from typing import Any, get_args, get_origin  # 类型内省

import pytest  # 测试框架
from pydantic import ValidationError  # 校验异常
from pydantic_core import PydanticUndefined  # 无默认值标记

from app.agent.contracts import (  # 被测对象
    GROUP_LABELS,  # 分组名
    GROUP_ORDER,  # 分组顺序
    PAYLOAD_BY_TYPE,  # kind → 载荷模型
    TERMINAL_TYPES,  # 终态集合
    DuplicateIdError,  # 重复 id 异常
    EventEnvelope,  # 事件信封
    EventType,  # 事件类型
    IdRegistry,  # id 注册表
    Namer,  # 命名器
    from_dict,  # 反序列化
    make,  # 构造
    merge_by_id,  # 按 id 合并
    merge_usage,  # 用量合并
    to_dict,  # 序列化
    validate_sequence,  # 序列校验
)  # 导入结束


def test_namer_node_id_from_location() -> None:  # 节点 id 派生
    """目录+文件+方法，不手写拼接。"""
    assert Namer.node_id("app.agent.nodes.rag.graph", "dense_node") == "nodes.rag.graph.dense_node"  # 派生
    assert Namer.relative_module("app.agent.nodes.rag.graph") == "nodes.rag.graph"  # 去前缀
    assert Namer.stage_id("nodes.rag.entry.rag_node", "rewrite") == "nodes.rag.entry.rag_node.rewrite"  # 步骤 id
    assert Namer.stage_id("nodes.rag.graph.dense_node") == "nodes.rag.graph.dense_node"  # 无短名


def test_registry_rejects_duplicate() -> None:  # 唯一性
    """重复 id 在构图期抛错。"""
    registry = IdRegistry()  # 新注册表
    registry.register("nodes.rag.graph.dense_node", "dense")  # 注册
    with pytest.raises(DuplicateIdError):  # 期望异常
        registry.register("nodes.rag.graph.dense_node", "dup")  # 重复
    assert registry.contains("nodes.rag.graph.dense_node")  # 已注册


def test_registry_claim_is_idempotent_per_source() -> None:  # 登记（可重复）
    """同一个来源位置每轮登记一次要放过；换一处来源才算真重复。"""
    registry = IdRegistry()  # 新注册表
    registry.claim("nodes.rag.graph.dense_node", "graph.py:43")  # 第一次
    registry.claim("nodes.rag.graph.dense_node", "graph.py:43")  # 同一处再来
    assert registry.source("nodes.rag.graph.dense_node") == "graph.py:43"  # 来源没变
    with pytest.raises(DuplicateIdError):  # 期望异常
        registry.claim("nodes.rag.graph.dense_node", "graph.py:99")  # 另一处撞车


def test_make_and_roundtrip() -> None:  # 构造与往返
    """事件可序列化、可还原。"""
    event = make(EventType.DELTA, text="你好", run_id="r1")  # 构造
    assert event.type is EventType.DELTA  # 类型
    assert event.payload.text == "你好"  # 载荷已收窄
    data = to_dict(event)  # 序列化
    assert data["type"] == "delta"  # 枚举转字符串
    restored = from_dict(data)  # 反序列化
    assert restored.payload.text == "你好"  # 还原


def test_from_dict_rejects_bad_payload() -> None:  # 载荷校验
    """delta 缺 text 直接失败。"""
    with pytest.raises(ValidationError):  # 期望异常
        from_dict({"event_id": "e1", "type": "delta", "payload": {}})  # 缺字段


def test_all_payload_shapes_construct() -> None:  # 全部载荷可构造
    """每种 type 的代表性字段名都可用。"""
    samples = [  # (类型, 字段)
        (EventType.ACK, {}),  # 已收到（run_id 在信封）
        (EventType.STATE, {"before": "a", "after": "b", "trigger": "start"}),  # 状态
        (EventType.STAGE, {"stage_id": "s", "status": "running", "title": "t", "duration_ms": 1, "group": "search"}),  # 步骤
        (EventType.BASIS, {"keywords": ["k"], "hit_counts": {"dense": 1}, "sources": []}),  # 依据
        (EventType.SETTINGS, {"model": "m", "temperature": 0.2, "system_prompt": "p"}),  # 设置
        (EventType.REASONING, {"text": "x"}),  # 思考
        (EventType.CONFLICT, {"message": "x"}),  # 冲突
        (EventType.APPROVAL, {"request_id": "r", "name": "n", "risk": "x"}),  # 审批
        (EventType.CONTEXT, {"turns": 1, "tokens": 2}),  # 上下文
        (EventType.PII, {"counts": {"phone": 1}}),  # 脱敏
        (EventType.TOOL, {"tool_name": "t", "status": "done"}),  # 工具
        (EventType.REPLACE, {"text": "x"}),  # 替换
        (EventType.CITATION, {"items": []}),  # 引用
        (EventType.USAGE, {"prompt_tokens": 1, "completion_tokens": 2}),  # 用量
        (EventType.NOTE, {"text": "x", "note_kind": "error"}),  # 说明
        (EventType.VERIFY, {"valid": [], "invalid": [], "missing": [], "unsupported": [], "coverage": 0.0, "passed": False, "reasons": []}),  # 校验
        (EventType.CANCELLED, {"reason": "user"}),  # 停止
        (EventType.ERROR, {"code": "c", "message": "m", "stage": "s"}),  # 失败
        (EventType.RETRY, {"attempt": 1, "max_attempts": 3, "error_kind": "rate_limit", "delay_ms": 800}),  # 重试
    ]  # 样本结束
    for event_type, fields in samples:  # 逐个构造
        event = make(event_type, **fields)  # 构造
        assert event.type is event_type  # 类型正确


def test_terminal_uniqueness_and_last() -> None:  # 终态唯一且最后
    """一个 run 只允许一个终态。"""
    done = make(EventType.DONE, ttft_ms=0, total_ms=1, run_id="r1")  # 完成
    delta = make(EventType.DELTA, text="x", run_id="r1")  # 增量
    validate_sequence([delta, done])  # 合法
    with pytest.raises(ValueError):  # 期望异常
        validate_sequence([done, delta])  # 终态不在最后
    with pytest.raises(ValueError):  # 期望异常
        validate_sequence([done, done])  # 两个终态


def test_merge_by_id_last_write_wins() -> None:  # 按 id 合并
    """后到覆盖先到，并保序。"""
    current = [{"id": "a", "status": "running"}, {"id": "b", "status": "running"}]  # 旧值
    incoming = [{"id": "a", "status": "done", "detail": "ok"}]  # 新值
    assert merge_by_id(current, incoming) == [  # 合并结果
        {"id": "a", "status": "done", "detail": "ok"},  # 覆盖
        {"id": "b", "status": "running"},  # 保留
    ]  # 断言结束


def test_merge_usage_accumulates_numbers() -> None:  # 用量合并
    """数值累加、字符串覆盖。"""
    result = merge_usage({"prompt_tokens": 10}, {"prompt_tokens": 5, "model": "x"})  # 合并
    assert result == {"prompt_tokens": 15, "model": "x"}  # 断言


def _type_sig(annotation: Any) -> str:  # 类型签名
    """把 annotation 压成稳定字符串：list[dict[str, Any]] / int | None 等。"""
    if annotation is typing.Any:  # 特殊的 Any
        return "Any"  # 输出
    if annotation is type(None):  # None 类型字面
        return "None"  # 输出
    origin = get_origin(annotation)  # 泛型的源
    if origin is types.UnionType:  # PEP 604 联合（int | None）
        return " | ".join(_type_sig(item) for item in get_args(annotation))  # 空格拼接
    if origin is None:  # 非泛型
        return getattr(annotation, "__name__", str(annotation))  # 类名
    inner = ", ".join(_type_sig(item) for item in get_args(annotation))  # 泛型参数
    return f"{getattr(origin, '__name__', str(origin))}[{inner}]"  # 泛型签名


def _field_sig(field: Any) -> str:  # 字段签名
    """类型 + 默认值（无默认 / =值 / =factory）。"""
    signature = _type_sig(field.annotation)  # 类型
    if field.default_factory is not None:  # 工厂默认
        return f"{signature}=factory"  # 标记
    if field.default is not PydanticUndefined:  # 有静态默认
        return f"{signature}={field.default!r}"  # 带上
    return signature  # 无默认


def _schema() -> dict[str, Any]:  # 计算事件协议 schema
    """信封字段 + 全部 kind 的载荷 + 分组；专供快照测试比对。"""
    return {  # schema
        "schema_version": 1,  # 协议版本（只加不删）
        "event_types": [event.value for event in EventType],  # 全部 kind
        "terminal": sorted(event.value for event in TERMINAL_TYPES),  # 终态
        "envelope": {name: _field_sig(field) for name, field in EventEnvelope.model_fields.items()},  # 信封字段
        "kinds": {  # 每种事件的载荷字段
            event.value: {  # kind 名
                name: _field_sig(field)  # 字段签名
                for name, field in PAYLOAD_BY_TYPE[event].model_fields.items()  # 逐个字段
            }  # 载荷结束
            for event in EventType  # 逐个 kind
        },  # kinds 结束
        "stage_group_order": [group.value for group in GROUP_ORDER],  # 分组的展示顺序
        "stage_group_labels": {group.value: GROUP_LABELS[group] for group in GROUP_ORDER},  # 分组名
    }  # schema 结束


def test_event_schema_snapshot() -> None:  # schema 快照
    """事件协议必须和夹具逐字一致：改字段 / 加 kind 都要**故意**更新快照。

    夹具在 ``tests/unit/_events_schema_v1.json``。改协议时先跑本测试看差异，
    确认是想要的变更后重生成夹具（用 ``_schema()`` 的输出），而不是改代码绕过。
    """
    fixture = Path(__file__).parent / "_events_schema_v1.json"  # 快照文件
    assert _schema() == json.loads(fixture.read_text(encoding="utf-8"))  # 逐字比对
