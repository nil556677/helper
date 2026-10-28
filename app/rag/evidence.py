"""证据组装（UI-free）：命中 child → SQLite 回填父块 → 去重 → Fragment 列表。"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.documents import Document

from app.rag import kb_store


def _loc_dict(row_or_meta: dict) -> dict:
    """兼容两种形状：SQLite 行的 `loc_json` 字符串，或 Qdrant metadata 的 `loc` dict。"""
    raw = row_or_meta.get("loc_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    if isinstance(raw, dict):
        return raw
    loc = row_or_meta.get("loc")
    return loc if isinstance(loc, dict) else {}


def loc_label(payload: dict | None) -> str:
    """把定位信息翻译成一句出处（页/行/表格/段落）。"""
    loc = payload or {}
    kind = loc.get("kind")
    if kind == "table":
        ref = str(loc.get("table") or "表格")
        parts = [ref]
        if loc.get("row_from") is not None:
            rows = f"第 {loc['row_from']} 行"
            if loc.get("row_to") and loc["row_to"] != loc["row_from"]:
                rows = f"第 {loc['row_from']}–{loc['row_to']} 行"
            parts.append(rows)
        if loc.get("col_from") is not None:
            cols = f"列 {loc['col_from']}"
            if loc.get("col_to") and loc["col_to"] != loc["col_from"]:
                cols = f"列 {loc['col_from']}–{loc['col_to']}"
            parts.append(cols)
        return " · ".join(parts)
    if loc.get("page") is not None:
        page = loc["page"]
        if loc.get("page_to") and loc["page_to"] != page:
            return f"第 {page}–{loc['page_to']} 页"
        return f"第 {page} 页"
    if loc.get("line_from") is not None:
        line_from = loc["line_from"]
        line_to = loc.get("line_to")
        if line_to and line_to != line_from:
            return f"第 {line_from}–{line_to} 行"
        return f"第 {line_from} 行"
    if loc.get("para") is not None:
        return f"第 {int(loc['para']) + 1} 段"
    return ""


def origin_label(heading: str, loc: str) -> str:
    if heading and loc and loc not in ("资料", heading):
        return f"{heading} · {loc}"
    if loc and loc != "资料":
        return loc
    return heading or "资料"


def _fetch_mode(meta: dict) -> int:  # 取数模式
    """0/1/2 都保留；只有缺省才回退 1（不能把 0 当假值）。"""
    value = meta.get("fetch_mode")  # 原值
    return 1 if value is None else int(value)  # 保留 0


def _dedupe_key(meta: dict) -> str:
    mode = _fetch_mode(meta)
    if mode == 2 and meta.get("row_uid"):
        return f"row:{meta.get('table_uid')}:{meta.get('row_uid')}"
    if mode == 0:
        return str(meta.get("chunk_key") or meta.get("chunk_id") or "")
    return str(meta.get("parent_id") or meta.get("chunk_key") or "")


def _row_key(meta: dict) -> tuple[int, int, str, str]:
    return (
        int(meta.get("doc_id") or 0),
        int(meta.get("version") or 0),
        str(meta.get("table_uid") or ""),
        str(meta.get("row_uid") or ""),
    )


def candidate_key(meta: dict) -> str:
    """一个“证据单位”：超宽行按 row、正文/行按 parent、独立块按 chunk。"""
    mode = _fetch_mode(meta)
    if mode == 2 and meta.get("row_uid"):
        return f"row:{meta.get('table_uid')}:{meta.get('row_uid')}"
    if mode == 0:
        return f"chunk:{meta.get('chunk_id') or meta.get('chunk_key')}"
    return f"parent:{meta.get('parent_id') or meta.get('chunk_key')}"


def compose_candidates(docs: list[Any]) -> list[Document]:
    """精排前把命中段归并成“证据单位”，并预取 parent/兄弟段拼好文本。"""
    versions = kb_store.active_versions(
        int((doc.metadata or {}).get("doc_id") or 0) for doc in docs
    )
    chosen: dict[str, dict[str, Any]] = {}
    for doc in docs:
        meta = dict(doc.metadata or {})
        doc_id = int(meta.get("doc_id") or 0)
        if versions and versions.get(doc_id) != int(meta.get("version") or 0):
            continue  # 旧版本/已删除的 Qdrant 点
        key = candidate_key(meta)
        score = float(meta.get("score") or 0.0)
        if key not in chosen:
            chosen[key] = {"doc": doc, "meta": meta, "score": score, "segments": set()}
        else:
            chosen[key]["score"] = max(chosen[key]["score"], score)
        if meta.get("segment_index") is not None and int(meta["segment_index"]) >= 0:
            chosen[key]["segments"].add(int(meta["segment_index"]))

    parent_ids = [
        int(item["meta"]["parent_id"])
        for item in chosen.values()
        if item["meta"].get("parent_id") is not None
        and _fetch_mode(item["meta"]) != 0
    ]
    parents = kb_store.parents_for(parent_ids)
    chunk_ids = [
        int(item["meta"]["chunk_id"])
        for item in chosen.values()
        if _fetch_mode(item["meta"]) == 0 and item["meta"].get("chunk_id")
    ]
    chunks = kb_store.chunks_by_ids(chunk_ids)
    row_keys = [
        _row_key(item["meta"])
        for item in chosen.values()
        if _fetch_mode(item["meta"]) == 2
    ]
    rows = kb_store.rows_for(row_keys)

    out: list[Document] = []
    for item in chosen.values():
        doc = item["doc"]
        meta = item["meta"]
        mode = _fetch_mode(meta)
        snippet = str(doc.page_content or "")
        parent = parents.get(int(meta.get("parent_id") or 0), {})
        parent_text = str(parent.get("text") or "")
        heading = str(parent.get("heading") or meta.get("heading") or "")
        relation_ok = True

        if mode == 0:
            chunk = chunks.get(int(meta.get("chunk_id") or 0), {})
            text = str(chunk.get("text") or snippet)
            relation_ok = bool(chunk) or not meta.get("chunk_id")
            if str(meta.get("role") or "") == "table_row" and heading and heading not in text:
                text = f"{heading}\n{text}"
        elif mode == 2:
            segments = rows.get(_row_key(meta), [])
            expected = int(meta.get("segment_count") or 1)
            indexes = [int(row.get("segment_index") or 0) for row in segments]
            relation_ok = len(segments) == expected and indexes == list(range(expected))
            body = "\n".join(str(row.get("text") or "") for row in segments)
            text = f"{parent_text}\n{body}" if parent_text and body else (body or parent_text or snippet)
        else:
            text = parent_text or snippet
            relation_ok = bool(parent_text) or not meta.get("parent_id")

        new_meta = {
            **meta,
            "score": item["score"],
            "evidence_text": text,
            "snippet": snippet,
            "heading": heading,
            "relation_ok": relation_ok,
            "segment_indexes": sorted(item["segments"]),
        }
        out.append(Document(page_content=text, metadata=new_meta))
    return out


def _compose_siblings(
    meta: dict, parent_text: str, snippet: str
) -> tuple[str, bool]:
    """`fetch_mode=2`：按 segment_index 拼回整行；返回 (文本, 关系是否完整)。"""
    siblings = kb_store.siblings_for(
        int(meta.get("doc_id") or 0),
        int(meta.get("version") or 0),
        str(meta.get("table_uid") or ""),
        str(meta.get("row_uid") or ""),
    )
    expected = int(meta.get("segment_count") or 1)
    indexes = [int(row.get("segment_index") or 0) for row in siblings]
    complete = len(siblings) == expected and indexes == list(range(expected))
    if not siblings:
        return (parent_text or snippet), False
    body = "\n".join(str(row.get("text") or "") for row in siblings)
    text = f"{parent_text}\n{body}" if parent_text else body
    return text, complete


def _table_cap() -> int:
    """同一张表最多占几条证据（避免一张宽表刷满 top-k）。"""
    try:
        return max(1, int(os.environ.get("HELPER_TABLE_EVIDENCE_CAP", "2")))
    except (TypeError, ValueError):
        return 2


def _documents(doc_ids: list[int]) -> dict[int, dict]:
    """批量取文档行；读库失败就当空（调用方按"不可溯源"处理）。"""
    try:  # 库异常不该让检索失败
        return kb_store.documents_by_ids(doc_ids)  # 批量
    except Exception:  # 读失败
        return {}  # 空


def build_evidence(docs: list[Any], *, top_k: int = 6) -> list[dict[str, Any]]:
    """把（已精排的）命中 child 组装成证据片段列表（Fragment 的 dict 形状）。

    取数按 `fetch_mode` 分派：
    - 0：直接用 child 文本（完整行/摘要）；
    - 1：取 parent 文本；
    - 2：取 parent + 同一 `row_uid` 的全部段，按 `segment_index` 拼回。
    """
    parent_ids = [
        int(doc.metadata["parent_id"])
        for doc in docs
        if (doc.metadata or {}).get("parent_id") is not None
        and _fetch_mode(doc.metadata or {}) != 0
        and "evidence_text" not in (doc.metadata or {})
    ]
    parents = kb_store.parents_for(parent_ids)
    documents = _documents([int(doc.metadata.get("doc_id") or 0) for doc in docs])  # 溯源用（text_hash）

    def _trace_fields(meta: dict) -> dict[str, Any]:  # 溯源字段
        """引用校验要用：这条资料现在还能不能回查（方案 §5.3）。"""
        doc_id = int(meta.get("doc_id") or 0)  # 文档 id
        return {  # 五个字段
            "doc_id": doc_id,  # 文档
            "chunk_id": int(meta.get("chunk_id") or 0),  # 命中 child
            "parent_id": int(meta.get("parent_id") or 0),  # 父块
            "version": int(meta.get("version") or 0),  # 索引版本
            "text_hash": str(documents.get(doc_id, {}).get("text_hash") or ""),  # 内容指纹
        }  # 字段结束

    fragments: list[dict[str, Any]] = []
    seen: set[str] = set()
    table_counts: dict[str, int] = {}
    cap = _table_cap()
    for doc in docs:
        meta = doc.metadata or {}
        parent_id = meta.get("parent_id")
        mode = _fetch_mode(meta)
        loc = meta.get("loc") or {}
        dedupe_key = _dedupe_key(meta)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        if "evidence_text" in meta:
            if meta.get("relation_ok") is False:
                continue
            heading = str(meta.get("heading") or "")
            snippet = str(meta.get("snippet") or doc.page_content or "")
            text = str(meta.get("evidence_text") or "")
            origin = origin_label(heading, loc_label(meta.get("loc") or {}))
            table_uid = str(meta.get("table_uid") or "")
            if table_uid and table_counts.get(table_uid, 0) >= cap:
                continue
            fragments.append(
                {
                    "index": len(fragments) + 1,
                    "source_name": str(meta.get("source_name") or "资料"),
                    "origin": origin,
                    "score": float(meta.get("score") or 0.0),
                    "score_kind": str(meta.get("score_kind") or "rrf"),
                    "text": text,
                    "snippet": snippet,
                    "chunk_key": str(meta.get("chunk_key") or ""),
                    "heading": heading,
                    "loc": loc,
                    "fetch_mode": mode,
                    "relation_ok": True,
                    "segment_indexes": meta.get("segment_indexes") or [],
                    **_trace_fields(meta),  # 溯源五字段
                }
            )
            if table_uid:
                table_counts[table_uid] = table_counts.get(table_uid, 0) + 1
            if len(fragments) >= top_k:
                break
            continue

        parent = parents.get(int(parent_id), {}) if parent_id is not None else {}
        parent_text = str(parent.get("text") or "")
        heading = str(parent.get("heading") or meta.get("heading") or "")
        snippet = str(doc.page_content or "")
        relation_ok = True

        if mode == 0:
            text = snippet
            if str(meta.get("role") or "") == "table_row" and heading and heading not in text:
                text = f"{heading}\n{text}"
        elif mode == 2:
            text, relation_ok = _compose_siblings(meta, parent_text, snippet)
        else:
            text = parent_text if snippet in parent_text else f"{parent_text}\n\n{snippet}"

        origin = origin_label(
            heading, loc_label(_loc_dict(parent) or meta.get("loc") or {})
        )
        table_uid = str(meta.get("table_uid") or "")
        if table_uid and table_counts.get(table_uid, 0) >= cap:
            continue
        fragments.append(
            {
                "index": len(fragments) + 1,
                "source_name": str(meta.get("source_name") or "资料"),
                "origin": origin,
                "score": float(meta.get("score") or 0.0),
                "score_kind": str(meta.get("score_kind") or "rrf"),
                "text": text,
                "snippet": snippet,
                "chunk_key": str(meta.get("chunk_key") or ""),
                "heading": heading,
                "loc": loc,
                "fetch_mode": mode,
                "relation_ok": relation_ok,
                **_trace_fields(meta),  # 溯源五字段
            }
        )
        if table_uid:
            table_counts[table_uid] = table_counts.get(table_uid, 0) + 1
        if len(fragments) >= top_k:
            break
    return fragments
