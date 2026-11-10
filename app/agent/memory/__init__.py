"""记忆包：work memory / segment 摘要 / long-term store / manager 统一入口 / 召回。

**写入纪律（T1.1 起）**：任何对长期记忆/用户画像的 DB 读写都必须经
`app.agent.memory.manager`（唯一入口，负责写入判定/去重/冲突/权限）；`MemoryStore`
只允许 manager 直接实例化，其余模块不得绕过 manager 直连 longterm。
"""

from __future__ import annotations  # 延迟值注解

from app.agent.memory.longterm import (  # 长期可信知识库
    MemoryFact,  # 事实
    MemoryStore,  # 仓储
    confidence_for,  # 可信度
    conflict_stats,  # 冲突治理统计
    cosine_sim,  # 余弦
    decay_score,  # 衰减
    embed_text,  # 嵌入
    extract_eav,  # 规则抽取
)  # 导入结束
from app.agent.memory.working import (  # 工作记忆白板
    WorkingMemory,  # 形状
)  # 导入结束
from app.agent.memory.working import (
    blank as blank_working,  # 空
)
from app.agent.memory.working import (
    promote as promote_working,  # 收尾
)
from app.agent.memory.working import (
    record_step as record_step_working,  # 记步
)
from app.agent.memory.working import (
    set_goal as set_goal_working,  # 设目标
)
from app.agent.memory.working import (
    touch as touch_working,  # 合并关键事实
)
from app.agent.memory.working import (
    update as update_working,  # 局部更新
)

__all__ = [  # 导出（按字典序，含大小写）
    # 长期知识库类型（大写在前）
    "MemoryFact",  # 事实
    "MemoryStore",  # 仓储
    "WorkingMemory",  # 白板形状
    # 工作记忆助手
    "blank_working",  # 空白板
    "confidence_for",  # 可信度
    "conflict_stats",  # 冲突治理统计
    "cosine_sim",  # 余弦
    "decay_score",  # 衰减
    "embed_text",  # 嵌入
    "extract_eav",  # 规则抽取
    "promote_working",  # 收尾
    "record_step_working",  # 记步
    "set_goal_working",  # 设目标
    "touch_working",  # 合并关键事实
    "update_working",  # 局部更新
]  # 结束
