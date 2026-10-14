"""分层红线：把文档里的规矩变成**机器守**的断言。

`技术方案.md` §3.1 定了三层：`app/ui` → `app/services` → `app/core`（箭头单向）。
这三条以前只靠人守，搬目录时顺手钉成测试：

1. **`app/core/**` 不许** import 界面（`nicegui`/`PySide6`）或上层（`app.ui`/`app.services`）
   —— 引擎必须能脱离界面单独跑（将来拆服务、跑批处理都靠这条）；
2. **`app/ui/**` 不许** import `app.core` —— 界面只能经 `app.services`（这是"界面只做三件事"的机械版）；
3. **`app/services/**` 不许** import `app.ui` 或界面库 —— services 是 UI-free 的（pytest 直接调）。

顺带钉住目录形态本身（`app/{ui,services,core}` 三个包都在、都非空）：搬过一次家，
不希望哪天又被随手摊平。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

#: 每层禁止出现的模块前缀（导入语句与字符串引用都查）
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "core": ("nicegui", "PySide6", "app.ui", "app.services"),
    "ui": ("app.core",),
    "services": ("app.ui", "nicegui", "PySide6"),
}


def _modules(path: Path) -> list[tuple[str, int]]:
    """收集一个文件里出现的模块名：import 语句 + 像模块路径的字符串常量。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.append((node.module, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            # 只看"像模块路径"的字符串（含点、不含空格与斜杠），避免把普通文案算进来
            if 3 < len(text) < 80 and text.count(".") >= 1 and " " not in text and "/" not in text:
                found.append((text, node.lineno))
    return found


def _violations(layer: str) -> list[str]:
    forbidden = FORBIDDEN[layer]
    problems: list[str] = []
    for path in sorted((APP / layer).rglob("*.py")):
        for module, line in _modules(path):
            for prefix in forbidden:
                if module == prefix or module.startswith(f"{prefix}."):
                    problems.append(f"{path.relative_to(ROOT)}:{line} 引用了 {module}")
    return problems


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layering_has_no_upward_imports(layer: str) -> None:
    """每一层都不许往上层或界面伸手。"""
    problems = _violations(layer)
    assert not problems, f"app/{layer}/ 越界：\n" + "\n".join(problems)


def test_three_layers_exist_and_are_not_empty() -> None:
    """目录形态本身：`app/{ui,services,core}` 三个包都在、都有内容。"""
    for layer in ("ui", "services", "core"):
        folder = APP / layer
        assert folder.is_dir(), f"少了 app/{layer}/"
        assert any(folder.rglob("*.py")), f"app/{layer}/ 是空的"
