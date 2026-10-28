"""M1 引用校验：L1 编号、L3 溯源、以及"无引用不展示"的端到端行为。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 事件循环

from app.agent.context import RunContext  # 上下文
from app.agent.events import EventType  # 事件类型
from app.agent.fsm.states import RunEvent  # 状态事件
from app.agent.middleware.base import ModelResponse  # 响应形状
from app.agent.nodes.answer import node as answer_mod  # 被测节点
from app.rag.verify import TraceSnapshot, verify, verify_l1  # 校验

EVIDENCE = [  # 一条能溯源到证据
    {  # 证据
        "index": 1,  # 序号
        "source_name": "合同.docx",  # 来源
        "origin": "第 1 页",  # 出处
        "score": 0.5,  # 分数
        "score_kind": "rrf",  # 语义
        "text": "违约金上限三十万元。",  # 正文
        "snippet": "违约金上限",  # 命中段
        "doc_id": 3,  # 文档
        "chunk_id": 11,  # child
        "parent_id": 7,  # 父块
        "version": 2,  # 版本
        "text_hash": "hash-3",  # 指纹
    }  # 证据结束
]  # 列表结束


def _snapshot(**overrides) -> TraceSnapshot:  # 造快照
    """默认：一切正常。"""
    data = {  # 默认值
        "documents": {3: {"text_hash": "hash-3", "stored_path": "contract.docx"}},  # 文档
        "chunks": {11: {"id": 11}},  # 片段
        "active": {3: 2},  # active 版本
        "files": {3: True},  # 文件在
    }  # 默认结束
    data.update(overrides)  # 覆盖
    return TraceSnapshot(**data)  # 快照


def _lookup(snapshot: TraceSnapshot):  # 假查询
    """忽略入参，永远返回给定快照。"""
    def call(_evidence):  # 签名对齐
        return snapshot  # 返回
    return call  # 返回函数


# ---------------- L1 ----------------

def test_l1_valid_keeps_text() -> None:  # L1-01 正
    """合法角标原样保留。"""
    text, valid, invalid = verify_l1("答案[1]", 3)  # 校验
    assert text == "答案[1]"  # 不动
    assert valid == [1]  # 有效
    assert invalid == []  # 无非法


def test_l1_out_of_range_is_removed() -> None:  # L1-02 反
    """越界角标要删掉，并记进非法列表。"""
    text, valid, invalid = verify_l1("答案[5]", 3)  # 校验
    assert "[5]" not in text  # 已删
    assert invalid == [5]  # 记非法
    assert valid == []  # 没有有效


def test_l1_zero_is_invalid() -> None:  # L1-03 反
    """`[0]` 不算引用。"""
    _, valid, invalid = verify_l1("答案[0]", 3)  # 校验
    assert invalid == [0]  # 非法
    assert valid == []  # 无效


def test_l1_fullwidth_is_recognized() -> None:  # L1-04 反（异形写法）
    """全角【1】按引用处理，不再当作"未标注"。"""
    _, valid, _ = verify_l1("答案【1】", 3)  # 校验
    assert valid == [1]  # 认出来了


def test_l1_ignores_code_block() -> None:  # L1-05 反
    """代码块里的 [1] 是示例，不是引用。"""
    text, valid, _ = verify_l1("看代码：\n```py\nx = a[1]\n```\n", 3)  # 校验
    assert valid == []  # 不认
    assert "a[1]" in text  # 代码原样保留


def test_l1_duplicate_is_deduped() -> None:  # L1-06 反
    """重复引用只留一个。"""
    text, valid, _ = verify_l1("答案[1][1]", 3)  # 校验
    assert valid == [1]  # 只记一次
    assert text.count("[1]") == 1  # 文本里只剩一个


# ---------------- L3 ----------------

def test_l3_all_checks_pass() -> None:  # L3-01 正
    """chunk/文档/版本/指纹/文件全在 → 有效。"""
    result = verify("正文[1]", EVIDENCE, lookup=_lookup(_snapshot()))  # 校验
    assert result.valid == [1]  # 有效
    assert result.missing == []  # 无失效
    assert result.passed is True  # 通过


def test_l3_chunk_deleted() -> None:  # L3-02 反
    """片段被删 → 失效。"""
    result = verify("正文[1]", EVIDENCE, lookup=_lookup(_snapshot(chunks={})))  # 校验
    assert result.missing == [1]  # 失效
    assert "[1]" not in result.text  # 角标被删


def test_l3_version_changed() -> None:  # L3-03 反
    """资料被重新导入、版本变了 → 失效。"""
    result = verify("正文[1]", EVIDENCE, lookup=_lookup(_snapshot(active={3: 9})))  # 校验
    assert result.missing == [1]  # 失效


def test_l3_hash_changed() -> None:  # L3-04 反
    """内容被替换（指纹变了）→ 失效。"""
    snapshot = _snapshot(documents={3: {"text_hash": "new-hash", "stored_path": "c.docx"}})  # 换指纹
    result = verify("正文[1]", EVIDENCE, lookup=_lookup(snapshot))  # 校验
    assert result.missing == [1]  # 失效


def test_l3_source_file_deleted() -> None:  # L3-05 反
    """源文件被手工删除 → 失效。"""
    result = verify("正文[1]", EVIDENCE, lookup=_lookup(_snapshot(files={3: False})))  # 校验
    assert result.missing == [1]  # 失效


def test_l3_all_missing_fails() -> None:  # L3-06 反
    """全部失效 → 不通过，且没有有效引用。"""
    result = verify("正文[1]", EVIDENCE, lookup=_lookup(_snapshot(documents={})))  # 校验
    assert result.valid == []  # 无有效
    assert result.passed is False  # 不通过


def test_lookup_failure_does_not_fail_answer() -> None:  # L3-07 反（降级）
    """查库异常时不能把答案判死，只记原因。"""
    def boom(_evidence):  # 假故障
        raise RuntimeError("库挂了")  # 抛
    result = verify("正文[1]", EVIDENCE, lookup=boom)  # 校验
    assert result.valid == [1]  # 认作"暂时查不到"
    assert "lookup_failed" in result.reasons  # 有原因


# ---------------- 端到端 ----------------

class _Chunk:  # 假 chunk
    """answer 节点只读 content / additional_kwargs / usage_metadata。"""

    def __init__(self, text: str) -> None:  # 构造
        self.content = text  # 正文
        self.additional_kwargs: dict = {}  # 额外
        self.usage_metadata = None  # 用量


class _StreamChain:  # 假链（流式）
    """忽略 call，直接返回一段字符串流（answer 节点只认 str）。"""

    def __init__(self, text: str) -> None:  # 构造
        self.text = text  # 文本

    async def model_call(self, ctx, request, call):  # 调模型
        """返回流。"""
        async def tokens():  # 生成器
            """一次吐出全部。"""
            yield self.text  # 一段纯文本

        return ModelResponse(stream=tokens())  # 流


def _ctx() -> RunContext:  # 造上下文
    """进入 GENERATING。"""
    ctx = RunContext(run_id="r", question="q")  # 新建
    ctx.transition(RunEvent.START)  # 开始
    ctx.transition(RunEvent.CONTEXT_LOADED)  # 上下文
    ctx.transition(RunEvent.RETRIEVAL_DONE)  # 有证据
    return ctx  # 返回


def _run(monkeypatch, text: str, snapshot: TraceSnapshot) -> tuple[dict, list]:  # 跑节点
    """替换链与查询，收集事件。"""
    monkeypatch.setattr(answer_mod, "_CHAIN", _StreamChain(text))  # 假链
    monkeypatch.setattr(answer_mod, "store_lookup", _lookup(snapshot))  # 假查询
    ctx = _ctx()  # 上下文
    events: list = []  # 事件
    original = ctx.emit  # 原出口
    ctx.emit = lambda event: (events.append(event), original(event))  # 记录 + 转发
    result = asyncio.run(  # 跑
        answer_mod.answer_node({"question": "q", "evidence": EVIDENCE}, {"configurable": {"run_ctx": ctx}})  # 调用
    )  # 结束
    return result, events  # 返回


def test_e2e_valid_citation_passes(fake_root, monkeypatch) -> None:  # E-01 正
    """有能溯源的引用 → 正常展示，fragments 标已验证。"""
    result, events = _run(monkeypatch, "违约金上限是 30 万[1]。", _snapshot())  # 跑
    assert result["answer"] == "违约金上限是 30 万[1]。"  # 正文不变
    assert result["fragments"][0]["verified"] is True  # 已验证
    assert any(event.type is EventType.VERIFY and event.payload.passed for event in events)  # 有通过事件
    assert not any(event.type is EventType.REPLACE for event in events)  # 没有替换


def test_e2e_no_citation_falls_back(fake_root, monkeypatch) -> None:  # E-02 反
    """模型一个引用都没给 → 不展示为答案，换成资料原文。"""
    result, events = _run(monkeypatch, "违约金上限是三十万。", _snapshot())  # 跑
    assert "违约金上限三十万元" in result["answer"]  # 已换成原文
    assert any(event.type is EventType.REPLACE for event in events)  # 发过替换
    assert result["note"].startswith("模型没给出能溯源的引用")  # 说明


def test_e2e_missing_citation_is_removed(fake_root, monkeypatch) -> None:  # E-03 反
    """部分引用指向已删资料 → 只移除那个角标，正文其余保留。"""
    second = {**EVIDENCE[0], "index": 2, "doc_id": 4, "chunk_id": 12, "text_hash": "hash-4"}  # 第二条证据
    evidence = [EVIDENCE[0], second]  # 两条
    monkeypatch.setattr(answer_mod, "_CHAIN", _StreamChain("违约金上限 30 万[1]，另见[2]。"))  # 假链
    monkeypatch.setattr(answer_mod, "store_lookup", _lookup(_snapshot()))  # 只有 doc 3 存在
    ctx = _ctx()  # 上下文
    events: list = []  # 事件
    original = ctx.emit  # 原出口
    ctx.emit = lambda event: (events.append(event), original(event))  # 记录
    result = asyncio.run(  # 跑
        answer_mod.answer_node({"question": "q", "evidence": evidence}, {"configurable": {"run_ctx": ctx}})  # 调用
    )  # 结束
    assert "[2]" not in result["answer"]  # 失效角标被删
    assert "[1]" in result["answer"]  # 有效角标保留
    assert "30 万" in result["answer"]  # 正文保留
    assert any(event.type is EventType.VERIFY and event.payload.missing == [2] for event in events)  # 有失效
    assert any(item.get("verified") is False for item in result["fragments"])  # 引用区能标"不可用"


def test_e2e_all_missing_falls_back(fake_root, monkeypatch) -> None:  # E-04 反（全部失效）
    """引用的资料全没了 → 视为不合格，降级为资料原文。"""
    result, events = _run(monkeypatch, "违约金上限是 30 万[1]。", _snapshot(documents={}))  # 跑
    assert "违约金上限三十万元" in result["answer"]  # 已换成原文
    assert any(event.type is EventType.REPLACE for event in events)  # 发过替换
