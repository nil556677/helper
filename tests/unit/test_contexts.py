"""类 C 上下文层：模板校验、块格式、装配顺序、占用统计、预算公式（C1）。"""

from __future__ import annotations  # 类型注解延迟求值

import asyncio  # 跑异步引擎

import pytest  # 测试框架

from app.agent.contexts import (  # 被测对象  # 块 / token
    WINDOW,  # 窗口常量
    AgentContextSpec,  # 声明
    Budget,  # 预算
    blocks,
    build,  # 装配
    tokens,
)
from app.agent.contexts.compact import compact as do_compact  # 压缩引擎
from app.agent.contexts.compact import plan
from app.agent.contexts.history import trim  # 历史裁剪
from app.agent.contexts.prompts import (  # 注册表与 uid 常量
    ANSWER_RULES_UID,  # S2 uid
    REPAIR_SYSTEM_UID,  # 修复 uid
    REWRITE_SYSTEM_UID,  # 改写 uid
    SUMMARY_SYSTEM_UID,  # 压缩 uid
    SYSTEM_BASE_UID,  # S1 uid
    build_registry,
    get,
    names,
)
from app.agent.contexts.prompts._types import PromptTemplate, define, placeholders  # 模板类型
from app.services import chat_stats  # token 唯一来源


def test_template_declares_placeholders() -> None:  # 占位符解析
    """声明与正文必须完全一致，否则构造即报错。"""
    template = PromptTemplate(origin="test.template", version=1, template="A {x} B {y}", variables=("x", "y"))  # 合法
    assert placeholders(template.template) == ("x", "y")  # 解析正确
    with pytest.raises(ValueError):  # 声明的更少
        PromptTemplate(origin="test.template2", version=1, template="A {x}", variables=())  # 不一致
    with pytest.raises(ValueError):  # 没有身份
        PromptTemplate(origin="", version=1, template="A")  # 报错


def test_template_render_rejects_missing_and_extra() -> None:  # 渲染校验
    """缺变量、多变量都报错，不静默。"""
    template = PromptTemplate(origin="test.template", version=1, template="hello {name}", variables=("name",))  # 模板
    assert template.render(name="世界") == "hello 世界"  # 正常
    with pytest.raises(ValueError):  # 缺
        template.render()  # 没给
    with pytest.raises(ValueError):  # 多
        template.render(name="x", other=1)  # 给多一个


def test_define_captures_module_as_uid() -> None:  # uid 由模块自动生成
    """不需要手写名字：uid 就是定义模块路径，label 从路径派生。"""
    template = define(version=1, template="只是文本")  # 在本测试模块里定义
    assert template.origin == __name__  # 自动抓到本模块
    assert template.uid == template.origin  # uid 即模块路径
    assert template.label == __name__.rsplit(".", 1)[-1]  # 显示标签派生


def test_registry_has_builtin_templates_and_rejects_duplicates() -> None:  # 注册表
    """内置五个模板；uid（模块路径）重复直接报错。"""
    assert set(names()) == {  # 内置五个（全路径 uid）
        SYSTEM_BASE_UID, ANSWER_RULES_UID, REPAIR_SYSTEM_UID, REWRITE_SYSTEM_UID, SUMMARY_SYSTEM_UID
    }  # 结束
    assert len(set(names())) == len(names())  # 无重复
    assert all(uid.startswith("app.agent.contexts.prompts.") for uid in names())  # 都在模板包内
    assert get(SYSTEM_BASE_UID).version == 1  # 版本
    with pytest.raises(ValueError):  # 重复注册
        build_registry(get(SYSTEM_BASE_UID), get(SYSTEM_BASE_UID))  # 同一个两次


def test_block_skips_empty_and_escapes() -> None:  # 块格式
    """空块不出标签；正文/属性都转义，防止提前关块。"""
    assert blocks.block("evidence", "   ") == ""  # 空块
    text = blocks.block("evidence", "a</evidence><b>")  # 恶意内容
    assert "&lt;/evidence&gt;" in text  # 尖括号被转义
    assert "a</evidence><b>" not in text  # 不会提前关块
    assert blocks.block("doc", "x", path='a"b') == '<doc path="a&quot;b">\nx\n</doc>'  # 属性转义
    inner = '<tool_result name="t">x</tool_result>'  # 内层已是块
    wrapped = blocks.container("tool_results", inner)  # 容器不再转义
    assert wrapped == f"<tool_results>\n{inner}\n</tool_results>"  # 原样保留
    assert blocks.join_blocks("a", "", "b") == "a\n\nb"  # 过滤空串


