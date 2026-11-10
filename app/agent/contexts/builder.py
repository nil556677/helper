"""ContextBuilder：唯一装配点（生产级，结合记忆自动拼接）。

管线：Prune(相关度剪枝) → Restructure(紧凑标签块) → Position(锚前/问题末)
     → Budget(分层截断) → Compose(system+messages+usage)。

顺序永远：S(固定) → F(半动态：working/facts/segments/dialog 自动拼接) → V(动态：tools/attachments) → 当前问题(最末)。
锚点(<working> + <question> + S 核心)永不压；key_facts/question 永不丢。
"""

from __future__ import annotations  # 延迟值注解

from collections.abc import Callable  # 类型
from dataclasses import dataclass, field  # 输入容器
from typing import Any

from app.agent.contexts.budget import Budget  # 预算
from app.agent.memory.longterm import cosine_sim  # 相关度（近轮剪枝）

__all__ = ["Block", "ContextOut", "build_context"]  # 导出

#: 默认常量（可配置）。
KEEP_TURNS = 6  # 近轮无条件保留轮数
PRUNE_THETA = 0.5  # 更旧近轮相关度阈值
F_TOP_K = 6  # 事实召回上限
KEEP_SEGMENTS = 2  # 段保留数
RATIO_F = 0.35  # 记忆区(F)占可用预算比例

_EMBED: Callable[[str], list[float] | None] | None = None  # 可注入嵌入（无则退保底）


def set_embed(embed: Callable[[str], list[float] | None] | None) -> None:  # 注入嵌入函数
    """生产接 bge-m3；测试/无模型时传 None（近轮剪枝退保底 keep）。"""
    global _EMBED  # 改模块级
    _EMBED = embed  # 存


def _sim(text: str, question: str) -> float | None:  # 语义相似度
    """有嵌入才算；否则 None（退保底规则）。"""
    if _EMBED is None or not text:  # 无嵌入/空
        return None  # 退保底
    try:  # 嵌入越界
        a = _EMBED(str(question or ""))  # 问题向量
        b = _EMBED(str(text or ""))  # 文本向量
        if a and b:  # 都有
            return cosine_sim(a, b)  # 余弦
    except Exception:  # 失败
        return None  # 退保底
    return None  # 无


@dataclass
class Block:  # 一块上下文（有序）
    """tag + 文本，供 position/预算/观测。"""

    tag: str  # 块标签
    text: str  # 渲染文本
    tokens: int = 0  # token（渲染后计数）

    def __post_init__(self) -> None:  # 构造后计数
        from app.agent.contexts.tokens import count_text  # 局部导入避环

        self.tokens = int(count_text(str(self.text or "")))  # 计


@dataclass
class ContextOut:  # 装配结果（可落 RunContext）
    system: str  # system：S + <working>（锚点区）
    messages: list[dict[str, Any]]  # 消息：facts/segments/dialog… + 问题
    blocks: list[Block]  # 有序块（观测）
    pruned: dict[str, int] = field(default_factory=dict)  # 各层剪掉计数
    dropped: dict[str, int] = field(default_factory=dict)  # 各层预算丢弃
    cache_prefix_tokens: int = 0  # 稳定前缀(S) token（Prompt Cache 标记用）
    constraints_kept: float = 0.0  # 关键约束保留率（key_facts+hints 落在终上下文的比例，0..1）

    def to_state_dict(self) -> dict[str, Any]:  # 可序列化视图（进 ChatState，供回答节点用）
        """装进 state：system / messages / usage，供 ReAct/dispatch/general 消费。"""
        from app.agent.contexts.tokens import count_messages  # 估算

        return {  # 视图
            "system": self.system,  # system 文本
            "messages": [dict(m) for m in self.messages],  # 消息
            "pruned": dict(self.pruned),  # 剪枝
            "dropped": dict(self.dropped),  # 丢弃
            "cache_prefix_tokens": self.cache_prefix_tokens,  # 缓存前缀
            "constraints_kept": self.constraints_kept,  # 约束保留率
            "estimate": int(count_messages(self.messages)),  # 消息 token
        }  # 结束


def _render_working(working: dict[str, Any]) -> str:  # <working> 锚点块
    """紧凑状态行（goal/step/key_facts），永不压。"""
    out: list[str] = []  # 行
    if working.get("task_goal"):  # 有目标
        out.append(f"goal={working.get('task_goal')}")  # 目标
    if working.get("current_step"):  # 有当前步
        out.append(f"step={working.get('current_step')}")  # 步
    facts = working.get("key_facts") or {}  # 关键事实
    if facts:  # 有
        out.append("facts=" + ", ".join(f"{k}={v}" for k, v in facts.items()))  # 合并
    if not out:  # 空
        return ""  # 空块
    return "<working>\n" + "\n".join(out) + "\n</working>"  # 成块


