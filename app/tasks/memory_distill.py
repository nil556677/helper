"""记忆蒸馏任务：从会话压缩段抽事实写回长期库（规则抽取 + P1 统一写口）。

由 finalize 成功后排程（T3.3 接线）；本任务调用 `manager.distill_conversation`，
它内部走 `record_facts`（去重 + PII 脱敏）。T3.3 在此追加 LLM 结构化抽取。
"""

from __future__ import annotations  # 延迟值注解

from pydantic import BaseModel  # 参数模型

from app.core.jobs.model import JobContext  # 任务上下文
from app.core.jobs.task import Task, TaskResult  # 任务基类


class MemoryDistill(Task):
    title = "记忆蒸馏（事实写回）"
    lane = "io"
    idempotent = True
    max_attempts = 2
    lease_s = 120.0

    class Params(BaseModel):
        conversation_id: str = ""  # 目标会话

    def run(self, ctx: JobContext, params: MemoryDistill.Params) -> TaskResult:
        from app.agent.memory import manager  # 记忆统一入口

        n = manager.distill_conversation(str(params.conversation_id or ""))  # 蒸馏写回
        ctx.report(1, 1, "完成")
        return TaskResult(note=f"记忆蒸馏写回 {n} 条")
