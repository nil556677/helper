"""引擎层：本项目的全部业务逻辑都在这里。

**项目定位**：本地桌面 AI 助手 —— 资料在本机、检索在本机，只把命中的资料片段 + 用户问题
交给云端大模型生成回答。**第一个功能是"聊天"**（本地检索 + 大模型作答），
后来长出了"知识库管理 / 任务系统 / 追踪"等能力。

与 `app/`（NiceGUI 界面层）的边界是硬约束：

- **core 不 import 界面**（`nicegui` / `app.*`），必须能脱离界面单测；
- **界面只调 `app/services/`**，不直接碰 core 内部细节（scripts/rule_check.py 的 R6 会拦）。

依赖方向：**UI → services → core**（与 agent/rag/integrations 平行，都站在 core 之上）。
core 内部只向上提供原子能力，不反向依赖上层。

包内分区（均已落地）：

| 包 | 职责 |
| --- | --- |
| `paths` | 安装目录 / data / logs / models 的唯一来源 |
| `logging` | 日志系统：轮转、脱敏、未捕获异常、导出示意图 |
| `config` | 引导配置 + 数据库配置项的读写、校验、索引指纹/过期判定 |
| `db` | SQLite 连接、表结构（schema_v1 权威）、迁移 |
| `security` | 密钥加密（Windows DPAPI；key 只以密文入库） |
| `llm` | 模型槽位与凭证管理（设置页"模型"用） |
| `models` | ONNX 模型单例（向量编码 / 精排共用） |
| `embed` / `rerank` / `sparse` / `retrieval` | 本地向量编码 / 精排 / BM25 稀疏编码 / 词条与分词 |
| `parse` / `chunk` | 文件解析（几种格式）与块→parent/child 切分树 |
| `trace` / `jobs` / `proc` | 运行追踪落库 / 后台任务系统 / 子进程管理 |
| `intent` | （已随旧意图路由移除；现决策走 `app/agent/contracts.output.RouteDecision`） |

上层消费：`app/integrations/` 是 core 的适配器（embeddings/rerank/sparse/qdrant/llm）；
`app/rag/` 是检索编排；`app/agent/` 是对话/多 Agent 管线（契约层 + 状态机 + 驱动 + 工具层）。
"""

# 本包为空实现载体：真正的实现分散在子包，`__init__` 不再聚合导出任何符号，
# 避免"顺手从 core 顶层 import 细节"破坏分层。
