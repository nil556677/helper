"""演示：bge-m3 固定宽度 320 token 截断 → 答案在尾部的内容"搜不到"。

完全走项目在用的真实编码路径 app.core.embed.model.embed_texts / embed_text
（bge-m3 ONNX，768 维，PAD_TO=320，截断口径 = 取前 320 个 token）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# 让 `python scripts/demo_truncation_search.py` 能从仓库根 import app
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import paths  # noqa: E402
from app.core.embed.model import PAD_TO, embed_texts  # noqa: E402
from app.core.embed.tokenization import load  # noqa: E402


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # 两边都是单位向量，点积 = 余弦


def main() -> None:
    # ---- 拼材料 -------------------------------------------------------------
    # 一段与"洋葱种子采购"完全无关的通用汇报（充当 chunk B 前 320 token 的内容）。
    filler = (
        "项目组完成了年度巡检工作，覆盖了全省十二个地市的仓储网点，对设备运行状态进行了"
        "系统排查，并整理出问题清单，共发现缺陷二百余项。分批整改工作预计在下一季度完成，"
        "届时将统一提交验收材料。期间同步更新了操作手册与应急预案，组织了两轮桌面演练。"
    )

    # 真正的答案：洋葱种子采购单价。会作为 chunk B 的"尾巴"被截掉。
    answer = (
        "关键结论：公司本年度洋葱种子的采购单价为每公斤三元五角，采购总量为八十六吨，"
        "费用已于财报中列支。"
    )

    query = "我司今年洋葱种子采购单价是多少？"

    # chunk A（短）：答案在开头，必进向量 → 应该能搜到。
    chunk_a = answer

    # chunk B（长）：前面全是无关巡检汇报，答案只在最末尾。超过 320 token 后，
    # 尾部会被静默截掉，答案根本进不了向量 → 应该搜不到。
    chunk_b = filler * 14 + answer

    model_dir = paths.models_dir()
    tokenizer = load(model_dir / "tokenizer.json")

    def ntok(t: str) -> int:
        return len(tokenizer.encode(t).ids)

    ta, tb, tq = ntok(chunk_a), ntok(chunk_b), ntok(query)
    print(f"[PAD_TO 截断宽度] {PAD_TO} token")
    print(f"chunk A token 数 = {ta}   (< {PAD_TO}，答案在开头，不被截)")
    print(f"chunk B token 数 = {tb}   (> {PAD_TO}，答案在末尾，会被截尾)")
    print(f"query    token 数 = {tq}")
    print(f"\nchunk B 里答案句的 token 起点 ≈ {ntok(filler * 14)}（> {PAD_TO}，恰好落在被截掉的区域）")
    print("-" * 66)

    # ---- 编码（走项目正式接口，入库路径固定 16 行批） -------------------------
    docs_vec = embed_texts([chunk_a, chunk_b])
    q_vec = embed_texts([query])[0]

    # ---- 模拟一次检索：谁离 query 最近 ---------------------------------------
    print("\n【用 query 在三块候选里检索】")
    for name, v in [("chunk A(短,含答案)", docs_vec[0]), ("chunk B(长,答案在尾部)", docs_vec[1])]:
        print(f"  {name:<24} 余弦相似度 = {cosine(q_vec, v):.4f}")

    if cosine(q_vec, docs_vec[1]) < cosine(q_vec, docs_vec[0]):
        print("\n>>> 结果：确实 B 更远、A 更近 —— 答案明明在 chunk B 全文里，"
              "却因为被截尾" + "向量里根本没有它，搜不到。")
    else:
        print("\n>>> 若 B 反而更近，说明这批材料没触发截断差异，换更长的 filler 再试。")

    # ---- 顺手证实"截断口径 = 只看前 320 token"：B 与它自己的开头几乎同向量 ----
    head_only = filler * 14
    head_vec = embed_texts([head_only])[0]
    print("\n【证实截断】")
    print(f"  chunk B  <->  chunk B 只取开头({ntok(head_only)} token)  余弦 = "
          f"{cosine(docs_vec[1], head_vec):.4f}  (≈1，说明尾巴根本没影响向量)")


if __name__ == "__main__":
    main()
