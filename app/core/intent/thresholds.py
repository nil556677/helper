"""L2 仲裁阈值（**代码常量**，不是用户配置；由标定脚本产出）。

与 `app/rag/plan.py` 同口径：实测标定、改动必须带评测数据。
"""

from __future__ import annotations

#: 每个去向的 top1 余弦阈值（初值，待 `scripts/calibrate_intent.py` 标定）。
THRESHOLDS: dict[str, float] = {
    "chitchat": 0.62,
    "search_kb": 0.55,
    "query_order": 0.70,
    "weather": 0.65,
    "human_handoff": 0.60,
}

#: top1 - top2 的最小差；低于它视为歧义，不硬分。
MARGIN = 0.08
