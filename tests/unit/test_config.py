"""引导配置：坏配置必须被隔离，并且不许让程序起不来。

``config.json`` 是用户能手改的文件，所以"改坏一个字符就打不开软件"是不可接受的失败模式。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import access
from app.core.config.schema import DEFAULT_HOST, BootstrapConfig, is_loopback


def test_missing_file_returns_defaults(fake_root: Path) -> None:
    """文件不存在：给默认值，不抛异常。"""
    config = access.load()
    assert config.log_level == "INFO"


def test_ensure_default_copies_example(fake_root: Path) -> None:
    """首次启动从模板生成配置文件（模板里的注释键要被剥掉）。"""
    (fake_root / "config.example.json").write_text(
        json.dumps({"log_level": "DEBUG", "_note": "这里是注释"}), encoding="utf-8"
    )
    path = access.ensure_default()

    assert path == fake_root / "config.json" and path.exists()
    assert access.load().log_level == "DEBUG"


def test_comment_keys_are_stripped(fake_root: Path) -> None:
    """``_`` 开头的键当注释，不影响校验。"""
    (fake_root / "config.json").write_text(
        json.dumps({"log_level": "WARNING", "_note": "随便写点什么"}), encoding="utf-8"
    )
    access.invalidate()
    assert access.load().log_level == "WARNING"


def test_unknown_key_is_isolated_and_defaults_used(fake_root: Path) -> None:
    """键名拼错（extra=forbid）→ 隔离坏文件 + 回落默认值。"""
    (fake_root / "config.json").write_text(json.dumps({"log_levle": "DEBUG"}), encoding="utf-8")
    access.invalidate()

    assert access.load().log_level == "INFO"
    assert list(fake_root.glob("config.bad-*.json")), "坏配置要挪到一边，别让下次启动再踩"


def test_bad_json_is_isolated(fake_root: Path) -> None:
    """不是合法 JSON 也一样：隔离 + 默认值。"""
    (fake_root / "config.json").write_text("{ 这不是 JSON", encoding="utf-8")
    access.invalidate()

    assert access.load().log_level == "INFO"
    assert list(fake_root.glob("config.bad-*.json"))


def test_save_is_atomic_and_takes_effect(fake_root: Path) -> None:
    """保存后立刻生效（缓存作废）、临时文件不残留、内容真的是新值。"""
    access.save(BootstrapConfig(log_level="ERROR"))

    assert access.load().log_level == "ERROR"
    assert not (fake_root / "config.json.tmp").exists()
    on_disk = json.loads((fake_root / "config.json").read_text(encoding="utf-8"))
    assert on_disk["log_level"] == "ERROR"


def test_cache_follows_file_change(fake_root: Path) -> None:
    """按"代"缓存：文件变了就要读到新值（同尺寸改写也要能发现）。"""
    access.save(BootstrapConfig(log_level="INFO"))
    assert access.load().log_level == "INFO"

    # 直接改文件（模拟用户手改），不改动缓存
    (fake_root / "config.json").write_text(json.dumps({"log_level": "DEBUG"}), encoding="utf-8")
    assert access.load().log_level == "DEBUG"


def test_default_host_is_loopback(fake_root: Path) -> None:
    """默认监听地址必须是回环：界面没有认证，绑出去等于把资料给整个局域网看。"""
    assert access.load().host == DEFAULT_HOST
    assert is_loopback(DEFAULT_HOST)


def test_host_round_trips(fake_root: Path) -> None:
    """host 能被改、能落盘、能读回来（配置文件以后要用起来）。"""
    access.save(BootstrapConfig(host="127.0.0.2"))
    assert access.load().host == "127.0.0.2"
    on_disk = json.loads((fake_root / "config.json").read_text(encoding="utf-8"))
    assert on_disk["host"] == "127.0.0.2"


def test_blank_host_falls_back_to_default(fake_root: Path) -> None:
    """留空不算错，回落默认值（用户清空一个字段不该让程序起不来）。"""
    (fake_root / "config.json").write_text(json.dumps({"host": "   "}), encoding="utf-8")
    access.invalidate()
    assert access.load().host == DEFAULT_HOST


def test_host_with_scheme_is_rejected_and_isolated(fake_root: Path) -> None:
    """填成 URL 这种"一看就填错了"的值：加载期报错 + 隔离坏文件 + 回落默认。"""
    (fake_root / "config.json").write_text(
        json.dumps({"host": "http://127.0.0.1"}), encoding="utf-8"
    )
    access.invalidate()

    assert access.load().host == DEFAULT_HOST
    assert list(fake_root.glob("config.bad-*.json"))


def test_is_loopback_recognizes_loopback_spellings() -> None:
    """回环的几种写法都算回环；0.0.0.0 与局域网地址不算。"""
    for host in ("127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]", "LOCALHOST"):
        assert is_loopback(host), host
    for host in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com"):
        assert not is_loopback(host), host


def test_listen_host_reads_config_and_warns_when_not_loopback(fake_root: Path) -> None:
    """`_listen_host()` 真的读配置；非回环地址要留下一条 WARNING（界面没有认证）。"""
    from loguru import logger

    from app.__main__ import _listen_host

    access.save(BootstrapConfig(host="127.0.0.1"))
    assert _listen_host() == "127.0.0.1"

    access.save(BootstrapConfig(host="0.0.0.0"))
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message), level="WARNING")
    try:
        assert _listen_host() == "0.0.0.0"
    finally:
        logger.remove(sink_id)

    assert captured, "非回环地址必须告警"
    assert "不是回环" in "".join(str(item) for item in captured)


def test_legacy_config_keys_are_ignored_not_fatal(fake_root: Path) -> None:
    """老配置里的 data_dir 要被静默忽略：删字段不能把用户的其它设置一起隔离走。"""
    (fake_root / "config.json").write_text(
        json.dumps({"log_level": "WARNING", "data_dir": "D:/old", "config_version": 1}),
        encoding="utf-8",
    )
    access.invalidate()

    assert access.load().log_level == "WARNING", "其它设置要保留"
    assert not list(fake_root.glob("config.bad-*.json")), "不该被判成坏配置"
