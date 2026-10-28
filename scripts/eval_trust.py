"""评测脚本：把 `tests/eval/trust_cases.json` 跑一遍，输出"该答/该拒"的通过率。

用法（**在仓库根执行**）：::

    python scripts/eval_trust.py --dry-run     # 只校验评测集格式（不需要库和模型）
    python scripts/eval_trust.py               # 真跑（需要 Qdrant 与 ONNX 模型可用）
    python scripts/eval_trust.py --min-pass 0.95

退出码：通过率低于 `--min-pass` 或任一失败时为 1，方便进 CI。

**为什么单独一个脚本而不是塞进 pytest**：真跑要拉起 Qdrant + 两个 ONNX 模型（数秒到数十秒），
不适合每次提交都跑；单测只覆盖纯判分逻辑（见 `tests/unit/test_evalset.py`）。
"""

from __future__ import annotations  # 类型注解延迟求值

import argparse  # 参数
import asyncio  # 事件循环
import sys  # 路径
from pathlib import Path  # 路径

ROOT = Path(__file__).resolve().parents[1]  # 仓库根
sys.path.insert(0, str(ROOT))  # 脚本从 scripts/ 启动时也能 import app

from app.rag import evalset  # noqa: E402 - 必须在 sys.path 调整之后导入


async def _evaluate(cases: list[dict], *, verbose: bool) -> list[tuple[bool, str]]:  # 跑评测
    """逐条跑 RAG 子图，返回判分结果。"""
    from app.agent.nodes.rag.factory import real_steps  # 真实步骤
    from app.agent.nodes.rag.node import RagRunner  # 运行器

    runner = RagRunner(real_steps())  # 编译一次
    results: list[tuple[bool, str]] = []  # 结果
    for case in cases:  # 逐条
        try:  # 单条失败不拖垮整轮
            outcome = await runner.run(str(case["question"]))  # 检索 + 门限
        except Exception as exc:  # 环境问题（模型缺失/库没起）
            results.append((False, f"运行失败：{type(exc).__name__}: {exc}"))  # 记失败
            continue  # 下一条
        ok, reason = evalset.score_case(case, outcome.gate, outcome.evidence)  # 判分
        results.append((ok, reason))  # 记录
        if verbose:  # 明细
            mark = "PASS" if ok else "FAIL"  # 标记
            print(f"[{mark}] {case.get('id', '?')} {case['question']} → {reason}")  # 一行
    return results  # 返回


def main() -> int:  # 入口
    """解析参数、跑评测、打印汇总。"""
    parser = argparse.ArgumentParser(description="答案可信评测")  # 参数解析
    parser.add_argument("--cases", default=None, help="评测集路径（默认 tests/eval/trust_cases.json）")  # 路径
    parser.add_argument("--dry-run", action="store_true", help="只校验评测集格式")  # 干跑
    parser.add_argument("--min-pass", type=float, default=0.9, help="通过率门槛（0~1）")  # 门槛
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")  # 安静
    args = parser.parse_args()  # 解析

    cases = evalset.load_cases(args.cases)  # 装载
    answer_cases = sum(1 for case in cases if case["expect"] == evalset.ANSWER)  # 该答条数
    reject_cases = len(cases) - answer_cases  # 该拒条数
    if args.dry_run:  # 只校验格式
        print(f"评测集 OK：{len(cases)} 条（该答 {answer_cases} / 该拒 {reject_cases}）")  # 打印
        return 0 if cases and answer_cases and reject_cases else 1  # 正反都要有

    results = asyncio.run(_evaluate(cases, verbose=not args.quiet))  # 跑
    summary = evalset.summarize(results)  # 汇总
    ratio = summary["passed"] / summary["total"] if summary["total"] else 0.0  # 通过率
    print(  # 汇总
        f"\n评测完成：{summary['passed']}/{summary['total']} 通过（{ratio:.1%}），门槛 {args.min_pass:.0%}"
    )  # 打印结束
    if not args.quiet:  # 失败明细
        for (ok, reason), case in zip(results, cases, strict=False):  # 对齐
            if not ok:  # 只看失败
                print(f"  ✗ {case.get('id', '?')} {case['question']} → {reason}")  # 一行
    return 0 if ratio >= args.min_pass else 1  # 达标才算过


if __name__ == "__main__":  # 直接运行
    raise SystemExit(main())  # 用返回值当退出码
