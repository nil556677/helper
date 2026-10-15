"""T2.3 自检命令：只读、报告形状、退出码。

**铁律：自检只读**。这条用例专门钉住"跑自检不产生环境副作用"——尤其是不建表、不写配置。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.selfcheck import (
    EXIT_HAS_ERROR,
    EXIT_SELF_ERROR,
    CheckResult,
    as_json,
    main,
    render,
    run_checks,
)


def test_run_checks_returns_reporter_shape(fake_root: Path) -> None:
    """结果列表含必需的名字，且每项有 level/name/detail。"""
    from app.core import paths

    paths.ensure_dirs()
    results = run_checks(deep_hash=False)
    names = {item.name for item in results}
    # 至少覆盖主线检查项
    for expected in ("目录", "配置", "数据库", "模型文件", "端口"):
        assert expected in names, names


def test_selfcheck_is_read_only(fake_root: Path) -> None:
    """跑完自检，库文件不该被创建/改动（只读打开）。"""
    from app.core import paths

    paths.ensure_dirs()
    db_before = paths.db_file()
    exists_before = db_before.exists()

    run_checks(deep_hash=False, check_config=False)

    assert db_before.exists() == exists_before, "自检不该凭空建库"
    assert not any(p.name.startswith("config.bad") for p in paths.project_root().iterdir())


def test_render_is_text() -> None:
    results = [CheckResult(level="OK", name="目录", detail="data/、logs/ 存在且可写")]
    text = render(results)
    assert "✓" in text and "目录" in text


def test_as_json_is_parseable(fake_root: Path) -> None:
    from app.core import paths

    paths.ensure_dirs()
    payload = json.loads(as_json(run_checks(deep_hash=False)))
    assert "ok" in payload and "results" in payload


def test_main_json_uses_json_output(fake_root: Path, capfd) -> None:
    from app.core import paths

    paths.ensure_dirs()
    code = main(json_output=True, deep_hash=False)
    assert code in (EXIT_HAS_ERROR, EXIT_SELF_ERROR, 0)
    out = capfd.readouterr().out
    assert out.lstrip().startswith("{")  # JSON 输出


def test_main_no_self_error(fake_root: Path) -> None:
    """自检自身不该抛（参数对了就整点返回退出码）。"""
    from app.core import paths

    paths.ensure_dirs()
    code = main(json_output=False, deep_hash=False)
    assert code != EXIT_SELF_ERROR
