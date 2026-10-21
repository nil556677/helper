"""知识库的上传/登记相关业务工具（暂存、判重键、格式与片段上限说明）。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from uuid import uuid4

from app.core import paths
from app.core.parse import supported_suffixes
from app.rag import store

#: 单个上传文件的上限（界面与**服务端**共用这一个数：前端拦一次，落盘后再校验一次）。
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class UploadTooLarge(Exception):
    """落盘后的文件超过上限（防绕过前端的超大上传）。"""


def check_size(path: Path, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """落盘后校验大小；超限抛 :class:`UploadTooLarge`（调用方负责删半成品）。"""
    size = path.stat().st_size
    if size > max_bytes:
        raise UploadTooLarge(
            f"文件 {size // 1024 // 1024} MB，超过上限 {max_bytes // 1024 // 1024} MB"
        )


def discard_pending(doc_id: int | str | None) -> None:
    """**入队失败的回滚**：删掉刚落的占位行（还没开始导入，删了最干净）。

    为什么需要：``register_staged`` 先落占位行、再 ``enqueue_import`` 排队——两步不在
    同一事务里。入队抛异常（DB 忙/队列不可用）时若不回滚，占位行会以"待处理"挂在列表里
    最长 24 小时才被清理，用户看到一份永远不动的资料。
    """
    if doc_id is None:
        return
    try:
        store.delete_many([int(doc_id)])
    except Exception:
        from loguru import logger

        logger.warning("回滚占位行失败（doc {}）", doc_id)


def supported_formats() -> str:
    """支持哪些格式（**从解析表生成**，避免界面文字与代码漂移）。"""
    return "、".join(supported_suffixes())


def is_supported_format(name: str) -> bool:
    """这份文件的**后缀**是不是支持的格式（上传时先查，不支持直接拦下）。"""
    return _suffix_of(name) in supported_suffixes()


def unsupported_hint(name: str) -> str | None:
    """不支持的格式给一句人话（老式 Office 另存为 / 图片走 OCR），没有就 None。"""
    from app.core.parse import UNSUPPORTED_HINTS

    return UNSUPPORTED_HINTS.get(_suffix_of(name))


def _suffix_of(name: str) -> str:
    """取小写后缀（``.docx``）；没有点返回 ``""``。"""
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def chunk_limit() -> str:
    """当前片段上限的**人话**（界面上说明用）。

    上限由**模型固定输入宽度**推导（``app/core/embed/model.py:PAD_TO``，单位是 bge-m3 token，
    不是字数）：子块 = ``PAD_TO`` − 安全余量。这里复算一遍再换算成字数，避免界面文字与代码漂移。
    """
    from app.core.chunk.derived import derive
    from app.core.embed import PAD_TO

    child = derive(PAD_TO).child_size
    return f"{child} token（约 {int(child / 0.69)} 字）"


def stage_upload(name: str, source) -> dict[str, str]:
    """把上传的**流**落进 ``data/inbox/``（1 MB 块边读边写，不整包进内存），
    顺带流式算 sha256（判重键）。返回导入任务的 payload。

    ``source`` 可以是 file-like（界面传的 ``event.content``，走流式）或 ``bytes``
    （测试/命令行，包一层读）。为什么先落盘：任务表里**只放引用**（路径/id），
    而且落了盘之后"导到一半关窗口"就变成"下次启动接着导"。
    """
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    target = inbox_target(name)
    digest = hashlib.sha256()
    with target.open("wb") as handle:
        while chunk := source.read(1 << 20):
            handle.write(chunk)
            digest.update(chunk)
    return _register_staged(name, target, digest.hexdigest())


def inbox_target(name: str) -> Path:
    """inbox 里给这次上传分配的落盘路径（``{uuid}_{安全文件名}``）。

    为什么要单独暴露：NiceGUI 3.18 的上传事件给的是 ``FileUpload``（异步流），
    界面层要用 ``await event.file.save(...)`` **流式落盘**——落完再把路径交回来登记。

    名字净化走 :func:`app.core.paths.safe_filename`（与副本落盘同一套规则：去路径、
    去非法字符、Windows 保留名加前缀、超长截断保后缀）；uuid 前缀 12 hex（48 bit）
    分不开的概率可忽略，仍用 `exists` 兜底重抽一次。
    """
    folder = paths.inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    safe = paths.safe_filename(name)
    for _ in range(3):
        candidate = folder / f"{uuid4().hex[:12]}_{safe}"
        if not candidate.exists():
            return candidate
    return folder / f"{uuid4().hex}_{safe}"


def register_staged(name: str, path: Path) -> dict[str, str]:
    """文件**已经落进 inbox** 之后登记：算哈希 → 判重 → 落占位行（P1.16）。

    参数：
        name: 原始文件名（展示与判重用）。
        path: inbox 里的暂存路径（由 :func:`inbox_target` 分配、界面层写好）。
    返回：导入任务的 payload（``path``/``name``/``sha256``，命中重复时带 ``skip``，
        正常时带 ``doc_id``）。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return _register_staged(name, path, digest.hexdigest())


def _register_staged(name: str, target: Path, sha256: str) -> dict[str, str]:
    """落占位行 + 判重的唯一实现（``stage_upload`` 与 ``register_staged`` 共用）。

    **查重与插入必须在同一个写事务里**（P7 审查 H5）：分两步做时两个并发上传都能
    "查空 → 各插一条"，留下一条永远没人认领的占位行（挂到 24h 才清）。这里用
    ``BEGIN IMMEDIATE`` 的写事务把两步串起来——第二个上传会看到第一个刚插的行并转为 skip。
    """
    payload = {"path": str(target), "name": Path(name).name, "sha256": sha256}

    # **入队前先落一行占位**（P1.16）：列表立刻能看到"待处理"，导入任务认领同一个 id。
    # 同一份内容**已经在库里或在队列里**时不落占位（直接告诉调用方"跳过"，连任务都不排）。
    # 只排除 failed：失败行允许重传（pipeline 会清掉失败行再做）；
    # pending/ready/indexing/superseded 都算"已有"——否则同一文件并发的第二次上传会多落
    # 一条永远没人认领的占位行。
    from app.core.db.connection import transaction

    with transaction() as conn:
        existing = store.find_id_by(
            "file_hash",
            sha256,
            statuses=("pending", "ready", "indexing", "superseded"),
            conn=conn,
        )
        if existing is not None:
            payload["skip"] = str(existing)
            return payload
        doc_id = store.insert_pending(
            payload["name"],
            size=target.stat().st_size,
            file_hash=sha256,
            conn=conn,
        )
    payload["doc_id"] = str(doc_id)
    return payload


def upload_dedupe_key(sha256: str | bytes) -> str:
    """按**内容**算去重键（stage_upload 流式算好 sha256；传 bytes 时转 hex）。

    同一份文件连点两次不会排两条任务。
    """
    if isinstance(sha256, bytes):
        sha256 = sha256.hex()
    return "import:" + sha256[:32]
