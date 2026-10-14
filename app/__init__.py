"""界面层（NiceGUI）：只做三件事——**画布局 / 渲染事件 / 调 services**。

红线（技术方案 §5.1）：

- 不许 import `core` 的内部实现，一律走 `app/services/`；
- 不许出现 `langchain` / `openai` 的 import；
- 不许出现业务规则（拼提示词、算分数这类），那是 `app/core/` 的活。

**现状**（P0 已完成）：外壳（聊天 / 设置两种模式）、聊天页、设置页都在；`app/services/` 已落地
（`settings.py` 管设置与数据概览、`preferences.py` 管窗口几何），页面只通过它拿数据。

**聊天页目前是模拟数据**（`app/screens/_chat_mock.py`）：版式、多会话与各种内容格式都已经定下来，
但**答案与出处是预置的**；接上真实链路后换成真实数据，渲染代码不用动。
"""
