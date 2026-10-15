"""``scripts/backup_docs.py`` 的行为契约（T4.2）。

- 两次备份落到不同的时间戳快照目录，且目标目录里能见到源文档；
- 明显临时文件（``.DS_Store``、``~$`` 开头的 Office 锁定文件）不被复制；
- 保留最近 N 份，更旧的只删"目录名匹配时间戳"的备份目录，绝不碰别的目录。

这里用仓库内 ``tmp_path`` 装置（受限环境指向 ``.tmp/``），源与目标都放临时目录，
不碰真实 ``docs/``，也不依赖它的内容。
"""

from __future__ import annotations

from pathlib import Path

from scripts.backup_docs import backup_docs, is_backup_dir, skip_file


def _make_source(base: Path) -> Path:
    """造一份假的"文档源"：两篇正经 md + 一个明显临时文件。"""
    src = base / "docs"
    (src / "sub").mkdir(parents=True)
    (src / "技术方案.md").write_text("# 技术方案\n" * 10, encoding="utf-8")
    (src / "sub" / "RAG开发计划.md").write_text("# RAG\n", encoding="utf-8")
    (src / ".DS_Store").write_bytes(b"\x00\x01")
    (src / "sub" / "~$技术方案.docx").write_bytes(b"\x00\x01")
    return src


def test_backup_creates_snapshot_and_skips_temp_files(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = tmp_path / "out"

    result = backup_docs(src, dest, keep=10)

    assert result.target.is_dir()
    assert (result.target / "技术方案.md").exists()
    assert (result.target / "sub" / "RAG开发计划.md").exists()
    # 明显临时文件必须被跳过
    assert not (result.target / ".DS_Store").exists()
    assert not (result.target / "sub" / "~$技术方案.docx").exists()
    assert result.total_bytes > 0


def test_backup_twice_lands_in_different_dirs(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = tmp_path / "out"

    first = backup_docs(src, dest, keep=10)
    second = backup_docs(src, dest, keep=10)

    assert first.target != second.target
    assert first.target.is_dir() and second.target.is_dir()


def test_backup_keeps_only_newest(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()

    # 预造 5 个旧快照（命名符合本脚本的时间戳模式）+ 1 个"别人的"目录
    for name in ["20250101-000000", "20250102-000000", "20250103-000000",
                 "20250104-000000", "20250105-000000"]:
        (dest / name).mkdir()
    foreign = dest / "not-a-backup"
    foreign.mkdir()

    result = backup_docs(src, dest, keep=3)

    # 保留：本轮新快照 + 最新的 2 个旧快照；删掉最老的 3 个
    assert len(result.removed) == 3
    assert {r.name for r in result.removed} == {
        "20250101-000000", "20250102-000000", "20250103-000000",
    }
    # "别人的"目录绝不能被动
    assert foreign.exists()


def test_is_backup_dir_only_matches_timestamp(tmp_path: Path) -> None:
    assert is_backup_dir("20261015-192945")
    assert is_backup_dir("20261015-192945-2")  # 同秒错开的后缀
    assert not is_backup_dir("not-a-backup")
    assert not is_backup_dir("20261015-19294")  # 秒位不足


def test_skip_file_flags_temp_files() -> None:
    assert skip_file(".DS_Store")
    assert skip_file("~$技术方案.docx")
    assert not skip_file("技术方案.md")
    assert not skip_file("正常.docx")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    dest = tmp_path / "out"

    result = backup_docs(src, dest, keep=10, dry_run=True)

    assert not dest.exists()      # 目标目录不该被建出来
    assert result.target.name == "" and not result.target.suffix  # 没生成新快照目录
    assert result.total_bytes > 0
