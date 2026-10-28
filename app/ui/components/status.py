"""状态文案中心（界面层唯一出处）。

**为什么集中**：知识库、检索追踪、任务三个页面各自写过一份"状态 → 中文"，改一处忘一处；
未知状态还容易被吞成空串。这里按**领域**分开（文档状态 ≠ 运行状态），只集中"文案"，
颜色/胶囊样式各页按自己的视觉语言决定（知识库用 Quasar chip、追踪用 hp-status）。
"""

from __future__ import annotations

#: 资料（documents.status）→ 中文。
DOC_STATUS_LABELS: dict[str, str] = {
    "pending": "待处理",  # 已登记、还没解析
    "indexing": "处理中",  # 解析完成、向量还在建
    "indexed": "就绪",  # 可检索
    "ready": "就绪",  # 早期文案，保留兼容
    "failed": "失败",  # 解析/索引失败
    "superseded": "已退场",  # 被新版本替代（保留可回滚）
}

#: 追踪运行（trace_runs.status）→ 中文。
RUN_STATUS_LABELS: dict[str, str] = {
    "ok": "成功",  # 正常结束
    "error": "失败",  # 异常结束
    "waiting": "等待",  # 等用户补参数
    "running": "运行中",  # 还在跑
    "cancelled": "已取消",  # 用户主动停止
}


def doc_status_label(status: str) -> str:
    """资料状态的文案（未知状态原样返回，别吞信息）。"""
    return DOC_STATUS_LABELS.get(status, status)


def run_status_label(status: str) -> str:
    """追踪运行状态的文案（未知状态原样返回）。"""
    return RUN_STATUS_LABELS.get(status, status)
