"""规则检查（《开发规则》的机械部分）：文件过大 / 函数过长 / 顶层函数过多 / 跨层 import。

判定（只读，不改代码；WARN 提示、ERROR 才让 `check.py` 整体失败）：

| 检查 | WARN | ERROR |
| --- | --- | --- |
| 文件行数 | 400–700 | > 700（非白名单） |
| 顶层函数数 | > 15 | — |
| 函数长度 | > 50 行 | — |
| 跨层 import | — | `app/ui` 直接 import `app/core` |

**白名单**（纯数据文件，规则 R1 例外）：DDL、CSS、参数常量表。
**跨层**：`app/ui/**` 里出现 `from app.core` 或 `import app.core` → ERROR
（`app/ui/screens/settings/**/preview.py` 这类里对 `app.ui.state.layout` 的局部 import 不算，
只查顶层 `from app.core`/`import app.core`）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

#: 纯数据文件（规则 R1 白名单）：不受行数限制
DATA_FILES = {
    "app/core/db/schema.py",
    "app/ui/resources/theme.py",
    "app/core/config/params.py",
}

#: 跨层检查：app/ui 里禁止顶层 import app.core
UI_ROOT = APP / "ui"


def _scan(path: Path) -> list[tuple[str, str]]:
    """返回 [(级别, 信息行)]，级别 ∈ {WARN, ERROR}。"""
    findings: list[tuple[str, str]] = []
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    text = path.read_text(encoding="utf-8")
    lines = text.count("\n") + 1

    data_exempt = rel in DATA_FILES
    if not data_exempt:
        if lines > 700:
            findings.append(("ERROR", f"{rel}: {lines} 行，超过 700 —— 必须拆文件（R1）"))
        elif lines > 400:
            findings.append(("WARN", f"{rel}: {lines} 行，超 400 —— 要写理由或拆文件（R1）"))

    try:
        tree = ast.parse(text)
    except SyntaxError:
        findings.append(("ERROR", f"{rel}: 无法解析（语法错误）"))
        return findings

    top_functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not data_exempt and len(top_functions) > 15:
        findings.append(("WARN", f"{rel}: {len(top_functions)} 个顶层函数，超 15（R1）"))

    if UI_ROOT in path.parents:
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if node.level == 0 and mod == "core":
                    findings.append(("ERROR", f"{rel}: 界面 import core —— 跨层直连（R6）"))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            size = (node.end_lineno or node.lineno) - node.lineno + 1
            if size > 50 and not node.name.startswith("test"):
                findings.append(
                    ("WARN", f"{rel}:{node.lineno} {node.name}() {size} 行，超 50（R2）")
                )
    return findings


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    files = 0
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        files += 1
        for level, msg in _scan(path):
            (errors if level == "ERROR" else warnings).append(msg)

    if warnings:
        print("[规则检查] WARN（提示，不判失败）：")
        for w in warnings:
            print("   " + w)
    if errors:
        print("[规则检查] ERROR（必须处理）：")
        for e in errors:
            print("   " + e)
        print(f"[规则检查] 失败：{len(errors)} 处 ERROR（共扫描 {files} 个文件）。")
        return 1
    print(f"[规则检查] 通过：扫 {files} 个文件，无 ERROR（{len(warnings)} 条 WARN 仅提示）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
