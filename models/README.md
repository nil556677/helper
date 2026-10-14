# models/ —— 本地模型（**不进版本控制**）

这个目录里的模型文件加起来约 **1.09 GB**，所以 `.gitignore` 里只放行本说明（`/models/*` + `!/models/README.md`）。
**换一台机器 / 重新 clone 后必须按下面把三个文件放到本目录**，文件名要一模一样（代码里是硬编码的路径）。

## 需要的三个文件（本机实测大小）

| 文件 | 大小 | 用途 |
| --- | --- | --- |
| `bge-m3.int8.onnx` | 542.1 MB | **嵌入**：文本 → 768 维向量（`last_hidden_state [B,L,1024]` 经 CLS 取前 768 维） |
| `bge-reranker-v2-m3.int8.onnx` | 544.3 MB | **精排**：`query</s></s>doc` → 一个分数（`logits [B,1]`） |
| `tokenizer.json` | 16.3 MB | 分词（XLM-R，25 万词表）。**单独就够**——不需要 sentencepiece / transformers |

## 从哪下

两个模型都来自 HuggingFace（国内网络可用镜像 `hf-mirror.com` 替换域名）：

| 用途 | 上游仓库 |
| --- | --- |
| 嵌入 + 分词 | <https://huggingface.co/BAAI/bge-m3>（取其 `onnx/` 目录下的导出与 `tokenizer.json`） |
| 精排 | <https://huggingface.co/BAAI/bge-reranker-v2-m3>（取其 `onnx/` 目录下的导出） |

```bash
# 例：只取需要的文件（需要 git-lfs 或直接下 resolve 链接；文件名以仓库里的实际文件为准）
pip install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-m3 --include "onnx/*" "tokenizer.json" --local-dir ./models-tmp
# 国内镜像：export HF_ENDPOINT=https://hf-mirror.com 后再跑同一条命令
```

> **两个关键事实**（当初挑模型的实测结论，别再重新发现）：
> ① 官方 ONNX **只输出 `last_hidden_state`**——**没有** sparse head、**没有** pooling head，
>    所以稠密向量必须自己算：`CLS → 按行 L2 归一（必须 keepdims）→ 截前 768 维 → 再按行 L2 归一`；
> ② 官方**不提供量化文件**，本机这两个 `.int8.onnx` 是自己做动态量化得到的。

## int8 动态量化怎么来的（要自己生成时照这个做）

```python
from onnxruntime.quantization import QuantType, quantize_dynamic

quantize_dynamic("model.onnx", "bge-m3.int8.onnx", weight_type=QuantType.QInt8)
quantize_dynamic("reranker.onnx", "bge-reranker-v2-m3.int8.onnx", weight_type=QuantType.QInt8)
```

需要 `onnxruntime`（运行时已包含 quantizer）。量化后精度实测量级：精排排序相关性没有可见退化，
但**别再用 fp32 的分数阈值**（见 `技术方案.md` §9.8）。

## 放进目录后怎么确认没问题

```bash
# 1) 三个文件在、大小对得上
dir models

# 2) 能加载、编码一段中文、行范数是 1.0（P0 之后会提供 scripts/bench_models.py，输出更全）
python -c "
import numpy, onnxruntime
from tokenizers import Tokenizer
tok = Tokenizer.from_file('models/tokenizer.json'); print('词表', tok.get_vocab_size())
sess = onnxruntime.InferenceSession('models/bge-m3.int8.onnx', providers=['CPUExecutionProvider'])
enc = tok.encode('测试一句话'); import numpy as np
ids = np.array([enc.ids]); mask = np.array([enc.attention_mask])
hid = sess.run(None, {'input_ids': ids, 'attention_mask': mask})[0]
v = hid[:, 0, :]; v = v / np.linalg.norm(v, axis=-1, keepdims=True)
v = v[:, :768]; v = v / np.linalg.norm(v, axis=-1, keepdims=True)
print('维度', v.shape, '行范数', float(np.linalg.norm(v, axis=-1)[0]))
"
```

行范数必须是 **1.0**；如果算出来是 0.58~0.72 那一档，就是**漏了 `keepdims=True`**（归一成了整批一个 Frobenius 范数，
后果是绝对分被缩放、跨批不可比、阈值全废——这是当初踩过的坑）。

## 与运行时相关的事实

- 只用 **ONNX Runtime**（CPU），**不引** torch / transformers；
- `intra_op_num_threads = 6`（实测最优；14 线程反而慢一倍以上）；
- 嵌入在**首次向量化**时加载；精排**首次需要精排时**才加载（省 550 MB 常驻）；
- 模型文件不参与打包决策（打包暂缓），届时再定"进安装包还是首启下载"。
