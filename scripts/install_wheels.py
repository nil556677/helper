"""在受限沙箱里装 Python 依赖（pip 在这里用不了）。

**为什么需要这个脚本**：pip 的下载与解包走 `tempfile.mkdtemp()`，而它建的是 **0o700 目录**；
受限令牌下这种目录**写不进去**（本机实测 `Errno 13`，pip 永远停在"下载成功、写不进去"）。
我们自己的界面测试装置里也记着同一类坑（`mkdtemp` 建的系统 TEMP 目录列不出、删不掉）。

**做法与 pip 等价**：从镜像的 simple 索引取 wheel → `urllib` 普通下载（普通 `open` 写文件，
权限正常）→ `zipfile` 解包进 `site-packages`（wheel 本质就是 zip）→ 校验能否 import。

**不解析依赖**：要装的包（含传递依赖）由调用方在命令行给全。

用法：
    python scripts/install_wheels.py onnxruntime numpy tokenizers
    python scripts/install_wheels.py --index https://pypi.org/simple <包名…>
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 默认走国内镜像（官方源在国内经常 SSL 断流）
DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple"

#: wheel 标签优先级：本机是 CPython 3.12 / Windows x64
TAG_PRIORITY = (
    "cp312-cp312-win_amd64",
    "cp312-abi3-win_amd64",
    "cp39-abi3-win_amd64",
    "py3-none-any",
    "py2.py3-none-any",
)

_HREF = re.compile(r'href="([^"#]+\.whl)(?:#[^"]*)?"', re.IGNORECASE)


def _wheels_on(index: str, name: str) -> list[str]:
    """取某个包在索引页上的所有 wheel 链接（绝对 URL）。"""
    url = f"{index.rstrip('/')}/{name}/"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - 只取固定索引
        html = response.read().decode("utf-8", "replace")
    links = []
    for href in _HREF.findall(html):
        links.append(href if href.startswith("http") else f"{url}{href}")
    return links


def _pick(links: list[str]) -> str | None:
    """按标签优先级与版本新旧挑一个 wheel。

    两轮：**先只挑正式版**（跳过 `1.14.0rc2`/`0.60b1` 这类）；一个都没有时再退回到预发布版。
    为什么要退：有些包的**所有**版本都带字母（`opentelemetry-semantic-conventions` 就是 `0.60b1`），
    只挑正式版会导致"63 个候选却一个都选不出来"。
    """
    stable: list[tuple[tuple[int, tuple[int, ...], str], str]] = []
    any_version: list[tuple[tuple[int, tuple[int, ...], str], str]] = []
    for link in links:
        filename = link.rsplit("/", 1)[-1]
        stem = filename[: -len(".whl")]
        parts = stem.split("-")
        version = parts[1] if len(parts) > 1 else "0"
        numbers = tuple(int(part) for part in re.findall(r"\d+", version)) or (0,)
        for rank, tag in enumerate(TAG_PRIORITY):
            if tag in filename:
                key = (-rank, numbers, filename)
                any_version.append((key, link))
                if not re.search(r"[a-zA-Z]", version):
                    stable.append((key, link))
                break
    if not stable and not any_version:
        return None
    pool = stable or any_version
    return max(pool, key=lambda item: item[0])[1]


def _download(url: str, folder: Path) -> Path:
    """下载到工作区（普通 open 写文件：这条路在受限令牌下是通的）。

    **缓存要验完整性**：只看"文件存在且非空"会踩坑——上次中断留下的**截断 wheel**
    会被当成已下载，随后解包报 `BadZipFile`（chromadb 就这么失败过一次）。
    """
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / url.rsplit("/", 1)[-1]
    if target.is_file() and target.stat().st_size > 0:
        try:
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is None:
                    print(f"  已有 {target.name}（完整）")
                    return target
        except zipfile.BadZipFile:
            pass
        print(f"  已有的 {target.name} 不完整，重下")
        target.unlink(missing_ok=True)
    print(f"  下载 {target.name} …", end="", flush=True)
    with urllib.request.urlopen(url, timeout=300) as response, open(target, "wb") as handle:  # noqa: S310
        while chunk := response.read(1 << 20):
            handle.write(chunk)
    print(f" {target.stat().st_size / 1048576:.1f} MB")
    return target


def _requirements(wheel: Path) -> list[str]:
    """读 wheel 里的 METADATA，取出它声明的依赖名（跳过 extras 与已装的）。"""
    names: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        meta = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            return names
        for line in archive.read(meta).decode("utf-8", "replace").splitlines():
            if not line.startswith("Requires-Dist:"):
                continue
            requirement = line.split(":", 1)[1].strip()
            if "extra ==" in requirement:  # extras 不装（我们要的是运行时）
                continue
            name = re.split(r"[\s;\[<>=!~()]", requirement, maxsplit=1)[0].strip()
            if name and _installed(name) is False:
                names.append(name)
    return names


def _installed(name: str) -> bool:
    """这个发行版装了吗（按发行版名查，找不到就算没装）。"""
    import importlib.metadata as metadata

    try:
        metadata.version(name)
        return True
    except Exception:  # noqa: BLE001 - PackageNotFoundError 等
        return False


def _installed_requirements(name: str) -> list[str]:
    """已装的包也把它的依赖交出来。

    **为什么要这一步**：包装上、依赖没装会留下"半吊子"状态（实测：`qdrant_client` 装进去了，
    它的 `grpcio` 没装 → import 直接炸）。所以在"已装"这条路径上也要查依赖，不能直接跳过。
    """
    import importlib.metadata as metadata

    try:
        requires = metadata.requires(name) or []
    except Exception:  # noqa: BLE001 - 查不到就算了
        return []
    names: list[str] = []
    for requirement in requires:
        if "extra ==" in requirement:  # extras 不装
            continue
        dep = re.split(r"[\s;\[<>=!~()]", requirement, maxsplit=1)[0].strip()
        if dep and not _installed(dep):
            names.append(dep)
    return names


def install(names: list[str], *, index: str, with_deps: bool = True) -> int:
    """下载并解包到当前解释器的 site-packages（可选自动解析依赖）。返回失败个数。"""
    import site

    target_dir = Path(site.getsitepackages()[-1])
    wheels = ROOT / ".tmp" / "wheels"
    print(f"目标目录 {target_dir}")
    queue = list(names)
    failures = 0
    while queue:
        name = queue.pop(0)
        if _installed(name):
            missing = _installed_requirements(name) if with_deps else []
            print(f"· {name} 已装" + (f"，缺依赖 {missing}" if missing else "，依赖齐"))
            for requirement in missing:
                if requirement not in queue:
                    queue.append(requirement)
            continue
        try:
            links = _wheels_on(index, name)
            chosen = _pick(links)
            if chosen is None:
                print(f"✗ {name}：索引里没有适配本机的 wheel（{len(links)} 个候选）")
                failures += 1
                continue
            wheel = _download(chosen, wheels)
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(target_dir)  # noqa: S202 - 内容来自固定镜像的 wheel
            print(f"✓ {name} ← {wheel.name}")
            if with_deps:
                for requirement in _requirements(wheel):
                    if requirement not in queue:
                        queue.append(requirement)
        except Exception as exc:  # noqa: BLE001 - 一次性工具，报清楚就行
            print(f"✗ {name}：{type(exc).__name__}: {exc}")
            failures += 1
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="受限沙箱下装 wheel（绕过 pip 的 mkdtemp）")
    parser.add_argument("names", nargs="+", help="包名（依赖可自动解析）")
    parser.add_argument("--index", default=DEFAULT_INDEX, help="simple 索引地址")
    parser.add_argument("--no-deps", action="store_true", help="只装指定的包，不解析依赖")
    args = parser.parse_args()
    return 1 if install(args.names, index=args.index, with_deps=not args.no_deps) else 0


if __name__ == "__main__":
    sys.exit(main())
