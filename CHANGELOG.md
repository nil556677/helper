# Changelog

本文件记录面向用户的改动轨迹。内部方案文档在 `docs/`、不进版本控制，因此公开可查的变更历史只有这一份 ——
**版本发布时必须在 `CHANGELOG.md` 补一条**，发布步骤见 [README.md 的「发布流程」](#发布流程)。

## 0.2.0 — 2026-10-22

- **净室重写**：编排层迁到 LangChain 1.4.7 + LangGraph 1.2.16（`StateGraph`、`interrupt`、
  SQLite checkpointer）；删除旧 agent route/pending、旧 RAG query/assemble、旧 vector/store。
- **数据库 v1**：全新 schema（documents + chunks parent/child、conversations/messages、trace_*）；
  旧 v20 库通过 `scripts/reset_workspace.py` 一次性重置，仅保留 `llm_slots`。
- **向量库**：Qdrant 单集合 `kb_chunks`（dense+sparse）；父块回 SQLite；删除
  `rag_parents`、alias、v2 集合；`models/` 三个 ONNX 文件继续使用。
- **检索/回答**：dense+sparse → RRF → 精排 → 父块证据 → 生成/引用校验；未配置 LLM 时回退原文。
- **检索追踪**：trace 落库（runs/spans/hits/events）+ 设置页「检索追踪」后台。
- **知识库**：查询/暂存/删除/重建/导入任务全部切到 v1 + 新 ingest pipeline。
- **UI**：真实引擎 DOM 检查 59/59 通过；单测全绿。

## 0.1.0 — 2026-10-15

- 首个可运行版本：NiceGUI + QtWebEngine 桌面窗口，双击即起、不用浏览器（聊天 / 设置两套模式，窗口大小与位置会记住）
- 设置面板（通用 / 模型配置 / 知识库 / 任务 四个分区），版本号在「关于」里显示
- 知识库导入：`.txt .md .pdf .docx .xlsx .pptx .json .html .csv .tsv .log` 等格式本地解析 → 切分 → 建索引；
  同内容重复导入自动跳过；失败的会列出并写清原因，可逐条重建、删除
- 任务系统：全项目后台任务集中查看，导入资料排队进行、可暂停/恢复、可取消/重试、失败原因看完整时间线；
  关窗口时正在跑的任务会记录、下次打开接着做
- 本地模型：嵌入与精排用 ONNX Runtime（CPU）跑，非首次使用时才加载、省常驻内存；模型文件由用户放进 `models/`
- 单实例守卫：同一台机器只允许一个实例在跑
- 自检命令 `python -m app --selfcheck`：一条命令给可复制的环境报告（无需 Qt 也能跑）
- 依赖锁定：`requirements.lock.txt` + `scripts/freeze_lock.py --check`，超 `requirements.txt` 的锁，复现以 lock 为准
- 生产级日志：主日志按天命名、只留最近 7 天，原生（Qt/Chromium）输出单独成文件，`log_level` 可调
- 配置文件 `config.json`（监听地址等"打开数据库之前就要知道"的设置），密钥用 Windows DPAPI 加密后存本地库，界面只显示尾号
- **启动 / 退出加固**：后台服务（Qdrant / worker）启动失败时落文件并弹窗；退出时**先等所有子进程真正退出，
  确认不了的（still-running）会弹窗提醒**，不留下孤儿后台进程（多进程模式下"先关子进程、再关主进程"）

> 说明：本版聊天页仍使用预置的模拟数据（`app/ui/screens/_chat_mock.py`）——版式、多会话与内容格式都是真的，
> 但检索与"接大模型出真实答案"还在落地中（见 [README.md 的「当前状态」](#当前状态)）。