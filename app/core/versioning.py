"""pipeline 版本号：**换了任何一步的实现，就要能识别出旧数据**。

三个版本互相独立（改切分不必重跑解析；换嵌入模型不必重解析）：

- ``PARSER_VERSION``：解析器（格式 → IR）的实现版本；
- ``CHUNK_POLICY_VERSION``：切分策略/算法版本（本次重写切分，从 1 提到 2）；
- ``EMBEDDING_VERSION``：嵌入模型/预处理版本。

它们会写进 ``documents`` 行（v14）与任务幂等键，**不参与** ``chunk_id``（位置锚点）。
"""

from __future__ import annotations

PARSER_VERSION = 1
CHUNK_POLICY_VERSION = 4
EMBEDDING_VERSION = 2
