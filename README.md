# helper

本地桌面 AI 助手：**资料在本机、检索在本机，只把命中的资料片段 + 你的问题交给云端大模型生成回答。**
第一个功能是**聊天**（本地检索 + 大模型作答），后面还会长出别的功能。

- **界面**：NiceGUI 写界面（纯 Python），装在 **PySide6 QtWebEngine** 窗口里 —— 双击打开，不用浏览器
  （不是 pywebview：它要经 pythonnet 加载 .NET，本机实测 CLR 加载失败，见技术方案 §16 v5）
- **数据**：全部落在本机 `data/` 下（SQLite 库 + 资料副本 + 以后的向量索引），备份 = 复制这个目录
- **模型调用**：自己写的**最小** OpenAI 兼容客户端（标准库 `urllib`，不引 SDK：这里只需要
  "发请求 + 按 SSE 读增量 + 把失败原因分好类"）；检索用 SQLite FTS5
- **资料解析**：自己写（`pypdf` / `python-docx` / `openpyxl` / `python-pptx` + 标准库），
  不引 langchain / unstructured：那套框架会带进来几十个依赖和一堆用不上的抽象
- **模型密钥**：用 Windows DPAPI 加密后存本地数据库，界面上只显示尾号

## 当前状态

**P0 骨架已完成**：窗口能起（`logs/helper.log` 里能看到 `界面已渲染`）、SQLite 建好 schema v1
（9 张表 + FTS5 索引 + 三个同步触发器）、左栏两套模式（聊天 / 设置）与设置页都在、
窗口的大小与位置会记住（存 SQLite 的 `settings` 表）。

**知识库已经能用**（设置 → 知识库）：`选择文件…` 导入资料，本地解析 → 切分 → 建索引，
列表里能看到片段数与状态（**失败的也会列出来并写清原因**），能逐条重建、删除。
支持 `.txt .md .markdown .log .csv .tsv .json .html .htm .pdf .docx .xlsx .xlsm .pptx`
（老式 `.doc/.xls/.ppt` 会提示"另存为新格式"）；同一份内容重复导入会自动跳过。

**任务系统已经能用**（设置 → 任务）：全项目的后台任务都从这儿看——正在跑、排队、最近完成/失败。
导入资料现在是**排队进行**的（上传先落 `data/inbox/`，再交给后台任务解析入库），
关窗口时正在跑的会记下来、下次打开接着做。队列可以暂停/恢复，任务可以取消、失败可以重试。
实现见技术方案 §13（`core/jobs/`：持久化任务表 + 调度线程 + 两条泳道 + 租约恢复 + 界面延迟让路）。

**还没做的**：**本地检索与接大模型出答案**（切分与检索策略等界面定稿后再谈）、
扫描件/图片的 OCR（会明确提示"可能是扫描件"）、向量化（`embed` 任务，等嵌入模型方案）。
所以**聊天页现在用的还是模拟数据**（`app/screens/_chat_mock.py`）：版式、多会话与各种内容格式
都是真的，答案与出处是预置的——界面上也明确标注了这一点。进度按《技术方案》的里程碑推进。

## 文档

**《技术方案》是本机文档，不进版本控制**。它包含：项目定位、技术栈、
分层与红线、目录定稿、数据模型、检索与模型、日志与配置约束、里程碑。

方案与代码冲突时以方案为准；要改方案就先改那份文档，并记进它的变更记录。

## 开发 / 测试怎么跑

**一律在仓库根 `D:\python_project\helper` 下执行**：`app/` 与 `core/` 靠"仓库根在 `sys.path` 里"
才能 import（`pytest.ini` 里也是这么配的），换个目录就会 import 失败。

### 启动应用

```
.venv\Scripts\python.exe -m app
```

- 弹出一个 QtWebEngine 窗口；日志在 `logs/helper.log`，看到 `界面已渲染（根节点到位）` 就是好了
- **正常关窗口**才保存窗口大小与位置（存在 `data/helper.db` 的 `settings` 表里）；强杀不保存
- 起不来时先看 `logs/startup-error.log`：启动前提不满足（目录建不出来 / 库损坏 / 迁移缺脚本）
  都会写在那里，同时弹一个提示框
- 数据全在 `data/`；想要一份干净数据：设环境变量 `HELPER_DATA_DIR` 指到别处再启动
- 界面改完不用重启服务：**改的是 Python 代码就重启应用**（NiceGUI 的热重载在桌面壳里是关掉的）

### 跑测试

```
.venv\Scripts\python.exe -m pytest                # 全部（单测 + 界面测试）
.venv\Scripts\python.exe -m pytest tests/unit -q  # 只跑单测：快，不碰界面
.venv\Scripts\python.exe -m pytest tests/ui -q    # 只跑界面测试（进程内模拟用户，不需要浏览器）
.venv\Scripts\python.exe -m pytest tests/ui/test_shell.py::test_first_question_becomes_the_title -q
```

