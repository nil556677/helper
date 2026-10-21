"""单文件入库观测：**直接调用** ``importer.import_path``，走完整正常路径，再把结果读回来。

用途：给它一个文件路径，看正常入库后**到底被切成什么样**（父块/子块、token 数、章节），
据此调整切分比例。

**默认用隔离数据目录**（``.tmp/inspect-run/``），**跑完自动删掉**，不会碰你真实的资料库；
要写进真实库加 ``--real``（会真的入库，重复内容会被判重跳过）。

用法（**用项目虚拟环境**；脚本会自己把项目根加进 ``sys.path``）::

    .venv\\Scripts\\python scripts\\inspect_chunks.py D:\\docs\\合同.pdf
    .venv\\Scripts\\python scripts\\inspect_chunks.py 合同.md --limit 20
    .venv\\Scripts\\python scripts\\inspect_chunks.py 合同.md --keep    # 跑完不删隔离数据
    .venv\\Scripts\\python scripts\\inspect_chunks.py 合同.md --real    # 写进真实 data/（谨慎）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 直接 `python scripts/xxx.py` 运行时，sys.path[0] 是 scripts/，要手动补上项目根，
# 否则 `import app` 会 ModuleNotFoundError。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require_runtime() -> None:
    """检查是不是用对了 Python 环境（系统 python 没装项目依赖）。"""
    missing: list[str] = []
    for module, hint in (
        ("numpy", "numpy"),
        ("docx", "python-docx"),
        ("fitz", "PyMuPDF"),
    ):
        try:
            __import__(module)
        except ModuleNotFoundError:
            missing.append(hint)
    if missing:
        print(
            "缺少依赖：" + "、".join(missing) + "\n"
            "请用**项目虚拟环境**运行（系统 python 没装这些依赖）：\n"
            "    .venv\\Scripts\\python scripts\\inspect_chunks.py <文件路径>",
            file=sys.stderr,
        )
        raise SystemExit(3)


def _configure_stdout() -> None:
    """Windows 控制台按 UTF-8 输出，避免中文乱码。"""
    import contextlib

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]


def _resolve_input(path: Path) -> Path | None:
    """把参数解析成**一个可导入的文件**。

    - 给文件：直接用；
    - 给目录：找里面**唯一**一个支持的格式（多个就列出来让人指定）。
    """
    if path.is_file():
        return path
    if not path.is_dir():
        print(f"路径不存在：{path}", file=sys.stderr)
        return None

    from app.core.parse import supported_suffixes

    allowed = set(supported_suffixes())
    candidates = sorted(
        item for item in path.iterdir() if item.is_file() and item.suffix.lower() in allowed
    )
    if len(candidates) == 1:
        print(f"给的是目录，自动选用其中唯一可导入的文件：{candidates[0].name}")
        return candidates[0]
    if candidates:
        print("目录里有多个可导入的文件，请指定其中一个：", file=sys.stderr)
        for item in candidates:
            print(f"  {item.name}", file=sys.stderr)
    else:
        print(
            f"目录里没有可导入的文件（支持：{'、'.join(sorted(allowed))}）：{path}",
            file=sys.stderr,
        )
    return None


def _bootstrap(isolated: bool) -> tuple[str, Path | None]:
    """初始化数据目录与库；返回（说明，跑完要清理的目录）。

    **只重定向数据目录**（``data/``），项目根保持真实——模型/分词器在 ``models/`` 下，
    要按正常路径加载。
    """
    cleanup: Path | None = None
    if isolated:
        # 固定目录 + 每次清空：重复运行不会越积越多，也不会被上次的判重挡住
        import shutil

        base = ROOT / ".tmp" / "inspect-run"
        shutil.rmtree(base, ignore_errors=True)
        data = base / "data"
        data.mkdir(parents=True, exist_ok=True)
        from app.core import paths

        paths.data_dir = lambda: data  # type: ignore[assignment]
        hint = f"隔离数据目录：{data}（跑完自动删除）"
        cleanup = base
    else:
        hint = "真实数据目录（会写进资料库）"

    from app.core.db.schema import init_db

    init_db()
    return hint, cleanup


def _cleanup(base: Path | None, *, keep: bool) -> None:
    """关闭连接后删掉隔离数据（Windows 上必须先把 Qdrant/SQLite 关掉才能删）。"""
    if base is None or keep:
        return
    import contextlib
    import shutil

    from app.core.db.connection import close_thread_connection
    from app.core.vector import qdrant_store

    with contextlib.suppress(Exception):
        qdrant_store.close()  # 释放本地 Qdrant 的文件句柄
    close_thread_connection()  # 释放 SQLite 句柄
    shutil.rmtree(base, ignore_errors=True)
    print(f"\n已清理测试数据：{base}")


def _read_back(doc_id: int, version: int) -> tuple[list, list]:
    """从 Qdrant 读回这次写入的 child / parent（按 seq 排序）。"""
    from app.core.vector import api, qdrant_store

    children = [
        point
        for point in qdrant_store.scroll_filter(
            api.COLLECTION_CHUNKS, {"documents_id": doc_id}, with_vectors=False
        )
        if int(dict(point.payload).get("version", -1)) == version
    ]
    parents = [
        point
        for point in qdrant_store.scroll_filter(
            api.COLLECTION_PARENTS, {"documents_id": doc_id}, with_vectors=False
        )
        if int(dict(point.payload).get("version", -1)) == version
    ]
    children.sort(key=lambda point: int(dict(point.payload).get("seq", 0)))
    parents.sort(key=lambda point: int(dict(point.payload).get("seq", 0)))
    return children, parents


def _print_split(children: list, parents: list, limit: int) -> None:
    from app.core.chunk.derived import derive
    from app.core.chunk.estimate import estimate_tokens, real_token_counter
    from app.core.embed import PAD_TO

    counter = real_token_counter() or estimate_tokens
    sizes = derive(PAD_TO)
    by_parent: dict[str, list] = {}
    for point in children:
        by_parent.setdefault(str(dict(point.payload).get("parent_key") or ""), []).append(point)

    print(
        f"\n推导尺寸：child={sizes.child_size} overlap={sizes.child_overlap} "
        f"min={sizes.min_child_size} parent={sizes.parent_size}；PAD_TO={PAD_TO}"
    )
    print(f"结果：父块 {len(parents)} 个，子块 {len(children)} 个")

    over = [p for p in children if counter(str(dict(p.payload).get("text") or "")) > PAD_TO]
    if over:
        print(f"⚠ 有 {len(over)} 个子块超过 PAD_TO（会被嵌入截断）")

    for index, parent in enumerate(parents, start=1):
        payload = dict(parent.payload)
        key = str(payload.get("key") or "")
        kids = by_parent.get(key, [])
        section = payload.get("section_path") or "（无标题）"
        text = str(payload.get("text") or "")
        print(f"\n──────── 父块 {index}/{len(parents)}：{section} ────────")
        print(
            f"  parent：{counter(text)} tok / {len(text)} 字 / "
            f"{payload.get('content_type')} / children={len(kids)}"
        )
        shown = kids[:limit] if limit else kids
        for child_index, child in enumerate(shown, start=1):
            child_payload = dict(child.payload)
            child_text = str(child_payload.get("text") or "")
            preview = " ".join(child_text.split())[:60]
            page = child_payload.get("page")
            where = f" / 第 {page} 页" if page else ""
            print(
                f"    ├ [{child_index}] {counter(child_text)} tok / {len(child_text)} 字{where}"
            )
            print(f"    │   {preview}")
        if limit and len(kids) > limit:
            print(f"    └ … 还有 {len(kids) - limit} 个子块未显示（--limit 0 看全部）")


def main() -> int:
    _configure_stdout()
    _require_runtime()

    parser = argparse.ArgumentParser(description="直接调 importer.import_path，看正常入库后的切分")
    parser.add_argument("path", nargs="?", help="要导入的文件路径（也可以给一个目录）")
    parser.add_argument("--real", action="store_true", help="写进真实数据目录（默认隔离）")
    parser.add_argument("--limit", type=int, default=0, help="每个父块最多显示几个子块（0=全部）")
    parser.add_argument("--keep", action="store_true", help="跑完保留隔离数据（默认自动删除）")
    parser.add_argument("--clean", action="store_true", help="只删除隔离数据目录（.tmp/inspect-run）后退出")
    args = parser.parse_args()

    if args.clean:
        _cleanup(ROOT / ".tmp" / "inspect-run", keep=False)
        return 0

    if not args.path:
        print("缺少文件路径（或使用 --clean 只清理）", file=sys.stderr)
        return 2
    path = _resolve_input(Path(args.path).expanduser())
    if path is None:
        return 2

    hint, cleanup = _bootstrap(not args.real)
    print(f"数据目录：{hint}")
    try:
        # **正路**：直接调用入库入口（真实模型、判重、切分、写 Qdrant 全都在里面）
        from app.rag.ingest import importer

        result = importer.import_path(path, path.name)
        print(f"\n导入结果：status={result.status}，chunks={result.chunks}，doc_id={result.doc_id}")
        if result.reason:
            print(f"原因：{result.reason}")
        if result.notes:
            print("提示：" + "；".join(result.notes))

        if result.status != "imported" or result.doc_id is None:
            return 0 if result.status == "skipped" else 1

        # 读回这次写入的片段（按 documents_id 过滤）
        from app.rag import store

        row = store.get(int(result.doc_id))
        version = int(row["version"]) if row is not None else 1
        children, parents = _read_back(int(result.doc_id), version)
        _print_split(children, parents, max(0, args.limit))
        return 0
    finally:
        # 测试完就把数据删掉（--keep 可保留）
        _cleanup(cleanup, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
