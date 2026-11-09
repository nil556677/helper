"""整合功能测试：新路由图上的聊天管道不变量（落库/事件/trace）与原回答路径。"""

from __future__ import annotations  # 类型注解延迟求值

from types import SimpleNamespace  # 假工具结果
from typing import Any  # 任意值

import pytest  # 测试框架

import app.agent.agent as agent_mod  # 图
import app.agent.nodes.dispatch.node as dispatch_mod  # dispatch（打桩工具）
from app.agent.context import RunContext  # 上下文
from app.agent.driver import drive  # 驱动器
from app.agent.events import EventType  # 事件
from app.core.db import schema_v1  # 建表
from app.core.db.connection import close_thread_connection, connect  # 连接


class _NoopTool:  # 打桩的检索工具
    name = "search_knowledge_base"  # 名

    async def ainvoke(self, args):  # 返回固定证据
        del args  # 不用
        return SimpleNamespace(  # 包络
            data={  # 数据
                "model_view": "#### 资料\n证据正文内容\n\n> 暂无",  # 编号文本
                "evidence": [  # 三条多源高分（避免质量信号升级）
                    {"index": 1, "text": "证据正文内容", "source_name": "资料", "origin": "第 1 段", "score": 0.9},  # 证据1
                    {"index": 2, "text": "补充内容", "source_name": "文档", "origin": "第 2 段", "score": 0.85},  # 证据2
                    {"index": 3, "text": "更多内容", "source_name": "FAQ", "origin": "第 3 段", "score": 0.8},  # 证据3
                ],  # 证据
                "gate": {"decision": "pass"},  # 门限
            }  # 数据结束
        )  # 返回


class _Sink:  # 事件出口
    def __init__(self) -> None:  # 构造
        self.events: list[Any] = []  # 事件

    def __call__(self, event: Any) -> None:  # 收
        self.events.append(event)  # 追加


@pytest.fixture(autouse=True)  # 独立库
def _db(fake_root):
    """建 v1 schema。"""
    conn = connect()  # 连接
    try:  # 建
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关
        conn.close()  # 关
    yield  # 交给用例
    close_thread_connection()  # 收尾


async def _drive(question: str, monkeypatch) -> tuple[Any, _Sink]:  # 跑一轮
    del monkeypatch  # 不再打桩 knowledge（检索只在 ReAct）
    graph = agent_mod.build_route_graph()  # 新图
    sink = _Sink()  # 事件
    ctx = RunContext(run_id="r", question=question, conversation_id="c1", emit=sink)  # 上下文
    initial = {"question": question, "conversation_id": "c1", "turns": [], "attachments": []}  # 初始
    result = await drive(graph, ctx, initial_state=initial)  # 驱动
    return result, sink  # 返回


async def test_pipeline_invariants_and_quick_answer(monkeypatch) -> None:  # 落库/事件/trace + 快捷路径
    async def to_quick(state, config):  # 打桩语义：direct → time（快捷无检索）
        del state, config  # 不用
        return {  # 更新
            "semantic_recall": {"decision": "direct", "candidates": [{"route": "time", "score": 0.99}], "top1": 0.99, "margin": 0.5},  # 召回
            "route": {"intent": "direct", "route": "time", "tools": ["current_time"], "decided_by": "embedding"},  # 决策
        }  # 返回

    monkeypatch.setattr(agent_mod, "semantic_node", to_quick)  # 直达 time
    result, sink = await _drive("现在几点", monkeypatch)  # 跑
    assert result.status == "done"  # 终态
    types = [event.type for event in sink.events]  # 事件类型
    assert types[0] is EventType.ACK  # 先回执
    assert types[-1] is EventType.DONE  # 终态在最后
    assert sum(1 for item in types if item is EventType.DONE) == 1  # 终态唯一
    assert str(result.state.get("answer") or "").startswith("现在是 ")  # 快捷作答


async def test_pipeline_miss_clarifies(monkeypatch) -> None:  # 无对应 → 澄清
    result, _sink = await _drive("帮我解释一段陌生产品代码的深层语义细节", monkeypatch)  # 跑
    assert result.status == "done"  # 终态
    assert str(result.state.get("answer") or "")  # 有澄清/拒绝话术