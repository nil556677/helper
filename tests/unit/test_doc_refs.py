"""文档引用一致性检查（方案 T5.1）。

**问题**：代码注释/文档字符串里会写「技术方案 §10.4」这类引用。文档与代码各改各的，
没人看住就会"文档说了、代码没做"，或反之——注释指到一个根本不存在的小节。本项目已经
两次踩过（docs/ 是整个被 `.gitignore` 排除的内部文档，漂移尤其容易）。

逻辑全部抽成纯函数：
- ``referenced_sections`` —— 扫 `app/**/*.py` 与 `tests/**/*.py`，抽「§N」引用（`§10.4`、
  `技术方案 §10.4` 都认）；
- ``doc_section_titles`` —— 从文档标题收集小节号（正则 `^#{1,6}\\s+(\\d+(\\.\\d+){0,2})`）；
- ``missing_sections`` —— 筛出「被引用但文档里没有」的那些。

**既有漂移的温和降级**：真仓里已经有一批旧引用（§13.4 / §13.5 / §13.10……）指向文档 §13
「里程碑与排期」下的旧子节，而文档那个 §13 早已压成一张里程碑表、没有这些子节。这是 T5.1
要抓的漂移，但**不该让 CI 一上来就红在这批既有漂移上**。做法：把已知漂移小节号登记在
``_KNOWN_MISSING_SECTIONS`` 里，遇到它们只打 WARNING、单独列出（这才是给维护者看的地方），
测试本身不因它们失败；**新冒出来**的不在清单里的缺失引用，才硬失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# §10.4 / 技术方案 §10.4 → 抓「10.4」。允许小节号最多两级子号（如 10.4.1）。
_REF_RE = re.compile(r"§\s*(?:技术方案\s*)?(\d+(?:\.\d+){0,2})")
# 标题行 `# 10.4 xxx` / `## 10.4` → 抓「10.4」。`#{1,6}` 对任意级标题都认。
_TITLE_RE = re.compile(r"^#{1,6}\s+(\d+(?:\.\d+){0,2})")

# 扫描时要跳过的目录（与 .gitignore / ruff.extend-exclude 保持一致）。
_SKIP_DIRS = {".venv", ".tmp", "models", "data", "logs"}

# 真仓里**已知**的漂移引用：代码指向文档不存在的旧子节（集中在已压成里程碑表的 §13、
# 文档根本没有的 §6.2 合同 §7.2 等）。它们照常被 WARNING 指出，但不让测试失败。
# 维护者修一批就从这里删一批；清单外的缺失引用仍会硬失败。
_KNOWN_MISSING_SECTIONS: set[str] = {
    "6.2",  # 方案 §6.2：文档 §6「数据模型」没有 6.2 子节
    "7.2",  # `合同 §7.2`：这是合同条款编号，不是对技术方案的引用
    "13.2",  # 旧任务系统子节，已并入 §8
    "13.4",
    "13.5",
    "13.6",
    "13.8",
    "13.10",
}


@dataclass(frozen=True)
class SectionRef:
    """一处源码里对某小节的引用。"""

    file: Path
    line: int
    section: str


@dataclass
class ScanResult:
    """一次扫描的结果：有效引用、匹配的缺失、新冒出的缺失。"""

    referenced: list[SectionRef]
    missing: list[SectionRef]
    known: list[SectionRef] = field(default_factory=list)
    unexpected: list[SectionRef] = field(default_factory=list)


def referenced_sections(root: Path) -> list[SectionRef]:
    """扫 `root/app` 与 `root/tests` 下所有 ``*.py``，抽「§N」引用。

    跳过 `.venv`/`.tmp`/`models`/`data`/`logs` 目录。判断用「相对根的路径分量」，
    不能看绝对 path.parts——测试临时目录本身在 `.tmp/` 下，那样会把被测文件全跳掉。
    """
    refs: list[SectionRef] = []

    def _scan(base: Path) -> None:
        for path in sorted(base.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for match in _REF_RE.finditer(line):
                    refs.append(SectionRef(path, lineno, match.group(1)))

    for name in ("app", "tests"):
        base = root / name
        if base.is_dir():
            _scan(base)

    return refs


def doc_section_titles(doc_path: Path) -> set[str]:
    """收集文档里由标题声明的所有小节号。"""
    if not doc_path.is_file():
        return set()
    titles: set[str] = set()
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        match = _TITLE_RE.match(line)
        if match:
            titles.add(match.group(1))
    return titles


def missing_sections(refs: list[SectionRef], titles: set[str]) -> list[SectionRef]:
    """从引用里筛出「文档标题里没有」的那些，保持源码出现顺序。"""
    existing = set(titles)
    return [ref for ref in refs if ref.section not in existing]


def split_known(missing: list[SectionRef]) -> tuple[list[SectionRef], list[SectionRef]]:
    """把缺失引用拆成两堆：``(已知漂移, 新冒出的缺失)``。

    已知漂移 → 只 WARNING；新缺失 → 该硬失败（这才是检查的价值）。
    """
    known = [ref for ref in missing if ref.section in _KNOWN_MISSING_SECTIONS]
    unexpected = [ref for ref in missing if ref.section not in _KNOWN_MISSING_SECTIONS]
    return known, unexpected


def _format_refs(refs: list[SectionRef]) -> str:
    lines = [f"  §{ref.section}  ←  {ref.file}:{ref.line}" for ref in refs]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 逻辑用例：不依赖真实 docs/，用构造的临时目录 + 文档测。故意写错的引用号放在
# 「临时文件」里（而不是本文件源码），否则真仓扫描会把本文件自己的测试串当真。
# ---------------------------------------------------------------------------


def test_missing_sections_reported_with_file_and_line(tmp_path: Path) -> None:
    """故意写一个文档里没有的小节号 → `missing_sections` 必须报出来，带文件与行号。"""
    src = tmp_path / "app" / "infra.py"
    src.parent.mkdir(parents=True)
    # 数字用拼接拼出来，避免源码里出现 `§`+编号 这种字面量，被真仓扫描误当真引用。
    bad = "技术方案 §" + "99" + "." + "9"
    src.write_text("# 正确引用\n§10.4\n# 错误引用\n" + bad + "\n", encoding="utf-8")

    doc = tmp_path / "docs" / "技术方案.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 10 文档\n## 10.4 约束\n## 15 变更记录\n", encoding="utf-8")

    refs = referenced_sections(tmp_path)
    missing = missing_sections(refs, doc_section_titles(doc))

    assert len(missing) == 1, missing
    assert missing[0].section == "99.9"
    assert missing[0].file == src
    assert missing[0].line == 4, "行号要指到真正写那个错误引用注释的那一行"
    # 新缺失不属于已知漂移 → 应该被 split_known 归进「硬失败」一侧
    known, unexpected = split_known(missing)
    assert known == []
    assert unexpected == missing


def test_no_missing_when_doc_covers_every_reference(tmp_path: Path) -> None:
    """正向：临时文档只含被引用的小节号 → 不缺失。"""
    src = tmp_path / "app" / "main.py"
    src.parent.mkdir(parents=True)
    ref_b = "§" + "1" + "." + "1"
    ref_c = "§" + "10" + "." + "4" + "." + "1"
    src.write_text(
        "# 见 " + ref_b + " 与 技术方案 §9\n# 以及三级子号 " + ref_c + "\n",
        encoding="utf-8",
    )

    doc = tmp_path / "docs" / "技术方案.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# 1 概览\n### 1.1 模块划分\n## 9 检索\n### 10.4.1 约束细则\n",
        encoding="utf-8",
    )

    refs = referenced_sections(tmp_path)
    assert missing_sections(refs, doc_section_titles(doc)) == []


def test_scan_skips_reserved_dirs(tmp_path: Path) -> None:
    """扫描必须把 `.venv`/`.tmp`/`models`/`data`/`logs` 里的引用当不存在处理。"""
    junk = "§" + "98" + "." + "7"
    for name in sorted(_SKIP_DIRS):
        d = tmp_path / name
        d.mkdir(parents=True)
        (d / "junk.py").write_text("# " + junk + " 这种不该被扫到\n", encoding="utf-8")

    real = tmp_path / "app" / "real.py"
    real.parent.mkdir(parents=True)
    real.write_text("# " + junk + " 真实引用\n", encoding="utf-8")

    refs = referenced_sections(tmp_path)
    assert len(refs) == 1
    assert refs[0].file == real


def test_split_known_tolerates_documented_drift_and_flags_new(tmp_path: Path) -> None:
    """降级路径：已知漂移只算 WARNING；清单外的新缺失必须硬失败。"""
    doc = tmp_path / "docs" / "技术方案.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 10 文档\n", encoding="utf-8")  # 文档里没有任何子节

    _mk_source(tmp_path / "app" / "a.py", "§" + "13" + "." + "4")
    _mk_source(tmp_path / "app" / "b.py", "§" + "7" + "." + "2")
    _mk_source(tmp_path / "app" / "c.py", "§" + "3" + "." + "7")
    refs = referenced_sections(tmp_path)
    missing = missing_sections(refs, doc_section_titles(doc))
    known, unexpected = split_known(missing)

    # 13.4、7.2 是已知漂移 → 只 WARNING
    assert {r.section for r in known} == {"13.4", "7.2"}
    # 3.7 不在清单里 → 新缺失，必须硬失败
    assert [r.section for r in unexpected] == ["3.7"]


def _mk_source(path: Path, ref: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# " + ref + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 对真实仓库的「扫描 + 校验」
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_real_docs_refs_consistent(capsys: pytest.CaptureFixture[str]) -> None:
    """真扫一遍 `app/` + `tests/`，与真人维护的 `docs/技术方案.md` 对齐。

    docs/ 缺省（clone 不带文档）→ **跳过**，打一行说明；文档在 → 校验：
    - 已知漂移（§13.4 等）→ 以 WARNING 形式单独列出，**不**因此失败；
    - 清单外的新缺失 → 硬失败（这才是本检查要拦的漂移）。
    """
    root = _repo_root()
    doc = root / "docs" / "技术方案.md"
    if not doc.is_file():
        print(f"[test_doc_refs] 未找到 {doc}（仓库未带内部文档），跳过引用一致性检查")
        pytest.skip("docs/技术方案.md 不存在——文档未随仓库分发，跳过")
        return

    refs = referenced_sections(root)
    missing = missing_sections(refs, doc_section_titles(doc))
    known, unexpected = split_known(missing)

    if known:
        # 这批是既有漂移：明确指出，让维护者看得见，但不让 CI 红。这是本检查的价值。
        print(f"[test_doc_refs] WARNING：以下 {len(known)} 处引用指向文档不存在的旧子节"
              f"（已登记为已知漂移）：\n{_format_refs(known)}")

    out = capsys.readouterr().out
    assert "WARNING" in out, "已知漂移必须被明确指出来（WARNING 且带文件:行号）"
    assert not unexpected, (
        f"发现 {len(unexpected)} 处**新**缺失引用（不在已知漂移清单里）"
        f"，请核对文档或补登记：\n{_format_refs(unexpected)}\n"
        f"（若是代码注释漂移，请修代码或改引用；若文档缺小节，请补文档。）"
    )
