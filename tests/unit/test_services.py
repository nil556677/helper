"""服务进程端口汇总（``port_summary``）：启动日志里一眼看清各进程端口。"""

from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 类型标注

import pytest  # 测试装置

from app.core.proc import services as proc_services  # 被测


def test_port_summary_reports_ui_and_worker_rpc(
    fake_root: Path,  # 隔离目录（worker.port 写在假数据目录）
    monkeypatch: pytest.MonkeyPatch,  # 打桩
) -> None:
    """嵌入式默认：报告 UI 与 worker 对话 RPC 端口，本机 Qdrant 为 0。"""
    from app.core import paths  # 数据目录

    monkeypatch.setattr(  # 不碰真实子进程/网络
        proc_services,  # 模块
        "status",  # 被测函数读取的状态
        lambda: {  # 假状态
            "url": "",  # 嵌入式（无 vector.url）
            "port": 6333,  # Qdrant 默认口（不适用）
            "qdrant": {"local": False, "pid": None},  # 没起 Qdrant
            "worker": {"pid": 999},  # worker 在跑
        },
    )  # 打桩结束
    port_file = paths.data_dir() / "worker.port"  # worker 写的对话 RPC 端口
    port_file.parent.mkdir(parents=True, exist_ok=True)  # 建目录
    port_file.write_text("61234", encoding="utf-8")  # 端口内容

    summary = proc_services.port_summary(ui_port=8080)  # 汇总

    assert summary["ui"] == 8080  # UI 端口
    assert summary["worker_rpc"] == 61234  # worker 对话 RPC
    assert summary["worker_pid"] == 999  # worker pid
    assert summary["qdrant_http"] == 0  # 无本机 Qdrant
    assert summary["qdrant_grpc"] == 0  # gRPC 一并归 0


def test_port_summary_reads_qdrant_ports_from_status(
    monkeypatch: pytest.MonkeyPatch,  # 打桩
) -> None:
    """本机 Qdrant：HTTP 端口来自状态，gRPC 固定 +1，并带 pid。"""
    monkeypatch.setattr(  # 不碰真实子进程/网络
        proc_services,  # 模块
        "status",  # 状态来源
        lambda: {  # 假状态
            "url": "http://127.0.0.1:7654",  # 本机服务端
            "port": 7654,  # HTTP 端口
            "qdrant": {"local": True, "pid": 111},  # Qdrant 在跑
            "worker": {"pid": None},  # worker 没起
        },
    )  # 打桩结束
    monkeypatch.setattr(proc_services, "worker_rpc_port", lambda: 0)  # 不读真实端口文件

    summary = proc_services.port_summary()  # 汇总（不传 UI 口）

    assert summary["ui"] == 0  # 没传就是 0
    assert summary["qdrant_http"] == 7654  # HTTP
    assert summary["qdrant_grpc"] == 7655  # gRPC = HTTP+1
    assert summary["qdrant_pid"] == 111  # pid
    assert summary["worker_rpc"] == 0  # 未就绪
    assert summary["worker_pid"] is None  # 没跑