def _render_facts(facts: list[dict[str, Any]], top: int) -> list[str]:  # <facts> 块行
    """每条带可信度/来源，取 top。"""
    lines: list[str] = []  # 行
    for item in (facts or [])[:top]:  # 取前 top
        conf = float(item.get("confidence") or 0)  # 可信度
        src = str(item.get("source") or "")  # 来源
        lines.append(f"- [{conf:.2f}] {item.get('key')}={item.get('value')} (source={src})")  # 一行
    return lines  # 返回


def _render_dialog(recent: list[dict[str, Any]], keep: int) -> list[str]:  # <dialog> 块行
    """近轮压缩成 q/a 一行。"""
    lines: list[str] = []  # 行
    for turn in (recent or [])[-keep:]:  # 取最近 keep 轮
        q = str(turn.get("user") or "")  # 问题
        a = str(turn.get("assistant") or "")[:80]  # 回答（截断）
        lines.append(f"q: {q}" + (f" | a: {a}" if a else ""))  # 一行
    return lines  # 返回


def _render_segments(segments: list[dict[str, Any]], keep: int) -> list[str]:  # <segments> 块行
    """最近段，各带 range。"""
    lines: list[str] = []  # 行
    for seg in (segments or [])[:keep]:  # 取最近 keep 段
        recap = (str(seg.get("summary") or "")).strip().replace("\n", " ")  # 摘要去换行
        lines.append(f"[{seg.get('seg_seq')}] {recap[:120]}")  # 一行
    return lines  # 返回


def _render_tools(tools: list[dict[str, Any]]) -> list[str]:  # <tools> 块行
    lines: list[str] = []  # 行
    for item in (tools or [])[:3]:  # 最多 3 条
        lines.append(f"{item.get('name')}: {str(item.get('detail') or '')[:60]}")  # 一行
    return lines  # 返回


def _render_attachments(attachments: list[dict[str, Any]]) -> list[str]:  # <attachments> 块行
    lines: list[str] = []  # 行
    for item in (attachments or [])[:3]:  # 最多 3 条
        lines.append(f"{item.get('source_name') or '附件'}：{str(item.get('snippet') or '')[:60]}")  # 一行
    return lines  # 返回


def _prune_recent(recent: list[dict[str, Any]], question: str, keep: int) -> tuple[list[dict[str, Any]], int]:  # 近轮剪枝
    """最近 keep 轮保底；更旧按语义相关度 / 退保底规则裁。"""
    items = list(recent or [])  # 拷贝
    if len(items) <= keep:  # 本来就不多
        return items, 0  # 全留
    keep_part = items[-keep:]  # 短期保底
    older = items[:-keep]  # 更旧
    kept_older: list[dict[str, Any]] = []  # 留下的更旧轮
    for turn in older:  # 逐更旧轮
        sim = _sim(str(turn.get("user") or ""), question)  # 相关度
        if sim is None or sim >= PRUNE_THETA:  # 相关（或无嵌入则保守留）
            kept_older.append(turn)  # 留
    pruned = len(older) - len(kept_older)  # 剪掉数
    return kept_older + keep_part, pruned  # 合并返回


