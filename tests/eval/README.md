# 检索评测（gold 数据）

评测脚本：`scripts/eval_retrieval.py`。算 chunk / parent 两级的
**Recall@k / Precision@k / Hit@k / MRR / NDCG@k**（带 bootstrap 95% CI）。

## 文件放在哪

```
tests/eval/
├── README.md
└── data/
    ├── gold.template.xlsx    # ★ 直接用 Excel 打开、填、保存（推荐）
    └── gold.example.jsonl    # JSONL 写法示例（可选）
```

## Excel 模板怎么填

打开 `gold.template.xlsx`，第 1 个 sheet「gold」表头不要改：

| 列 | 含义 | 示例 |
| --- | --- | --- |
| `query` | 用户会怎么问 | `合同违约金怎么计算` |
| `relevant_chunks` | 该问题的**相关 child key**，多个用 `;` 分隔 | `74:1:5;74:1:6` |
| `relevant_parents` | 相关父块 key，多个用 `;` 分隔 | `74:1:5` |
| `negative` | 负例填 `1`（此时相关列留空） | `1` |
| `answer` | 期望答案要点（评测暂不用，留给答案正确率） | `按合同约定比例` |
| `notes` | 备注 | `这条容易和 XX 混` |

第 2 个 sheet「说明」里也有同样的填写说明。

- 多个 key 用**英文分号 `;`** 分隔；
- 行数随意加，空 `query` 行会被跳过；
- `.xlsx` 不存在编码问题，直接在 Excel 里保存即可。

**口径提醒**：
- `relevant_chunks` 要标**该 query 的全部相关块**，才是真 Recall；
  只标一个时 `R@k` 退化为 `Hit@k`（仍能看排序趋势）。
- 想做 Precision，需要把 top-k 里**每条**都判过（未标按不相关算→低估）；
  建议先跑三种模式取并集（pooling）再逐条标。
- 负例用来观察“无答案时是否硬凑”，也是定 `score_floor` 的依据。

## 跑法

```bash
python scripts/eval_retrieval.py --gold tests/eval/data/gold.template.xlsx
python scripts/eval_retrieval.py --gold tests/eval/data/gold.template.xlsx --modes semantic,hybrid --k 1,3,5,10
```

## 怎么拿到 key

- 知识库详情页显示片段的 `parent_key`；child key 形如 `74:1:5`（doc:version:seq）。
- 或读 Qdrant `rag_chunks` 的 payload `key` / `parent_key`。
- key 写成零填充（`74:01:05`）也能匹配（脚本会规范化），但**别把 doc 写错**。

## 注意

- `keyword` 通道目前是空实现（`keyword_unavailable`），它的指标要等 sparse 落地才有意义。
- 伪查询标定（`scripts/calibrate_retrieval.py`）只能定候选池饱和点，**不能替代本评测**。
