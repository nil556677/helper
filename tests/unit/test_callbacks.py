"""追踪回调：span 起止、TTFT、fail-open（类 D · D2）。"""

from __future__ import annotations  # 类型注解延迟求值

from app.agent import callbacks as cb  # 被测模块


class _Session:  # 假 trace 会话
    run_id = "r1"  # 运行 id
    _stack: tuple = ()  # 节点 span 栈（空即可）


def test_callbacks_record_llm_span(monkeypatch) -> None:  # LLM span
    """llm 起止写 span，token 用量进 output，TTFT 被记录。"""
    added: list[tuple] = []  # 开 span 记录
    finished: list[tuple] = []  # 关 span 记录

    monkeypatch.setattr(cb, "current_trace", lambda: _Session())  # 注入会话
    monkeypatch.setattr(  # 记录 add_span
        cb.trace_store, "add_span", lambda run, name, kind, **kw: (added.append((name, kind, kw)) or len(added))
    )
    monkeypatch.setattr(  # 记录 finish_span
        cb.trace_store, "finish_span", lambda span, **kw: finished.append((span, kw))
    )
    handler = cb.TraceCallbacks()  # 回调
    handler.on_chat_model_start({"name": "chat"}, [["m"]], run_id="x1")  # 开始
    handler.on_llm_new_token("t", run_id="x1")  # 首 token
    handler.on_llm_end(  # 结束
        type("R", (), {"llm_output": {"token_usage": {"prompt_tokens": 2, "completion_tokens": 3}}})(),  # 响应
        run_id="x1",  # 对应开始
    )
    assert added and added[0][0] == "chat" and added[0][1] == "llm"  # 名称/类别
    assert finished and finished[0][1]["status"] == "ok"  # 正常结束
    assert finished[0][1]["output"]["prompt_tokens"] == 2  # 用量
    assert "ttft_ms" in finished[0][1]["output"]  # TTFT 被记


def test_callbacks_fail_open(monkeypatch) -> None:  # fail-open
    """落库抛错时回调不许把业务搞挂。"""
    monkeypatch.setattr(cb, "current_trace", lambda: _Session())  # 注入会话

    def boom(*_args, **_kwargs):  # 炸
        raise RuntimeError("db down")  # 抛

    monkeypatch.setattr(cb.trace_store, "add_span", boom)  # add 失败
    handler = cb.TraceCallbacks()  # 回调
    handler.on_chain_start({"name": "c"}, {}, run_id="x2")  # 不应抛
    handler.on_chain_end({}, run_id="x2")  # 不应抛
