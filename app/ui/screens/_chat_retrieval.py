"""聊天界面的**真实数据源**：dense 语义检索 → 父块证据回填。

替代原来的模拟数据（``_chat_mock.py``）：命中 child 后把各自所在父块（章节上下文）
取回来作为引用证据，"子块负责检索、父块喂给模型"正是这条链的自然落地。

数据形状仍是 :class:`~app.ui.state.chat_model.Turn`，渲染器不用改。
"""

from __future__ import annotations

from time import perf_counter

from app.core.vector import api
from app.rag import store as rag_store
from app.rag.plan import RAG_PLAN
from app.ui.state import conversations as store
from app.ui.state.chat_model import Fragment, Process, Turn

#: 召回候选条数（dense top-k；父块去重后再展示）
TOPK = 6


def _doc_name(doc_id: object) -> str:
    if not doc_id:
        return "资料"
    try:
        row = rag_store.get(int(doc_id))
    except Exception:
        return "资料"
    if row is None:
        return "资料"
    return str(dict(row).get("name") or "资料")


def _origin(payload: dict) -> str:
    """把 payload 的定位信息翻译成人类可读的出处（第几页/第几段/表格行）。"""
    loc = payload.get("loc") or {}
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
    if loc.get("page"):
        return f"第 {loc.get('page')} 页"
    if loc.get("para") is not None:
        return f"第 {int(loc.get('para', 0)) + 1} 段"
    heading = str(payload.get("heading") or "")
    return heading or "资料"


def _answer(fragments: list[Fragment]) -> str:
    """**抽取式**答案：把检索命中的父块原文列出来（本应用不接生成式 LLM，不组稿）。"""
    if not fragments:
        return ""
    blocks = [
        f"#### {fragment.source_name} · {fragment.origin}\n\n{fragment.text}"
        for fragment in fragments[:5]
    ]
    return (
        "\n\n---\n\n".join(blocks)
        + "\n\n> 以上为本地资料原文（未改写）。检索链路：dense 语义召回 child → 回填父块证据。"
    )


def pick_turn(question: str = "") -> Turn:
    """按真实检索结果造一轮答复。

    参数：
        question: 用户输入。
    返回：:class:`~app.ui.state.chat_model.Turn`（过程行 + 抽取式答案 + 引用父块）。
    """
    start = perf_counter()
    hits = RAG_PLAN.retriever.search(question, limit=TOPK)
    elapsed_ms = (perf_counter() - start) * 1000

    if not hits:
        return Turn(
            process=Process(
                running="正在检索本地资料…",
                done=f"检索本地资料 · 命中 0 条 · {elapsed_ms:.0f} ms",
                details=[f"查询词：{question}", "没有片段命中这些词"],
            ),
            note="没有在本地资料里找到相关内容。换个说法试试。",
        )

    # ---- 命中 child → 按 parent_key 批量取回父块证据 ----
    parent_keys = [str(h.payload.get("parent_key")) for h in hits if h.payload.get("parent_key")]
    parents: dict[str, dict] = {}
    if parent_keys:
        rows = api.iter_points(api.COLLECTION_PARENTS, {"key": parent_keys})
        for row in rows:
            payload = dict(row["payload"] or {})
            key = str(payload.get("key") or "")
            if key:
                parents[key] = payload

    document_ids = sorted({int(h.payload.get("documents_id")) for h in hits if h.payload.get("documents_id")})
    names = {doc_id: _doc_name(doc_id) for doc_id in document_ids}

    fragments: list[Fragment] = []
    seen: set[str] = set()
    details: list[str] = [f"查询词：{question}"]
    for hit in hits:
        payload = hit.payload
        parent_key = str(payload.get("parent_key") or "")
        loc = payload.get("loc") or {}
        # 表格：同一张表的普通行共享表级 parent，不能按 parent_key 去重（会把不同行吞掉）；
        # 按 row_from 去重，保证每行各给一条证据。
        if loc.get("kind") == "table":
            dedupe_key = f"{parent_key}:{loc.get('row_from')}:{loc.get('col_from')}"
        else:
            dedupe_key = parent_key or hit.key
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        parent = parents.get(parent_key) if parent_key else None
        parent_text = str(parent.get("text") or "") if parent else ""
        parent_loc = dict(parent.get("loc") or {}) if parent else {}
        # 普通行 parent 只有表摘要：证据 = 摘要 + 命中行；
        # 超宽行 parent 已含完整行：直接用 parent，避免重复拼半行。
        if parent and parent_text:
            if parent_loc.get("row_from") == loc.get("row_from") and loc.get("row_from") is not None:
                text = parent_text
            elif hit.text and hit.text not in parent_text:
                text = f"{parent_text}\n\n{hit.text}"
            else:
                text = parent_text
        else:
            text = hit.text
        heading = str(parent.get("heading") or payload.get("heading") or "")
        document_id = int(payload.get("documents_id")) if payload.get("documents_id") else None
        source_name = names.get(document_id, "资料")
        origin = heading if heading else _origin(payload)
        details.append(f"#{len(fragments) + 1} {hit.score:.2f} · {source_name} · {origin}")
        fragments.append(
            Fragment(
                index=len(fragments) + 1,
                source_name=source_name,
                origin=origin,
                score=hit.score,
                text=text,
            )
        )

    return Turn(
        process=Process(
            running="正在检索本地资料…",
            done=f"检索本地资料 · 命中 {len(fragments)} 条 · {elapsed_ms:.0f} ms",
            details=details,
        ),
        answer=_answer(fragments),
        fragments=fragments,
    )


def ensure_seeded() -> None:
    """第一次进来时建一个**空会话**（真实检索源不需要示例假数据）。

    有会话就用现有的；没有就建一个空的，好让"新对话"这一屏能正常渲染。
    """
    if store.conversations():
        return
    store.new(title="新对话")
