"""冻结/校验当前环境的依赖清单。

**为什么不用 pip freeze**：本机受限环境下 pip 大多数命令不可用，但 ``importlib.metadata``
是只读的、可用。而且我们要的不是裸 ``name==version``，而是按"直接依赖 / 传递依赖"分组，
可读性对于换机器复盘很重要。

用法（**在仓库根执行**）：

    python scripts/freeze_lock.py            # 写 requirements.lock.txt（全量 134→按当前）
    python scripts/freeze_lock.py --check    # 比对 venv 与 lock，有漂移退出码 1

**已知限制（写进文件头）**：不含哈希（离线环境无法解析全部 sdist/wheel 的 hash）；
不锁 Python 解释器的补丁版本（只锁次版本 >=3.12）。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata as metadata
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock.txt"
REQ = ROOT / "requirements.txt"

#: 文件头（标注生成信息，让人一眼知道这份 lock 是哪来的）。
HEADER_BEGIN = "# helper 依赖锁（由 scripts/freeze_lock.py 生成，别手改）"


def direct_names() -> set[str]:
    """从 requirements.txt 解析"直接依赖"的发行版名（跳过注释与空行）。"""
    names: set[str] = set()
    if not REQ.exists():
        return names
    for raw in REQ.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # 形如 `nicegui==3.18.0` / `pydantic>=2.9,<3`：取第一个名字令牌
        name = line.split("=")[0].strip()
        if name and not name.startswith("["):
            names.add(name)
    return names


def installed_freeze() -> dict[str, str]:
    """当前环境全部发行版 name→version（按名字排序）。"""
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        version = (dist.metadata.get("Version") or "").strip()
        if name:
            out[name.lower()] = version
    return dict(sorted(out.items()))


def resolve_top_level() -> set[str]:
    """从依赖图算出"真正被直接请求/顶端"的包（其余=传递依赖）。

    简化：把 requirements.txt 里的直接依赖当作"直接"，其余全部当作传递依赖。
    因为 RGB 的入口没有别的策略，这里足够。
    """
    return direct_names()


def freeze() -> None:
    """写 requirements.lock.txt（全量 + 分组注释）。"""
    direct = direct_names().copy()
    env = installed_freeze()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        HEADER_BEGIN,
        f"# 生成：{now} | Python {sys.version.split()[0]} | 平台 {sys.platform}",
        "# " + f"{ROOT}",
        "# 已知限制：不含哈希；不锁解释器补丁版本",
        "",
    ]

    groups: list[tuple[str, list[str]]] = []
    direct_lines, indirect_lines = [], []
    for name, version in env.items():
        line = f"{name}=={version}"
        (direct_lines if name in direct else indirect_lines).append(line)
    groups.append(("# --- 直接依赖 ---", direct_lines))
    groups.append(("# --- 传递依赖 ---", indirect_lines))

    for heading, items in groups:
        lines.append(heading)
        lines.extend(items)
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写 {LOCK}（共 {len(env)} 个包）")


def _load_lock() -> dict[str, str]:
    """读 lock 文件里的 name→version（跳过注释、空行与 BOM）。"""
    out: dict[str, str] = {}
    if not LOCK.exists():
        return out
    text = LOCK.read_text(encoding="utf-8-sig", errors="replace")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, version = line.split("==", 1)
        out[name.strip().lower()] = version.strip()
    return out


def check() -> int:
    """比对 venv 与 lock，返回漂移个数（>0 退出码 1）。"""
    env = installed_freeze()
    lock = _load_lock()
    missing = sorted(set(lock) - set(env))
    extra = sorted(set(env) - set(lock))
    changed = sorted(k for k in set(env) & set(lock) if env[k] != lock[k])
    if not (missing or extra or changed):
        print(f"依赖一致（lock {len(lock)} 项，venv {len(env)} 项）")
        return 0
    if missing:
        print("lock 有但环境没装：", ", ".join(f"{k}=={lock[k]}" for k in missing))
    if extra:
        print("环境多装（lock 没有）：", ", ".join(f"{k}=={env[k]}" for k in extra))
    if changed:
        print("版本不符：", ", ".join(f"{k}（{lock.get(k)} → {env[k]}）" for k in changed))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结/校验依赖清单")
    parser.add_argument("--check", action="store_true", help="只比对不写文件")
    args = parser.parse_args()
    # 输出统一 UTF-8：中文提示在 GBK 终端下会崩
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.check:
        return check()
    freeze()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
