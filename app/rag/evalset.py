"""评测集的装载与判分（纯函数）：**不碰模型、不碰库**，方便单测与 CI。

评测集是一组"问题 + 期望"：该答的必须答出来，该拒的必须拒掉。
`score_case` 只吃"门限判决 + 证据"，所以既能离线跑，也能接在真实链路上。
"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 读评测集
from pathlib import Path  # 路径
from typing import Any  # 任意值

#: 默认评测集位置。
DEFAULT_CASES = Path("tests") / "eval" / "trust_cases.json"  # 相对仓库根

#: 期望取值。
ANSWER = "answer"  # 应当答（pass/weak 都算）
REJECT = "reject"  # 应当拒


def load_cases(path: Path | str | None = None) -> list[dict[str, Any]]:  # 装载
    """读 JSON 评测集；字段缺省按空处理。"""
    target = Path(path) if path is not None else DEFAULT_CASES  # 目标文件
    raw = json.loads(target.read_text(encoding="utf-8"))  # 解析
    if not isinstance(raw, list):  # 必须是数组
        raise ValueError("评测集必须是 JSON 数组")  # 报错
    cases: list[dict[str, Any]] = []  # 结果
    for item in raw:  # 逐条
        if not isinstance(item, dict) or not item.get("question"):  # 必须有 question
            continue  # 跳过坏条目
        case = dict(item)  # 复制
        case.setdefault("expect", ANSWER)  # 默认"该答"
        case.setdefault("must_contain", [])  # 默认不强求内容
        cases.append(case)  # 收集
    return cases  # 返回


def score_case(case: dict[str, Any], gate: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[bool, str]:  # 判分
    """返回 `(是否通过, 原因)`。"""
    decision = str((gate or {}).get("decision") or "")  # 门限判决
    expect = str(case.get("expect") or ANSWER)  # 期望
    if expect == REJECT:  # 该拒
        if decision == "reject":  # 确实拒了
            return True, "rejected"  # 通过
        return False, f"应拒未拒（decision={decision or 'unknown'}）"  # 失败
    if decision not in ("pass", "weak"):  # 该答却没通过
        return False, f"应答未答（decision={decision or 'unknown'}）"  # 失败
    if not evidence:  # 没有证据
        return False, "判决通过但没有证据"  # 失败
    body = "\n".join(str(item.get("text") or "") for item in evidence)  # 证据全文
    for needle in case.get("must_contain") or []:  # 逐条关键内容
        if str(needle) not in body:  # 没出现
            return False, f"证据里缺少「{needle}」"  # 失败
    return True, "answered"  # 通过


def summarize(results: list[tuple[bool, str]]) -> dict[str, int]:  # 汇总
    """总数 / 通过 / 失败。"""
    passed = sum(1 for ok, _ in results if ok)  # 通过数
    return {"total": len(results), "passed": passed, "failed": len(results) - passed}  # 汇总
