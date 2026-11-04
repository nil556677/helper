"""E 偏反/边界用例：成功×失败、空值、异常、网络重连——对正用例的补充。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 跑异步
from pathlib import Path  # 路径
from typing import Any  # 任意值

import httpx  # 网络错误
import pytest  # 测试框架

from app.agent import (
    checkpoint,  # 检查点
    reconcile,  # 对账
)
from app.agent.run import RunSession  # 运行会话
from app.core.db import schema_v1  # 建表
from app.core.db.connection import connect, get_connection  # 连接
from app.services import attachments  # 附件


@pytest.fixture(autouse=True)
def _db(fake_root):  # 隔离库（部分用例用）
    conn = connect()  # 连接
    try:  # 建表
        schema_v1.initialize(conn)  # 初始化
    finally:  # 关闭
        conn.close()  # 关


# ---------- attachments.load_text 正反 ----------

def test_load_text_reads_existing_file(fake_root) -> None:  # 正：可读文件
    f = Path(fake_root) / "a.txt"  # 路径
    f.write_text("你好，世界。", encoding="utf-8")  # 写
    item = {"path": str(f)}  # 元数据
    assert attachments.load_text(item) == "你好，世界。"  # 读回


def test_load_text_missing_path_returns_empty() -> None:  # 反：无 path
    assert attachments.load_text({}) == ""  # 空
    assert attachments.load_text({"path": ""}) == ""  # 空路径


def test_load_text_missing_file_returns_empty() -> None:  # 反：文件不存在
    assert attachments.load_text({"path": str(Path(".tmp") / "no-such.txt")}) == ""  # 空


# ---------- 统一结果契约（core.Result）字段完整性 ----------

def test_envelopes_always_have_full_keys() -> None:  # 反：字段齐全
    from app.core.result import Result  # 唯一结果契约

    s = Result(ok=True, code="ok")  # 成功
    e = Result(ok=False, code="protocol.x", message="m")  # 错误
    for ev in (s, e):  # 两者
        data = ev.model_dump()  # 转字典
        for key in ("ok", "code", "message", "retryable", "retry_after_s", "data", "meta"):  # 必含键
            assert key in data  # 在
    assert s.code == "ok" and e.code == "protocol.x"  # code 语义


# ---------- reconcile 反/边界 ----------

def test_mark_interrupted_empty_is_noop(fake_root) -> None:  # 反：空列表
    assert reconcile.mark_interrupted([]) == 0  # 0 改动


def test_stale_excludes_future_started_at(fake_root) -> None:  # 反：未来时间不视为残留
    import time  # 时间戳

    get_connection().execute(  # 插一个未来 run
        "INSERT INTO trace_runs(id, question, started_at, status) VALUES (?,?,?,?)",  # 语句
        ("future", "q", time.time() + 300, "running"),  # 未来
    )
    assert reconcile.stale_run_ids() == []  # 不认为是残留


def test_resume_decision_empty_status_is_interrupted() -> None:  # 反：空串
    assert reconcile.resume_decision("", has_checkpoint=True) == "interrupted"  # 无终态


# ---------- checkpoint.prune 空/安全 ----------

def test_prune_on_empty_db_is_safe(fake_root) -> None:  # 反：空库不报错
    removed = checkpoint.prune(keep=10, path=str(Path(fake_root) / "empty.sqlite"))  # 空库
    assert removed == 0  # 0


# ---------- run_session 断线重连（反：首次网络错误重试成功） ----------

class _Flaky:  # 假客户端：第一次抛网络错，第二次成功
    def __init__(self) -> None:  # 构造
        self.attempt = 0  # 尝试次数

    async def start(self, *_a: Any, **_kw: Any) -> str:  # 启动
        return "run-1"  # id

    async def events(self, run_id: str, *, last_event_id: str = "") -> Any:  # 事件流
        del run_id, last_event_id  # 不用
        self.attempt += 1  # 计数
        if self.attempt == 1:  # 第一次
            raise httpx.ConnectError("conn refused")  # 网络错误
        yield 1, "ok-event"  # 第二次成功

    async def cancel(self, *_a: Any, **_kw: Any) -> None:  # 取消
        return None  # 无


def test_run_session_reconnects_after_network_error() -> None:  # 反：断线续传
    async def main() -> list[Any]:  # 异步主体
        session = await RunSession.create(_Flaky(), None)  # 启动（内部起 _feed）
        out: list[Any] = []  # 收集
        while True:  # 消费
            ev = await session.events.get()  # 取
            if ev is RunSession.SENTINEL:  # 结束
                break  # 停
            out.append(ev)  # 记
        return out  # 返回

    assert asyncio.run(main()) == ["ok-event"]  # 重连后收到事件
