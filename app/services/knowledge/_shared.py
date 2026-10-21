"""知识库包的共享视图模型（界面只读它们；`from_row` 是「行 → 视图」的唯一映射处）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DocumentView:
    """资料列表里的一行。"""

    id: int
    name: str
    suffix: str
    size: int
    chunks: int
    status: str
    detail: str
    updated_at: float
    #: 解析摘要（P1.11 行内展示）：总页数 / 待 OCR 页 / 正文字符数
    pages: int | None = None
    ocr_pages: tuple[int, ...] = ()
    chars: int | None = None
    #: 向量化进度（P1.11 行内"处理中 3/12"）；非处理中状态两者相等
    vectors_done: int = -1
    vectors_total: int = -1

    @classmethod
    def from_row(cls, row: Any) -> DocumentView:
        """一行 documents → 界面直接能画的字段（**唯一**映射处）。"""
        name = str(row["name"])
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        return cls(
            id=int(row["id"]),
            name=name,
            suffix=suffix.lstrip("."),
            size=int(row["size"]),
            chunks=int(row["chunks"]),
            status=str(row["status"]),
            detail=str(row["detail"] or ""),
            updated_at=float(row["updated_at"]),
            pages=None if row["pages"] is None else int(row["pages"]),
            ocr_pages=tuple(json.loads(str(row["ocr_pages"]))) if row["ocr_pages"] else (),
            chars=None if row["chars"] is None else int(row["chars"]),
        )


@dataclass(frozen=True)
class KnowledgeStats:
    """概览：份数 / 片段 / 占用 / 向量覆盖率 / 失败与处理中（P1.15 + P1.17）。"""

    documents: int
    failed: int
    chunks: int
    bytes: int
    updated_at: float | None
    #: 处理中（排队或正在解析/建向量）的份数
    indexing: int = 0
    #: 向量占用（Qdrant 集合的字节量；目录总量另算）
    vector_bytes: int = 0
    #: 已向量化 / 应向量化的子块数
    vectors_done: int = 0
    vectors_total: int = 0
    #: 覆盖率百分比（0–100；没有片段时按 100 算）
    vector_ratio: int = 100
    #: 向量后端暂时不可用时的原因（非空 → 界面上把这几个数标成"暂不可用"，不显示 0 误导）
    vector_error: str = ""


@dataclass(frozen=True)
class DocumentPage:
    """资料列表的一页（服务端分页的结果）。"""

    items: tuple[DocumentView, ...]
    total: int
    page: int
    pages: int
    size: int


@dataclass(frozen=True)
class RebuildOutcome:
    """重建一份资料的结果。"""

    ok: bool
    detail: str
    chunks: int
