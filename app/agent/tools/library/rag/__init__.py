"""知识库检索域工具汇总。"""

from __future__ import annotations  # 延迟求值注解

from app.agent.tools.library.rag.search_knowledge_base import (  # 检索工具
    collect_evidence,  # 取证据
    search_knowledge_base,  # 检索壳（mode 区分强度）
)  # 导入结束

__all__ = ["collect_evidence", "search_knowledge_base"]  # 导出