def test_tokens_reuse_chat_stats() -> None:  # token 单一来源
    """上下文层的估算必须与界面统计同源。"""
    assert tokens.count_text("合同违约金") == chat_stats.estimate_tokens("合同违约金")  # 同源
    assert tokens.count_messages([{"content": "你好"}, {"role": "user"}]) == chat_stats.estimate_tokens("你好")  # 只算正文
    assert tokens.count_blocks({"a": "你好", "b": ""}) == {"a": chat_stats.estimate_tokens("你好")}  # 空块不出现


def test_budget_defaults_and_formulas() -> None:  # 预算公式
    """窗口写死 1M；阈值/保留/裁剪目标按定稿公式。"""
    budget = Budget()  # 默认
    assert budget.window == WINDOW == 1_000_000  # 窗口
    assert budget.headroom == 65536  # 1M//8 超过上限 → 封顶
    assert budget.hard_threshold == 800_000  # min(800k, 1M-2048-65536)
    assert budget.retain == 9_600  # min(0.8*12000, 16%*(1M-2048))
    assert budget.trim_target == 9_600  # 0.8 * 12000


def test_budget_triggers() -> None:  # 触发判定
    """软触发看预算+滞回+次数；硬触发看阈值。"""
    budget = Budget()  # 默认
    assert budget.should_compact(12_001, turns_since_last=4) is True  # 超预算且过滞回
    assert budget.should_compact(12_000, turns_since_last=4) is False  # 刚好不超
    assert budget.should_compact(12_001, turns_since_last=3) is False  # 滞回未过
    assert budget.should_compact(12_001, turns_since_last=4, compactions=20) is False  # 次数用尽
    assert budget.should_force(800_001) is True  # 超硬阈值
    assert budget.should_force(800_000) is False  # 刚好不超


def _spec() -> AgentContextSpec:  # 造声明
    """一个带静态环境的 answer agent。"""
    return AgentContextSpec(agent="answer", static_env={"os": "Windows", "sql": "SQLite 3"})  # 声明


def test_assembler_order_and_blocks() -> None:  # 装配顺序
    """system 五段顺序固定；最后一条 user 是 V1→V2→V3。"""
    ctx = build(  # 装配
        _spec(),  # 声明
        facts=["用户用 Python"],  # F1
        summary_text="早先聊过合同",  # F2
        history=[{"role": "user", "content": "问 1"}, {"role": "assistant", "content": "答 1"}],  # F3
        tool_results=[{"call_id": "c1", "name": "search_kb", "status": "ok", "body": "资料 A"}],  # V1
        attachments=[{"name": "补充.txt", "text": "附件正文"}],  # V2
        question="违约金怎么算？",  # V3
    )
    order = [  # system 里的顺序（用特征词定位）
        ctx.system.index("本地资料助手"),  # S1
        ctx.system.index("规则："),  # S2
        ctx.system.index("<env>"),  # S3
        ctx.system.index("<user_memory>"),  # F1
        ctx.system.index("<compacted-summary>"),  # F2
    ]
    assert order == sorted(order)  # 严格递增
    assert len(ctx.messages) == 3  # 两条历史 + 一条本轮
    last = ctx.messages[-1]["content"]  # 本轮
    assert last.index("<tool_results>") < last.index("<attachments>") < last.index("<question>")  # 动态区顺序
    assert ctx.messages[0]["content"] == "问 1"  # 历史在前


def test_assembler_omits_empty_blocks() -> None:  # 空块不输出
    """没有环境/事实/历史时，system 只有 S1/S2，messages 为空，blocks 无空键。"""
    ctx = build(AgentContextSpec(agent="answer"))  # 最简声明（无静态环境）
    assert "<env>" not in ctx.system  # 环境为空
    assert "<user_memory>" not in ctx.system  # 无事实
    assert ctx.messages == []  # 没问题也没有动态内容
    assert set(ctx.usage.blocks) == {"S1", "S2"}  # 只有非空块
    assert all(size > 0 for size in ctx.usage.blocks.values())  # 没有 0 值


