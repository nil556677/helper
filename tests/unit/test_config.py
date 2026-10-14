"""引导配置：坏配置必须被隔离，并且不许让程序起不来。

``config.json`` 是用户能手改的文件，所以"改坏一个字符就打不开软件"是不可接受的失败模式。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import access
from app.core.config.schema import BootstrapConfig


def test_missing_file_returns_defaults(fake_root: Path) -> None:
    """文件不存在：给默认值，不抛异常。"""
    config = access.load()
    assert config.log_level == "INFO"
    assert config.data_dir is None


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
        json.dumps({"log_level": "WARNING", "_data_dir_note": "随便写点什么"}), encoding="utf-8"
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
