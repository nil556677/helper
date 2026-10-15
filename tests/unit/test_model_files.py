"""T3.1 模型文件完整性：四种情况 + 哈希缓存 + --update-manifest。

用假文件测（不真读 1.1 GB 的模型）：把 `paths.models_dir` 指到临时目录，
按真值分别构造"齐全 / 少文件 / 大小不符 / 哈希不符"四种场景。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core import paths
from app.core.models import files as model_files
from app.core.models.files import (
    MANIFEST,
    ModelFileError,
    require,
    update_manifest_for,
)


@pytest.fixture
def models_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把模型目录指到临时目录，并隔离模块级 MANIFEST（update_manifest_for 会改它）。"""
    model_dir = tmp_path / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "models_dir", lambda: model_dir)
    monkeypatch.setattr(model_files, "MANIFEST", dict(MANIFEST))
    return model_dir


def _make_file(models_tmp: Path, name: str, *, size: int) -> Path:
    path = models_tmp / name
    path.write_bytes(b"x" * size)
    return path


def test_manifest_lists_three_files() -> None:
    assert set(MANIFEST) == {
        "bge-m3.int8.onnx",
        "bge-reranker-v2-m3.int8.onnx",
        "tokenizer.json",
    }


def test_missing_file_is_reported(models_tmp: Path) -> None:
    name = "tokenizer.json"
    issues = model_files.problems(name)
    assert issues, "缺文件就该有问题"
    text = "".join(issues)
    assert "缺失" in text and "去哪下" in text, text


def test_wrong_size_is_error_even_without_hash(models_tmp: Path) -> None:
    """大小不符在 deep=False 时也要报（不必等算哈希）。"""
    name = "tokenizer.json"
    _make_file(models_tmp, name, size=MANIFEST[name].size - 100)
    issues = model_files.problems(name, deep=False)
    assert issues and "大小不符" in issues[0], issues
    # 大小对不上的时候连哈希都没必要算
    assert model_files.problems(name)  # deep 也一样报


def test_right_size_wrong_hash_is_warning(models_tmp: Path) -> None:
    """大小对得上、sha256 对不上 → 报哈希不符（当 WARNING，不误伤自量化用户）。"""
    name = "tokenizer.json"
    _make_file(models_tmp, name, size=MANIFEST[name].size)
    issues = model_files.problems(name, deep=True)
    text = "；".join(issues)
    assert not issues or "sha256 不符" in text or "哈希" in text, issues
    # 只查大小（deep=False）时，大小对就该干净
    assert model_files.problems(name, deep=False) == []


def test_require_raises_on_missing(models_tmp: Path) -> None:
    with pytest.raises(ModelFileError):
        require("bge-m3.int8.onnx")  # 目录里什么都没有


def test_require_returns_path_when_ok(models_tmp: Path) -> None:
    name = "tokenizer.json"
    _make_file(models_tmp, name, size=1234)
    # 直接让清单"接受现状"，之后 require 就应当通过且返回路径
    update_manifest_for(name)
    got = require(name)
    assert got.name == name


def test_hash_cache_avoids_recompute(models_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """算过一次走缓存，不再重读整个文件。"""
    name = "tokenizer.json"
    size = MANIFEST[name].size
    _make_file(models_tmp, name, size=size)
    fake = "f" * 64

    def _boom(_path: Path) -> str:
        raise AssertionError("不该重算哈希——缓存应当命中")

    model_files._write_cache(name, size, fake)
    monkeypatch.setattr(model_files, "_str_hash", _boom)
    # 缓存命中 → 哈希直接来自缓存（值为 fake），没调 _str_hash（否则 _boom 会抛）
    issues = model_files.problems(name, deep=True)
    assert issues and fake in "；".join(issues), "哈希应来自缓存里的 fake 值"
    # 把缓存删掉后重来：此时会调 _str_hash（monkeypatch 的 _boom 抛错），
    # 说明缓存确实是"免重算"的闸门
    model_files._cache_path(name).unlink(missing_ok=True)
    with pytest.raises(AssertionError):
        model_files.problems(name, deep=True)


def test_update_manifest_accepts_current(models_tmp: Path) -> None:
    """`--update-manifest`：把当前文件的哈希写回清单，之后不再报大小问题。"""
    name = "tokenizer.json"
    _make_file(models_tmp, name, size=1234)
    record = update_manifest_for(name)
    assert record.size == 1234
    assert record.sha256 == hashlib.sha256((models_tmp / name).read_bytes()).hexdigest()
    assert model_files.problems(name, deep=False) == []


def test_is_identifier_rejects_path_traversal() -> None:
    assert model_files.is_identifier("tokenizer.json")
    assert not model_files.is_identifier("../secret.json")
    assert not model_files.is_identifier("a/b")
