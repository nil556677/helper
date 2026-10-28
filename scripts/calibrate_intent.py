"""意图路由 gold 评测 + L2 阈值/margin 标定。

输入 JSONL（每行）::

    {"text": "帮我查下订单 123456", "intent": "tool_call", "tool": "query_order"}

输出：L1 覆盖率/准确率、L2 增量、全局 grid search 得到的 ``threshold`` / ``margin``
建议值，以及可直接抄进 `app/core/intent/thresholds.py` 的代码块。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.intent import exemplars, rules  # noqa: E402
from app.core.intent.schema import Intent  # noqa: E402

DEFAULT_GOLD = ROOT / "tests" / "eval" / "data" / "intent_gold.example.jsonl"


def _route(intent: str, tool: str = "") -> str:
    if intent == Intent.CHITCHAT.value:
        return "chitchat"
    if intent == Intent.HUMAN_HANDOFF.value:
        return "human_handoff"
    return tool or "search_kb"


def _load(path: Path) -> list[dict]:
    items: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        text = str(raw.get("text") or "").strip()
        intent = str(raw.get("intent") or "").strip()
        if not text or not intent:
            continue
        items.append({"text": text, "route": _route(intent, str(raw.get("tool") or ""))})
    return items


def _l1(item: dict):
    decision = rules.classify(item["text"])
    if decision is None:
        return None
    return _route(str(decision.intent), decision.tool)


def _case(item: dict) -> dict:
    """预先算好 L1 与 L2（**只 embed 一次**，网格搜索复用）。"""
    l1 = _l1(item)
    scored = None if l1 is not None else exemplars.score_all(item["text"])
    return {"expected": item["route"], "l1": l1, "scored": scored}


def _evaluate(cases: list[dict], threshold: float, margin: float) -> dict:
    correct = wrong = abstain = 0
    for case in cases:
        if case["l1"] is not None:
            predicted = case["l1"]
        else:
            scored = case["scored"] or []
            predicted = "search_kb"  # 兜底默认
            if scored:
                route, score, _ex = scored[0]
                second = scored[1][1] if len(scored) > 1 else 0.0
                if score >= threshold and score - second >= margin:
                    predicted = route
                else:
                    abstain += 1
        if predicted == case["expected"]:
            correct += 1
        else:
            wrong += 1
    total = max(1, len(cases))
    return {
        "threshold": round(threshold, 2),
        "margin": round(margin, 2),
        "accuracy": round(correct / total, 4),
        "wrong": wrong,
        "abstain": abstain,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    items = _load(args.gold)
    if not items:
        raise SystemExit(f"gold 为空或格式不对：{args.gold}")

    cases = [_case(item) for item in items]
    l1_hits = sum(1 for case in cases if case["l1"] is not None)
    l1_correct = sum(1 for case in cases if case["l1"] == case["expected"])
    print(f"[gold] {len(items)} 条；L1 覆盖 {l1_hits}/{len(items)}，L1 正确 {l1_correct}/{len(items)}")

    best = None
    for threshold in [round(0.40 + i * 0.02, 2) for i in range(26)]:
        for margin in [round(0.02 + i * 0.02, 2) for i in range(10)]:
            report = _evaluate(cases, threshold, margin)
            key = (report["accuracy"], -report["wrong"], -report["abstain"])
            if best is None or key > best[0]:
                best = (key, report)
    assert best is not None
    report = best[1]
    print(
        f"[suggest] threshold={report['threshold']} margin={report['margin']} "
        f"accuracy={report['accuracy']:.2%} wrong={report['wrong']} abstain={report['abstain']}"
    )
    print("\n# 建议写回 app/core/intent/thresholds.py：")
    print("THRESHOLDS = {")
    for route in ("chitchat", "search_kb", "query_order", "weather", "human_handoff"):
        print(f'    "{route}": {report["threshold"]},')
    print("}")
    print(f"MARGIN = {report['margin']}")

    out = args.out or (ROOT / ".tmp" / "eval" / "intent-calibration.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"gold": str(args.gold), "best": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
