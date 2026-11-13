# Changelog

本文件记录面向用户的改动轨迹。内部方案文档在 `docs/`、不进版本控制，因此公开可查的变更历史只有这一份 ——
**版本发布时必须在 `CHANGELOG.md` 补一条**，发布步骤见 [README.md 的「发布流程」](#发布流程)。

## 未发布

- **多轮指代消解 / 焦点管理**：实现《指代消解与焦点管理补充方案》——在 `preprocess_node` 内合入
  "实体抽取 → 话题切换(五信号) → 指代消解(实体/序数/链回溯) → 焦点栈维护"，跨轮随 State(checkpointer)
  持久化，产出 `resolved_query` + `anchor_entities` 供路由/执行；模型复用默认大模型、只在 ambiguous
  一趟附带消解。
  - 模块：`contracts/focus.py`、`preprocess/focus/{extract,topic,stack,resolve,store}.py`；`focus_events`
    审计表同步写 SQLite（只追加）+ `correct_binding`（纠正回滚走 `update_state`）。
  - **实现修订**：焦点合进 `preprocess`（而非独立图节点）——实测在热路由链插新节点会让 LangGraph
    把 `rule_route`/`dispatch_fixed` 调度进同一 superstep 抛 `InvalidUpdateError`。
  - 单测：`test_focus.py`（抽取硬约束/栈/话题/消解/端到端"第二个呢"绑定67890）、`test_focus_store.py`。
  - 全部单测绿。

- **执行分层（简单 ReAct / 复杂 Plan+Execute）**：实现《执行分层方案》——`triage` 在"要走 agent 的那批"里
  加一个 `complexity`（simple/complex）判定，简单问题走原单 ReAct，复杂/多步问题走**计划+执行**子图
  （规划 → 分步执行 → 重规划，见 `app/agent/fallback/`）。
  - 路由/编排：`nodes/plan_execute` 节点 + `routing/edges.after_triage` 分流 + 节点级事件（`plan` STAGE）；
  - **每计划独立预算** `fallback/budget.PlanBudget`（步/重规划/token/工具/超时/cost，逐计划独立、不共享）；
  - **完成判定/推进可见**：`contracts/plan_status` 的 `step.status` / `plan_status` / `completion`
    （区分"真正完成"与"止损"，不把 is_done 一律当成功）；
  - **无用重试去重**：同一步尝试超限即停（`max_attempts_per_step`）+ 无进展循环检测；
  - **敏感步前置审批**：不可逆/涉敏感步骤执行前被拦（`make_sensitive_approver`）；
  - **可恢复内层子图**：`fallback/plan_subgraph` 提供内层可 checkpoint 子图 + `resume_state`
    （崩溃后"从下一步续跑、不重跑已完成步"，挂 checkpointer 由真 worker 决定，遵循 `checkpoint.py` 说明）；
  - **灰度回退**：`HELPER_PLAN_EXECUTE=off` 全部回现状单 ReAct；计划决策进 `escalate_reason`（`complex_plan`）便于离线归因。
  - **确认→查询→留痕（用户侧，非后台审批）**：订单/敏感查询在真正执行前先落一条 `approval_requests`（pending），
    弹框**告知**要查什么 + 确认/拒绝；确认 → 记录 approved → 才查订单 + 写 `approval_audit` 的 executed 留痕；
    拒绝/超时 → rejected/expired 不执行。判定由工具元数据（`approval="manual"`）+ 记录状态驱动，不靠文字关键词。
  - **执行/确认可恢复**：确认走 LangGraph `interrupt`/`Command(resume)`（生产父图挂 `AsyncSqliteSaver`）；
    `decide_and_execute` 对"已 approve 但未 executed"（确认后崩在执行前）恢复时幂等补跑；
    `approval_service.catchup_approved()` 在 worker 启动时补齐这类残留记录；
    计划路径确认后通过内联 `interrupt` 处理器**继续跑剩余步**（不再停在确认那一步）。
  - 全部单测绿（含 `test_consent_recovery`）。
  - 全部单测绿（含 `test_complexity / test_budget / test_plan_status / test_dedupe / test_feature_plan /
    test_plan_approval / test_plan_execute / test_plan_subgraph`）。

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