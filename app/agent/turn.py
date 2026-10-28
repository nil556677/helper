"""Turn ↔ ChatState 的映射（UI 契约在 `app/ui/state/chat_model.py`，这里只转换）。"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意类型

from app.ui.state.chat_model import Fragment, Process, Step, Turn  # UI 数据形状

_FRAGMENT_FIELDS = frozenset(  # Fragment 允许的字段
    {"index", "source_name", "origin", "score", "text", "snippet", "score_kind"}  # 字段集合
)  # frozenset 结束


def _step(item: dict[str, Any]) -> Step:  # 状态里的一条步骤 -> UI 的 Step
    """逐字段挑，不整包透传：旧数据只有 kind，新数据是 group（见类 A 的 A3）。"""
    return Step(  # 步骤
        group=str(item.get("group") or item.get("kind") or "search"),  # 分组（旧数据回落 kind）
        title=str(item.get("title") or ""),  # 标题
        detail=str(item.get("detail") or ""),  # 明细
        status=str(item.get("status") or "done"),  # 状态
    )  # 步骤结束


def turn_from_state(state: dict[str, Any]) -> Turn:  # 把图状态还原成一轮答复
    """把图状态还原成 UI 的一轮答复（只保留 Turn 契约字段）。"""
    steps = [_step(item) for item in (state.get("steps") or [])]  # 步骤列表
    process = None  # 默认没有过程组
    if steps:  # 有步骤才画过程组
        process = Process(  # 组装过程组
            running="",  # 已完成，没有运行文案
            done=steps[-1].title if steps else "",  # 用最后一步当标题
            steps=steps,  # 步骤列表
            reasoning="",  # 没有推理预览
            duration_ms=None,  # 没有耗时
        )  # 过程组结束
    fragments = [  # 引用片段列表
        Fragment(**{key: value for key, value in item.items() if key in _FRAGMENT_FIELDS})  # 只保留契约字段
        for item in (state.get("fragments") or [])  # 遍历状态里的片段
    ]  # 列表结束
    return Turn(  # 返回一轮答复
        process=process,  # 过程组
        answer=str(state.get("answer") or ""),  # 回答正文
        fragments=fragments,  # 引用片段
        note=str(state.get("note") or ""),  # 一句说明
        note_kind=str(state.get("note_kind") or "plain"),  # 说明样式
    )  # Turn 结束


def as_fragment_dict(index: int, *, text: str, snippet: str = "", source_name: str = "资料",  # 造片段字典
                    origin: str = "", score: float = 0.0, score_kind: str = "cosine") -> dict[str, Any]:  # 造片段字典
    return {  # 片段字典
        "index": int(index),  # 序号
        "source_name": source_name,  # 来源名
        "origin": origin,  # 出处
        "score": float(score),  # 分数
        "text": text,  # 正文
        "snippet": snippet,  # 命中片段
        "score_kind": score_kind,  # 分数语义
    }  # 字典结束
