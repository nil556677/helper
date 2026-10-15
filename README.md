# helper

本地桌面 AI 助手：**资料在本机、检索在本机，只把命中的资料片段 + 你的问题交给云端大模型生成回答。**

- **界面**：NiceGUI 写界面（纯 Python），装在 **PySide6 QtWebEngine** 窗口里 —— 双击打开，不用浏览器
  （不是 pywebview：它要经 pythonnet 加载 .NET，本机实测 CLR 加载失败）
- **数据**：全部落在本机 `data/` 下（SQLite 库 + 资料副本 + 向量索引），备份 = 复制这个目录
- **模型调用**：自己写的**最小** OpenAI 兼容客户端（标准库 `urllib`，不引 SDK：这里只需要
  "发请求 + 按 SSE 读增量 + 把失败原因分好类"）
- **本地模型**：嵌入与精排都用 **ONNX Runtime** 跑（不引 torch / transformers），模型文件自己准备（见下）
- **资料解析**：自己写（`pypdf` / `python-docx` / `openpyxl` / `python-pptx` + 标准库），
  不引 langchain / unstructured：那套框架会带进来几十个依赖和一堆用不上的抽象
- **密钥**：用 Windows DPAPI 加密后存本地数据库，界面上只显示尾号

## 准备模型文件

**模型文件不进版本控制**（1.09 GB）。第一次用、或换机器 clone 之后，把下面三个文件放进 `models/`，
文件名要一模一样（代码里是硬编码路径）：

| 文件 | 大小 | 用途 | 从哪下 |
| --- | --- | --- | --- |
| `bge-m3.int8.onnx` | 542 MB | 嵌入：文本 → 768 维向量 | [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) 的 `onnx/` 导出（自己做 int8 动态量化） |
| `bge-reranker-v2-m3.int8.onnx` | 544 MB | 精排：`query` + 候选块 → 一个分数 | [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) 的 `onnx/` 导出（同上） |
| `tokenizer.json` | 16 MB | 分词（XLM-R，25 万词表） | 同上任一仓库的 `tokenizer.json`（**单独就够**，不需要 sentencepiece / transformers） |

国内网络把 `https://huggingface.co` 换成 `https://hf-mirror.com` 即可。

```bash
pip install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-m3 --include "onnx/*" "tokenizer.json" --local-dir ./models-tmp
```

**量化命令、自检脚本、以及两个必须知道的坑**（官方 ONNX 没有 pooling/sparse head，稠密向量要自己按
`CLS → 按行归一(keepdims) → 取前 768 维 → 再按行归一` 算）都写在下面。

### 两个关键事实（当初挑模型的实测结论，别再重新发现）

1. **官方 ONNX 只输出 `last_hidden_state [B, L, 1024]`** —— **没有** sparse head、**没有** pooling head。
   所以稠密向量必须自己算：`CLS → 按行 L2 归一 → 取前 768 维 → 再按行 L2 归一`，且**不加** query 前缀。
   > ⚠ 归一**必须带 `keepdims=True`**（按行做）。漏掉它就是把整批当成一个矩阵做 Frobenius 范数：
   > 行范数会掉到 0.58~0.72，后果是**绝对分被缩放、跨批不可比、任何余弦阈值失效**
   > （同批内排序看着还正常，所以这个坑很难靠眼睛发现）。
2. **官方不提供量化文件**。这两个 `.int8.onnx` 是自己做动态量化得到的（精排排序相关性没有可见退化，
   但**别再用 fp32 的分数阈值**）：

```python
from onnxruntime.quantization import QuantType, quantize_dynamic

quantize_dynamic("model.onnx", "bge-m3.int8.onnx", weight_type=QuantType.QInt8)
quantize_dynamic("reranker.onnx", "bge-reranker-v2-m3.int8.onnx", weight_type=QuantType.QInt8)
```

### 放进目录后自检

```bash
dir models            # 三个文件在、大小对得上

python -c "
import numpy, onnxruntime
from tokenizers import Tokenizer
tok = Tokenizer.from_file('models/tokenizer.json'); print('词表', tok.get_vocab_size())
sess = onnxruntime.InferenceSession('models/bge-m3.int8.onnx', providers=['CPUExecutionProvider'])
enc = tok.encode('测试一句话')
ids = numpy.array([enc.ids]); mask = numpy.array([enc.attention_mask])
hid = sess.run(None, {'input_ids': ids, 'attention_mask': mask})[0]
v = hid[:, 0, :]; v = v / numpy.linalg.norm(v, axis=-1, keepdims=True)
v = v[:, :768];    v = v / numpy.linalg.norm(v, axis=-1, keepdims=True)
print('维度', v.shape, '行范数', float(numpy.linalg.norm(v, axis=-1)[0]))
"
```