def build_context(  # 装配一次
    *,
    system: str,  # S 核心系统文本
    working: dict[str, Any],  # 工作记忆（锚点）
    facts: list[dict[str, Any]] = (),  # 长期事实（search 结果）
    segments: list[dict[str, Any]] = (),  # 分段摘要
    recent: list[dict[str, Any]] = (),  # 近轮
    tools: list[dict[str, Any]] = (),  # 工具结果
    attachments: list[dict[str, Any]] = (),  # 附件
    hints: list[str] = (),  # 护栏提示（候选/意图确认/追问预算，注入 system 防止追问循环）
    profile_text: str = "",  # 画像块文本（稳定约束，优先级高于可变事实）
    question: str = "",  # 当前问题
    budget: Budget | None = None,  # 预算
    keep_turns: int = KEEP_TURNS,  # 近轮保底
    f_topk: int = F_TOP_K,  # 事实上限
    keep_segments: int = KEEP_SEGMENTS,  # 段保留
    ratio_f: float = RATIO_F,  # 记忆区比例
) -> ContextOut:  # 结果
    """prune→restructure→position→budget→compose。"""
    used = budget or Budget()  # 预算
    pruned: dict[str, int] = {}  # 剪枝计数
    recent, pruned["recent"] = _prune_recent(list(recent or []), question, keep_turns)  # 近轮剪枝

    # —— Restructure：各层成紧凑块（有序数组：position 已隐含）——
    working_block = _render_working(working)  # <working> 锚点
    fact_lines = _render_facts(facts, f_topk)  # 事实行
    seg_lines = _render_segments(segments, keep_segments)  # 段行
    dialog_lines = _render_dialog(recent, keep_turns)  # 近轮行
    tool_lines = _render_tools(tools)  # 工具行
    att_lines = _render_attachments(attachments)  # 附件行

    blocks: list[Block] = []  # 有序块
    if profile_text:  # 有画像
        blocks.append(Block("profile", f"<profile>\n{str(profile_text).strip()}\n</profile>"))  # 画像块（稳定约束最先）
    if fact_lines:  # 有事实
        blocks.append(Block("facts", "<facts>\n" + "\n".join(fact_lines) + "\n</facts>"))  # 事实块
    if seg_lines:  # 有段
        blocks.append(Block("segments", "<segments>\n" + "\n".join(seg_lines) + "\n</segments>"))  # 段块
    if dialog_lines:  # 有近轮
        blocks.append(Block("dialog", "<dialog>\n" + "\n".join(dialog_lines) + "\n</dialog>"))  # 近轮块
    if tool_lines:  # 有工具
        blocks.append(Block("tools", "<tools>\n" + "\n".join(tool_lines) + "\n</tools>"))  # 工具块
    if att_lines:  # 有附件
        blocks.append(Block("attachments", "<attachments>\n" + "\n".join(att_lines) + "\n</attachments>"))  # 附件块

    # —— Budget：锚点永留；反向贪心填（贴近问题的 dialog/tools 先保，facts/segments 预算紧先丢）——
    dropped: dict[str, int] = {}  # 预算丢弃计数
    anchor_tokens = (len(working_block) + len(str(system or "")) + len(str(question or ""))) // 4  # 锚点估算（近似 token）
    available = max(0, used.window - used.output_reserve - used.headroom - anchor_tokens)  # 可用
    memory_cap = int(available * ratio_f)  # 记忆区上限
    keep_blocks: list[Block] = []  # 最终块
    for block in reversed(blocks):  # 从后往前（贴近问题的先保）
        if block.tokens + sum(b.tokens for b in keep_blocks) <= memory_cap:  # 放得下
            keep_blocks.append(block)  # 留
        else:  # 放不下
            dropped[block.tag] = block.tokens  # 记丢
    keep_blocks = keep_blocks[::-1]  # 还原 position 顺序

    # —— Compose ——
    system_out = (str(system or "") + (("\n\n" + working_block) if working_block else "")).strip()  # S + working（缓存前缀+锚点）
    # 稳定前缀 = S + working（hints 每轮可变，不计入缓存前缀；注释口径与需求一致）
    cache_prefix = (len(str(system or "")) + (len(working_block) + 2 if working_block else 0)) // 4  # 缓存前缀 token（不含 hints）
    hints_lines = [str(h) for h in (hints or []) if str(h).strip()]  # 护栏提示（防追问循环）
    if hints_lines:  # 有护栏
        system_out += "\n\n<hints>\n" + "\n".join(hints_lines) + "\n</hints>"  # 注入 system（锚点区）
    non_empty = [b for b in keep_blocks if b.text]  # 去空
    messages: list[dict[str, Any]] = []  # 消息
    if non_empty:  # 有记忆/动态块
        body = "\n\n".join(b.text for b in non_empty)  # 合并
        q = str(question or "")  # 问题
        messages.append({"role": "user", "content": body + (("\n\n" + q) if q else "")})  # 记忆+问题同一条 user（问题最末）
    else:  # 无块
        messages.append({"role": "user", "content": str(question or "")})  # 仅问题
    constraints_kept = _constraints_kept_fraction(  # 关键约束保留率（P5 指标）
        system_out,  # 终 system
        messages,  # 终消息
        working.get("key_facts") or {},  # key_facts 锚点
        hints_lines,  # 护栏提示
    )  # 计算
    return ContextOut(  # 返回
        system=system_out,  # system
        messages=messages,  # messages
        blocks=keep_blocks,  # 有序块
        pruned=pruned,  # 剪枝
        dropped=dropped,  # 预算丢弃
        cache_prefix_tokens=cache_prefix,  # 缓存前缀 token
        constraints_kept=constraints_kept,  # 约束保留率
    )  # 结束


def _constraints_kept_fraction(  # 关键约束保留率
    system_text: str,  # 终 system 文本
    messages: list[dict[str, Any]],  # 终消息
    key_facts: dict[str, Any],  # key_facts 锚点
    hints: list[str],  # 护栏提示
) -> float:  # 0..1
    """计算 key_facts 值与 hint 文本落在最终上下文的比例（P5 指标 6.6①）。

    约束集合 = 每个 key_facts 值的文本 + 每条 hint（非空）。全被注入 → 1.0；
    预算紧被裁掉 → 相应降低，暴露"关键约束被丢"。
    """
    pool = str(system_text or "") + " ".join(  # 终上下文全文（system+消息）
        str(m.get("content") or "") for m in messages  # 各消息
    )  # 拼接
    targets: list[str] = []  # 约束集合
    for value in (key_facts or {}).values():  # 逐关键事实值
        text = str(value).strip()  # 值文本
        if text:  # 非空
            targets.append(text)  # 记
    targets.extend(str(h).strip() for h in (hints or []) if str(h).strip())  # 护栏提示
    if not targets:  # 无约束
        return 1.0  # 空集合视为全保留
    kept = sum(1 for t in targets if t in pool)  # 落在上下文里的条数
    return round(kept / len(targets), 4)  # 比例
