"""聊天界面的**模拟数据源**（当前只做界面，后端数据先用模拟的）。

数据形状在 `app/state/chat_model.py`；这里只负责"造出几轮不同的内容"。

**覆盖各种情况**（按用户要求"各种模拟的"）：富 Markdown（标题/列表/表格/代码块/引用）+
引用角标、长表格、无命中、检索失败。发送消息时按顺序轮换，所以连着问几条就能把各种
显示格式都看到。
"""

from __future__ import annotations

from app.ui.state import conversations as store
from app.ui.state.chat_model import Entry, Fragment, Process, Turn

#: 示例会话的说明（**必须说清楚是模拟数据**，否则用户会以为"它已经有资料、已经能答"）
SAMPLE_NOTE = "这个会话里的答案与出处都是示例数据（后端检索还没接），用来展示各种内容格式。"

_RICH = Turn(
    process=Process(
        running='正在检索本地资料…',
        done='检索本地资料 · 命中 3 条 · 18 ms',
        details=[
            '查询词：违约金 · 计算 · 比例',
            '#1 bm25 4.82 · 设备采购合同.md · 第 5 段',
            '#2 bm25 3.17 · 付款流程说明.md · 第 1 段',
            '#3 bm25 2.44 · 验收纪要-2026Q1.md · 第 2 段',
        ],
    ),
    answer="""根据本地资料，**违约金的算法**写在《设备采购合同》里，要点有三条：

1. 计算基数是**合同总额**，不是未交付部分的金额；
2. 比例是每日**千分之三**；
3. 有封顶：累计不超过合同总额的 **5%**。

| 项目 | 约定 | 出处 |
| --- | --- | --- |
| 起算日 | 约定交货日次日 | 合同 §7.2 |
| 日比例 | 0.3% | 合同 §7.2 |
| 封顶 | 合同总额 5% | 合同 §7.2 |
| 质保金 | 10%，验收合格后 30 天退 | 付款流程说明 |

> 注意：合同原文用的是"千分之三"，不是"百分之三"——这两者差了十倍。

计算方式可以写成一行：

```python
def penalty(total: float, days: int) -> float:
    return min(total * 0.003 * days, total * 0.05)
```
""",
    fragments=[
        Fragment(
            index=1,
            source_name='设备采购合同.md',
            origin='第 5 段',
            score=4.82,
            text="""## 违约

逾期交货的，每逾期一日按合同总额的千分之三支付违约金，累计不超过合同总额的 5%。
起算日为约定交货日的次日，以双方确认的实际到货日期为准。""",
        ),
        Fragment(
            index=2,
            source_name='付款流程说明.md',
            origin='第 1 段',
            score=3.17,
            text="""付款节奏分三段：**预付款 30%**、**到货款 60%**、**质保金 10%**。
质保金在验收合格后 30 天内无息退还，逾期退还的按同期贷款利率计息。""",
        ),
        Fragment(
            index=3,
            source_name='验收纪要-2026Q1.md',
            origin='第 2 段',
            score=2.44,
            text="""本次验收共 3 个批次，其中第 2 批次延迟 6 天到货，
按合同 `§7.2` 计违约金 18 天 × 0.3% × 合同额。""",
        ),
    ],
)

_TABLE = Turn(
    process=Process(
        running='正在检索本地资料…',
        done='检索本地资料 · 命中 1 条 · 11 ms',
        details=['查询词：参数 · 对照 · 交付 · 周期', '#1 bm25 5.31 · 设备参数对照表.md · 第 1 段'],
    ),
    answer="""《设备参数对照表》里的主要参数如下：

| 型号 | 交付周期 | 质保期 | 单价（元） |
| --- | --- | --- | --- |
| A-100 | 30 天 | 12 个月 | 12,800 |
| A-200 | 45 天 | 24 个月 | 19,600 |
| B-300 | 60 天 | 24 个月 | 27,500 |

选型建议：

- 预算优先 → `A-100`；
- 长期运行（>2 年）→ `A-200`，质保期多一倍，摊到每年反而更便宜；
- 有扩展需求 → `B-300`，但交付周期最长。

---

以上都是**本地资料原文**，没有做任何推测。
""",
    fragments=[
        Fragment(
            index=1,
            source_name='设备参数对照表.md',
            origin='第 1 段',
            score=5.31,
            text="""| 型号 | 交付周期 | 质保期 | 单价（元） |
| --- | --- | --- | --- |
| A-100 | 30 天 | 12 个月 | 12,800 |
| A-200 | 45 天 | 24 个月 | 19,600 |
| B-300 | 60 天 | 24 个月 | 27,500 |""",
        ),
    ],
)

_MISS = Turn(
    process=Process(
        running='正在检索本地资料…',
        done='检索本地资料 · 命中 0 条 · 9 ms',
        details=['查询词：量子 · 计算机 · 稀释 · 制冷机', '没有片段命中这些词'],
    ),
    note='没有在本地资料里找到相关内容。换个说法试试。',
)

_ERROR = Turn(
    process=Process(
        running='正在检索本地资料…',
        done='检索失败 · 耗时 2.1 s',
        details=['错误：数据库被占用（database is locked）', '已重试 1 次'],
    ),
    note='这次检索没跑完：数据库被占用。稍后重试。',
    note_kind='error',
)

#: 按顺序轮换的场景（最后一个是报错，用来确认错误样式和"没找到"能区分开）
SCENARIOS: tuple[Turn, ...] = (_RICH, _TABLE, _MISS, _ERROR)

_cursor = 0


def pick_turn(question: str = "") -> Turn:
    """取下一轮模拟数据（**轮换**返回，所以连着问几条能看到各种显示格式）。

    参数：
        question: 用户输入（模拟数据源用不到它，真实数据源会用）。
    返回：:class:`~app.ui.state.chat_model.Turn`。
    """
    _ = question  # 模拟数据源用不到输入，真实数据源才会用
    global _cursor
    turn = SCENARIOS[_cursor % len(SCENARIOS)]
    _cursor += 1
    return turn


def ensure_seeded() -> None:
    """第一次进来时建一个**示例会话**（里面有完整的一轮，并标注是模拟数据）。

    为什么要有它：多会话之后"新对话"就该是空的；如果一进来什么都没有，
    界面能显示成什么样就没人看得见。所以第一个会话直接给一轮完整示例。
    """
    global _cursor
    if store.conversations():
        return
    store.new(title='示例对话（模拟数据）', note=SAMPLE_NOTE)
    store.append(Entry(question='违约金怎么算？', turn=_RICH))
    _cursor = 1  # 示例用掉了第一个场景，之后发送从下一个开始（免得第一问就重复它）
