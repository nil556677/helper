"""记忆包：work memory / segment 摘要 / long-term store / 召回（异步后台执行）。"""

from __future__ import annotations  # 延迟值注解

from app.agent.memory.working import (  # 工作记忆白板
    WorkingMemory,  # 形状
    blank as blank_working,  # 空
    promote as promote_working,  # 收尾
    record_step as record_step_working,  # 记步
    set_goal as set_goal_working,  # 设目标
    touch as touch_working,  # 合并关键事实
    update as update_working,  # 局部更新
)  # 导入结束
from app.agent.memory.longterm import (  # 长期可信知识库
    MemoryFact,  # 事实
    MemoryStore,  # 仓储
    confidence_for,  # 可信度
    cosine_sim,  # 余弦
    decay_score,  # 衰减
    distill_and_write,  # 异步写回入口
    embed_text,  # 嵌入
    extract_eav,  # 规则抽取
)  # 导入结束

__all__ = [  # 导出
    # 工作记忆
    "WorkingMemory",  # 白板形状
    "blank_working",  # 空白板
    "promote_working",  # 收尾
    "record_step_working",  # 记步
    "set_goal_working",  # 设目标
    "touch_working",  # 合并关键事实
    "update_working",  # 局部更新
    # 长期知识库
    "MemoryFact",  # 事实
    "MemoryStore",  # 仓储
    "confidence_for",  # 可信度
    "cosine_sim",  # 余弦
    "decay_score",  # 衰减
    "distill_and_write",  # 异步写回入口
    "embed_text",  # 嵌入
    "extract_eav",  # 规则抽取
]  # 结束