**行范数必须是 1.0**；0.58~0.72 那一档就是漏了 `keepdims=True`。

### 运行时相关

- 只用 **ONNX Runtime（CPU）**，**不引** torch / transformers；
- `intra_op_num_threads = 6`（实测最优；14 线程反而慢一倍以上）；
- 嵌入在**首次向量化**时加载，精排**首次需要精排时**才加载（省 550 MB 常驻）。

## 当前状态

**界面骨架与设置都在**：窗口能起（`logs/helper.log` 里看到 `界面已渲染`）、SQLite schema 已到 v3、
左栏两套模式（聊天 / 设置）、设置里有通用 / 模型配置 / 知识库 / 任务四个分区，
窗口大小与位置会记住。

**知识库已经能用**（设置 → 知识库）：`选择文件…` 导入资料，本地解析 → 切分 → 建索引，
列表里能看到片段数与状态（**失败的也会列出来并写清原因**），能逐条重建、删除。
支持 `.txt .md .markdown .log .csv .tsv .json .html .htm .pdf .docx .xlsx .xlsm .pptx`
（老式 `.doc/.xls/.ppt` 会提示"另存为新格式"）；同一份内容重复导入会自动跳过。

**任务系统已经能用**（设置 → 任务）：全项目的后台任务都从这儿看——正在跑、排队、最近完成/失败。
导入资料是**排队进行**的（上传先落 `data/inbox/`，再交给后台任务解析入库），
关窗口时正在跑的会记下来、下次打开接着做。队列可暂停/恢复，任务可取消、失败可重试、
失败原因能点开看完整时间线（每次失败/中断各一笔，含报错原文）。

**还没做的**：**向量化与本地检索、接大模型出真实答案**（嵌入模型与检索方案已定稿，正在按
里程碑落地）、扫描件/图片的 OCR、记忆召回、查询路由。所以**聊天页现在用的还是模拟数据**
（`app/ui/screens/_chat_mock.py`）：版式、多会话与各种内容格式都是真的，答案与出处是预置的，
界面上也明确标注了这一点。

## 文档

**内部方案文档在 `docs/`，不进版本控制**（项目定位、技术栈、分层红线、目录、数据模型、
任务系统、检索与模型的全部设计与实测依据、RAG 开发计划）。本 README 只写"外人需要知道的东西"：
怎么跑起来、要准备什么、目录长什么样。

方案与代码冲突时以方案为准；要改方案就先改那份文档，并记进它的变更记录。

## 开发 / 测试怎么跑

**一律在仓库根下执行**：包根是 `app/`，靠"仓库根在 `sys.path` 里"才能 import
（`pytest.ini` 里也是这么配的），换个目录就会 import 失败。

### 启动应用

```
.venv\Scripts\python.exe -m app
```

- 弹出一个 QtWebEngine 窗口；日志在 `logs/helper.log`，看到 `界面已渲染（根节点到位）` 就是好了
- **正常关窗口**才保存窗口大小与位置（存在 `data/helper.db` 的 `settings` 表里）；强杀不保存
- 起不来时先看 `logs/startup-error.log`：启动前提不满足（目录建不出来 / 库损坏 / 迁移缺脚本）
  都会写在那里，同时弹一个提示框
- 数据全在 `data/`（**路径写死**在安装目录旁，没有改道的开关）；想要一份干净数据：把 `data/` 改名或删掉再启动
- 改的是 Python 代码就**重启应用**（NiceGUI 的热重载在桌面壳里是关掉的）

### 配置文件

