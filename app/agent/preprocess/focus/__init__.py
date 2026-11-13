"""焦点管理：多轮指代消解 + 焦点栈维护（实体抽取/话题切换/消解/栈更新）。"""

from app.agent.preprocess.focus.extract import extract_entities  # 抽取
from app.agent.preprocess.focus.resolve import resolve_reference  # 消解
from app.agent.preprocess.focus.stack import new_stack, update_focus_stack  # 栈
from app.agent.preprocess.focus.topic import detect_topic_switch, topic_scores  # 话题

__all__ = ["detect_topic_switch", "extract_entities", "new_stack", "resolve_reference", "topic_scores", "update_focus_stack"]  # 导出