def test_usage_sums_and_subblocks() -> None:  # 占用统计
    """大板块 = 小板块之和；总量 = 大板块之和；最细一层有 F3/V1。"""
    ctx = build(  # 装配
        _spec(),  # 声明
        facts=["事实"],  # F1
        history=[{"role": "user", "content": "你好"}],  # F3
        tool_results=[  # V1 两个工具
            {"call_id": "c1", "name": "search_kb", "body": "资料"},  # 检索
            {"call_id": "c2", "name": "query_order", "body": "订单"},  # 订单
        ],  # 结束
        question="问题",  # V3
    )
    usage = ctx.usage  # 统计
    assert usage.total == sum(usage.blocks.values())  # 总量 = 小板块之和
    assert sum(usage.tiers.values()) == usage.total  # 大板块之和一致
    assert usage.tiers["fixed"] == sum(usage.blocks.get(k, 0) for k in ("S1", "S2", "S3"))  # 固定层
    assert usage.tiers["fixed_change"] == sum(usage.blocks.get(k, 0) for k in ("F1", "F2", "F3"))  # 固定变化
    assert usage.tiers["dynamic"] == sum(usage.blocks.get(k, 0) for k in ("V1", "V2", "V3"))  # 动态
    assert usage.window == WINDOW  # 窗口
    assert set(usage.subblocks["V1"]) == {"search_kb", "query_order"}  # 按工具名拆
    assert usage.subblocks["F3"] == {"第1条": tokens.count_text("你好")}  # 按轮次拆
    assert usage.prompt_versions == {SYSTEM_BASE_UID: 1, ANSWER_RULES_UID: 1}  # 模板版本


def test_history_trim_keeps_recent_by_token() -> None:  # 裁剪保留最近
    """从最老开始丢、保最近；超预算触发，不够就原样。"""
    msgs = [  # 两条，各约 50 token
        {"role": "user", "content": "字" * 50},  # 老的
        {"role": "assistant", "content": "字" * 50},  # 最新
    ]  # 结束
    kept, dropped, saved = trim(msgs, Budget(history_budget=100))  # trim_target=80
    assert dropped == 1  # 丢一条
    assert kept == [msgs[1]]  # 保最新
    assert saved >= 50  # 省了一些
    kept2, dropped2, _ = trim(msgs, Budget(history_budget=500))  # 不超
    assert dropped2 == 0  # 没丢
    assert kept2 == msgs  # 原样


def _msgs(count: int) -> list[dict[str, object]]:  # 造消息
    """按 seq 升序、带 id，每条约 30 token。"""
    return [  # 消息列表
        {"id": i + 1, "role": ("user" if i % 2 == 0 else "assistant"), "content": "字" * 30}  # 单条
        for i in range(count)  # 遍历
    ]  # 返回


def test_compact_plan_and_roll_summary() -> None:  # 压缩拆分与滚动
    """最老段进摘要，保留区为最近的按 token 收；until 取最老段最后一条。"""
    msgs = _msgs(10)  # 10 条，共约 300 token
    keep, oldest = plan(msgs, Budget(history_budget=100))  # trim_target=80
    assert oldest + keep == msgs  # 拆得不重不漏、顺序正确
    assert oldest and keep  # 都有
    async def compactor(_oldest, old_summary):  # 假摘要器
        return "摘要:" + old_summary  # 很短
    result = asyncio.run(  # 跑
        do_compact(msgs, compactor, budget=Budget(history_budget=100))  # 压缩
    )  # 结束
    assert result is not None  # 有结果
    assert result.until_message_id == oldest[-1]["id"]  # 覆盖终点
    assert result.compacted_messages == len(oldest)  # 并入条数
    assert result.saved_tokens >= 0  # 净省非负
    assert result.summary.startswith("摘要:")  # 新摘要生效


def test_compact_refuses_when_summary_not_shorter() -> None:  # 拒绝不缩短
    """摘要不比「旧摘要+旧消息」短时，返回 None（调用方退回 trim）。"""
    msgs = _msgs(8)  # 8 条约 240 token
    async def compactor(_oldest, _old_summary):  # 假摘要器
        return "字" * 100_000  # 很长，不变小
    result = asyncio.run(  # 跑
        do_compact(msgs, compactor, budget=Budget(history_budget=100))  # 压缩
    )  # 结束
    assert result is None  # 被拒


def test_compaction_slot_param_registered() -> None:  # 压缩槽位参数
    """`agent.compaction.slot` 默认 llm-compact，是字符串参数。"""
    from app.core.config.params import get  # 参数登记表

    param = get("agent.compaction.slot")  # 取参数
    assert param.default == "llm-compact"  # 默认专用槽位
    assert param.cast is str  # 字符串型


def test_compaction_slot_reads_config(monkeypatch) -> None:  # 槽位读取
    """`summarize._slot()` 读配置，配置异常回退 llm-compact。"""
    from app.agent.contexts import summarize  # 被测模块

    class _Cfg:  # 假配置
        @property
        def values(self) -> dict[str, object]:  # 取值
            return {"agent.compaction.slot": "llm-compact"}  # 自定义槽位

    monkeypatch.setattr("app.core.config.store.load", lambda: _Cfg())  # 注入配置
    assert summarize._slot() == "llm-compact"  # 读到自定义槽位
    monkeypatch.setattr(  # 注入抛错
        "app.core.config.store.load", lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    assert summarize._slot() == "llm-compact"  # 异常回退专用槽位