首次启动会在安装目录旁生成 `config.json`（模板是 `config.example.json`，里面带注释键说明每一项）。
**只放"打开数据库之前就必须知道"的东西**，其余配置都在程序里改（存 SQLite）：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `log_level` | `INFO` | 文件日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`）。**界面上不提供**——正常别动它，排障时才调 |
| `host` | `127.0.0.1` | 界面服务的监听地址。**默认只对本机开放**；填 `0.0.0.0` 或局域网 IP 会让同网络的人都能打开界面（界面没有认证），启动时会告警 |

日志在 `logs/`：`helper-<日期>.log`（程序自己的记录）+ `console-<日期>.log`（Qt/Chromium 等原生输出，
白板这类问题的现场都在这里），**都是按天一个文件、只留最近 7 天**。

### 跑测试

```
.venv\Scripts\python.exe -m pytest                # 全部（单测 + 界面测试）
.venv\Scripts\python.exe -m pytest tests/unit -q  # 只跑单测：快，不碰界面
.venv\Scripts\python.exe -m pytest tests/ui -q    # 只跑界面测试（进程内模拟用户，不需要浏览器）
```

- 测试的临时目录与数据都在 `.tmp/` 下，**不会碰真实的 `data/`**（界面测试用 `.tmp/ui-test-root`）
- 唯一例外：测试进程的日志会追加进真实的 `logs/helper.log`（日志 sink 在进程内只建一次）
- `tests/unit/test_layering.py` 是**分层红线**的机械检查：`app/core` 不许碰界面与 `services`、
  `app/ui` 不许碰 `core`、`app/services` 不许碰界面 —— 越界会直接测试失败

### 想验"界面在真实浏览器引擎里渲染成什么样"

用户模拟看不到 markdown-it 的产物，所以要靠 [scripts/check_ui.py](scripts/check_ui.py)
（QtWebEngine 真渲染后取 HTML 断言）：

```
.venv\Scripts\python.exe -u scripts\check_ui.py
```

它打印 **47 项**断言（表格是否真渲染成 `<table>`、代码块容器、引用角标、过程行可收起、
会话列表、分页页脚区间、自动刷新三件套、批量动作……）。**退出码全过为 0**，可以进 CI。
这一层抓到过真问题：代码块被浏览器内置 HTML 消毒丢掉 `<pre>`、会话列表从不画初始状态、
空库时分页断言写错。

### 两条环境限制（踩过，别重复诊断）

1. **窗口与 DOM 断言必须在不受限的环境里跑**：两者都要 QtWebEngine，而 Chromium 的
   浏览器↔渲染进程通信本身就是命名管道，受限沙箱下建不了 → **窗口白屏**、断言脚本直接崩。
   判断依据：日志里没有 `界面已渲染`，stderr 出现 `Check failed: . : 拒绝访问 (0x5)`。
2. **受限沙箱里 `pip` 用不了**（它走 `tempfile.mkdtemp` 建 0o700 目录，受限令牌下写不进去），
   用 [scripts/install_wheels.py](scripts/install_wheels.py) 代替：

```
.venv\Scripts\python.exe scripts\install_wheels.py <包名…>     # 自动解析依赖，等价于 pip
```

  自己的机器上没有这个限制，用 `pip install -r requirements.txt` 即可
  （`requirements.txt` = 直接依赖；`requirements.lock.txt` = 锁定版本，复现/打包以 lock 为准）。

## 目录一览

| 目录 | 放什么 |
| --- | --- |
| `app/` | 应用根。组装入口：`__main__.py`（起服务 → 开窗口 → 优雅退出）、`bootstrap.py`、`tasks.py` |
| `app/ui/` | 界面层：`shell.py`（两套模式）、`screens/`（一页一个文件）、`components/`（分页页脚、自动刷新）、`state/`、`resources/`（样式）。只做"画布局 / 渲染事件 / 调 services"三件事 |
| `app/services/` | 编排层（**UI-free**）：把引擎能力拼成一次操作，界面与 engine 之间的唯一接口 |
| `app/core/` | 引擎层（**禁 import 界面**）：`paths logging config db security jobs llm`、`parse embed rerank vector retrieval`、`features/{rag,memory}` |
| `data/` | 运行期数据：`helper.db` + 资料副本 `files/` + `qdrant/` + 暂存 `inbox/` + 词库 `userdict.txt`（不进版本控制） |
| `models/` | 本地 ONNX 模型（见上；文件不进版本控制，只留说明） |
| `logs/` | 日志（不进版本控制） |
| `scripts/` | 开发脚本：`check_ui.py`（真实引擎里的 DOM 断言）、`install_wheels.py` |
| `tests/` | `unit`（不碰界面）+ `ui`（NiceGUI 的 user 装置，进程内模拟用户） |
| `docs/` | 内部方案文档（不进版本控制） |

## 发布流程

版本号唯一来源 = [pyproject.toml](pyproject.toml) 的 `version`（设置页"关于"、自检命令都从它读，
**不要在代码里再写一份版本号**）。内部文档不进版本控制，公开可查的变更轨迹只有 `CHANGELOG.md` ——
**每次发布都要在上面补一条**。四步（在仓库根执行）：

```powershell
# 1) 改版本号（改 pyproject.toml 的 version，如升到 0.2.0）
#    改完顺带在 CHANGELOG.md 顶部补一条对应当前版本的改动

# 2) 全检：lint + 单测 + 全量 + 依赖漂移 +（能跑就）真实渲染
.venv\Scripts\python.exe scripts\check.py

# 3) 提交 + 打 tag（tag 名 = 版本号前面加 v）
git commit -m "<本次变更的标题>"
git tag v0.1.0

# 4) 推送（网络允许时；离线环境可先打 tag，等有网再 push）
git push
git push origin v0.1.0
```

> `git log` 用的是版本号在 `pyproject.toml` 里那个；发布前先确认 `check.py` 全绿、tag 名与版本号一致。
