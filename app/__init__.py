"""界面层（NiceGUI）：只做三件事——**画布局 / 渲染事件 / 调 services**。

红线（技术方案 §5.1）：

- 不许 import `core` 的内部实现，一律走 `app/services/`；
- 不许出现 `langchain` / `openai` 的 import；
- 不许出现业务规则（拼提示词、算分数这类），那是 `app/core/` 的活。

**现状**（P0 已完成）：外壳（聊天 / 设置两种模式）、聊天页、设置页都在；`app/services/` 已落地
（`settings.py` 管设置与数据概览），页面只通过它拿数据。

**聊天页已接真实检索**（`app/screens/_chat_retrieval.py`）：dense 语义召回 child → 回填父块
证据，答案用**抽取式**（不接生成式 LLM）。版式、多会话与内容格式都已定下来；渲染代码只依赖
`Turn` 数据形状，换数据源时不用改。
"""