- 测试的临时目录与数据都在 `.tmp/` 下，**不会碰真实的 `data/`**（界面测试用 `.tmp/ui-test-data`）
- 唯一例外：测试进程的日志会追加进真实的 `logs/helper.log`（日志 sink 在进程内只建一次）

### 想验"界面在真实浏览器引擎里渲染成什么样"

用户模拟看不到 markdown-it 的产物，所以要靠 [scripts/check_ui.py](scripts/check_ui.py)
（QtWebEngine 真渲染后取 HTML 断言）：

```
.venv\Scripts\python.exe -u scripts\check_ui.py
```

它打印 20 多项断言：表格是否真渲染成 `<table>`、代码块容器、引用角标、过程行可收起、
会话列表、界面里还有没有"问答"这个词……**退出码全过为 0**，可以进 CI。
这一层抓到过真问题：代码块被浏览器内置 HTML 消毒丢掉 `<pre>`、会话列表从不画初始状态。

### 受限沙箱下怎么办

**测试现在不用管了**：`pytest`（含 `tests/ui` 界面测试）能在受限模式下直接跑，不用授权。
原本会失败，是因为 NiceGUI 启动时建一个**我们用不到的** `ProcessPoolExecutor`（给 `run.cpu_bound`
用的），它在 Windows 上必然建命名管道 → `WinError 5` → 连服务都起不来。
[app/nicegui_patch.py](app/nicegui_patch.py) 在启动前把 `run.setup` 换成空操作就绕开了
（`io_bound` 走的是独立线程池，不受影响）。

**窗口与 DOM 断言仍然必须在不受限的环境里跑**：两者都要 QtWebEngine，而 **Chromium 的
浏览器↔渲染进程通信本身就是命名管道**，这部分绕不开——受限模式下窗口会白屏，DOM 断言脚本会
直接崩（退出码 `0x80000003`）。自己的终端里没有这个限制。

#### 为什么"批准一次"还会再问

批准的是**那一次调用**，不是这个会话：逐调用升级只把当前那条命令从沙箱里放出来，会话自身的模式
（`workspace-write` + 审批 `ask`）不变，所以下一条命令重新受限、重新弹授权。界面测试又是一条命令
里起好几个进程，于是变成跑一次问一次。

#### 一次管一个会话：`/permission`

在对话框里发：

```
/permission danger-full-access
```

（不带参数只报当前 preset 和所有可选项。）它把沙箱模式与审批策略**一起**改成 full access，作为
`permission/preset` 事件写进会话日志——**重启后靠回放依然生效**，且**只影响这个会话**；会话之间
的权限状态互相独立，所以每个新会话要各自切一次。

#### 一次管以后的会话

默认值由 `permission.defaultPreset` 决定，它是"设置"里的值，**只在新会话创建时读取**，之后改它
不会动已经存在的会话。在设置页把默认权限选成 full access 就行；这一项会写进
`C:\Users\HW\.dsh\profiles\desktop\cordis.patch.yml` 的 `permission` 条目。

也可以直接改这个 patch 文件：`sandbox-policy.mode` 是更硬的部署级兜底（现在是
`!!js DSH_PERMISSION_MODE ?? 'workspace-write'`），改它等于新会话的基础模式就是无约束。
注意 patch 条目的 `config` 是**整块替换、不深合并**，改 `sandbox-policy` 时必须把 `workspaceRoot`
一起重写。这两处都要**重启 DSH** 才生效。

### 装依赖

```
.venv\Scripts\python.exe -m pip install --no-cache-dir ^
    -i https://pypi.mirrors.ustc.edu.cn/simple/ -r requirements.txt
```

两条本机环境事实：pip 默认缓存目录权限是坏的（要 `--no-cache-dir`）、官方源不稳（要镜像）。
`requirements.txt` = 直接依赖；`requirements.lock.txt` = 锁定版本（复现 / 打包以 lock 为准）。

## 目录一览

| 目录 | 放什么 |
| --- | --- |
| `app/` | 界面层（NiceGUI）：布局、页面、组件、界面态；只做"画布局 / 渲染事件 / 调 services"三件事 |
| `core/` | 引擎层（禁 import 界面）：通用设施 `paths logging config db`、通用能力 `parse embed rerank retrieval llm`、功能 `features/rag/` |
| `data/` | 运行期数据：`helper.db`（+ 界面缓存 `qtwebengine/`）——不进版本控制 |
| `models/` | 本地 ONNX 模型（还没用到；不进版本控制） |
| `logs/` | 日志（不进版本控制） |
| `scripts/` | 开发脚本：`check_ui.py`（真实引擎里的界面 DOM 断言） |
| `tests/` | `unit`（不碰界面）+ `ui`（NiceGUI 的 user 装置，进程内模拟用户） |
