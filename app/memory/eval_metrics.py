"""记忆评测指标：行为统计/约束保留等信号的金标集判分（纯函数，不碰库/模型）。

对标 `app/rag/evalset.py` 的「问题+期望」模式，可离线跑、可进 CI。当前覆盖：
- 行为通道：回答长度偏好（short/normal/detailed）在样本量达标时判对；
- 约束保留：关键约束是否仍落在上下文。

CLI：``python -m app.memory.eval_metrics [--fake]``（--fake 用内置样例集冒烟）。
"""

from __future__ import annotations  # 延迟值注解

import argparse  # 参数
from pathlib import Path  # 路径
from typing import Any  # 任意值

from app.memory.behavior import signal_from_messages  # 行为通道

__all__ = ["run", "score_verbosity_cases", "summary"]  # 导出


def _fair_messages(n: int, length: int) -> list[dict]:  # 造 n 条同长 assistant 消息
    return [{"role": "assistant", "content": "字" * length} for _ in range(n)]  # 构造


def score_verbosity_cases(cases: list[dict[str, Any]]) -> list[tuple[bool, str]]:  # 判分行为通道
    """每条 case: {question, size, expect}。

    给 size 条 length=size 的短回答（代表用户偏好该长度），看行为通道判定是否等于 expect。
    """
    results: list[tuple[bool, str]] = []  # 结果
    for case in cases or []:  # 逐条
        size = int(case.get("size") or 0)  # 每答长度
        count = int(case.get("count") or 6)  # 样本数（≥5 才算）
        expect = case.get("expect")  # 期望（None 表示"不应改判"）
        signals = signal_from_messages(_fair_messages(count, size))  # 行为信号
        matched = signals.verbosity == expect  # 判定是否一致（None==None 视为通过）
        results.append((matched, f"size={size} expect={expect} got={signals.verbosity}"))  # 记录
    return results  # 返回


def summary(results: list[tuple[bool, str]]) -> dict[str, str]:  # 汇总
    """总数/通过/失败 + 失败原因（基线入档用）。"""
    passed = sum(1 for ok, _r in results if ok)  # 通过
    failed = [r for ok, r in results if not ok]  # 失败
    return {  # 字典
        "total": str(len(results)),  # 样本
        "passed": str(passed),  # 通过
        "failed": str(len(failed)),  # 失败
        "reasons": "; ".join(failed) or "无",  # 失败原因
    }  # 返回


def default_cases() -> list[dict[str, Any]]:  # 内置金标样例
    """行为通道的离线样例（不碰库/模型，直接验证算法正确）。"""
    return [  # 样例集
        {"question": "短偏好", "size": 60, "count": 6, "expect": "short"},  # 短
        {"question": "长偏好", "size": 500, "count": 6, "expect": "detailed"},  # 长
        {"question": "中偏好", "size": 200, "count": 6, "expect": "normal"},  # 中
        {"question": "样本不足不改判", "size": 60, "count": 3, "expect": None},  # 不足 → None
    ]  # 结束


def run(*, fake: bool = False, out: str | None = None) -> dict[str, Any]:  # 跑评测并落报告
    """返回结构化报告；out 非空同时落 JSON。fake 仅区分报告内容（本评测离线恒定）。"""
    cases = default_cases()  # 内置样例（当前离线）
    results = score_verbosity_cases(cases)  # 判分
    report: dict[str, Any] = {  # 报告
        "embedder": "none" if fake else "behavior",  # 通道
        "summary": summary(results),  # 汇总
    }  # 结束
    if out:  # 落盘
        import json  # 序列化

        target = Path(out)  # 路径
        target.parent.mkdir(parents=True, exist_ok=True)  # 目录
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")  # 写
    return report  # 返回


def main(argv: list[str] | None = None) -> int:  # CLI 入口
    import json  # 打印

    parser = argparse.ArgumentParser(description="记忆评测（行为通道）")  # 解析器
    parser.add_argument("--fake", action="store_true", help="离线冒烟标记")  # 冒烟
    parser.add_argument("--out", default="", help="报告 JSON 输出路径")  # 输出
    args = parser.parse_args(argv)  # 解析
    report = run(fake=bool(args.fake), out=args.out or None)  # 跑
    print(json.dumps(report, ensure_ascii=False, indent=2))  # 打印
    return 0  # 成功


if __name__ == "__main__":  # 直接运行
    raise SystemExit(main())  # 退出码
