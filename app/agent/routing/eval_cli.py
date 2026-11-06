"""评估闭环 CLI：金标集 → 建索引 → 多策略对比 → 输出报告。

用法：
  python -m app.agent.routing.eval_cli                  # 本地 bge-m3
  python -m app.agent.routing.eval_cli --fake           # 离线冒烟（哈希编码）
  python -m app.agent.routing.eval_cli --out .tmp/report.json
"""

from __future__ import annotations  # 延迟求值注解

import argparse  # 参数
import hashlib  # 离线哈希编码
import json  # 报告
from pathlib import Path  # 路径
from typing import Any  # 任意值

from app.agent.routing.embeddings import DEFAULT_STORE, ROUTE_EMBED_VERSION, default_embed, load_or_build  # 索引
from app.agent.routing.eval import DEFAULT_GOLDEN, compare_policies, load_golden, summary  # 评估
from app.agent.routing.policy import CANDIDATE_POLICY, DEFAULT_POLICY  # 策略
from app.agent.routing.rules.loader import default_rules, load_routes  # 规则/路由
from app.agent.routing.versioning import fingerprint  # 指纹

__all__ = ["main", "run"]  # 导出


def _hash_embed(text: str, dim: int = 8) -> list[float]:  # 离线确定性编码（仅冒烟）
    """对文本取稳定哈希前 dim 字节归一；只为跑通闭环，不代表语义质量。"""
    digest = hashlib.sha256(str(text).encode("utf-8")).digest()  # 哈希
    return [byte / 255.0 for byte in digest[:dim]]  # 归一


def run(  # 跑一次评估
    *,
    fake: bool = False,  # 离线冒烟
    golden_path: str | Path = DEFAULT_GOLDEN,  # 金标集
    out: str | Path | None = None,  # 报告输出
    store: str | Path | None = None,  # 索引库
) -> dict[str, Any]:  # 报告
    """返回结构化报告；out 非空时同时落 JSON。"""
    rules = default_rules()  # 规则
    routes = load_routes()  # 路由
    embed = _hash_embed if fake else default_embed()  # 编码器
    version = f"{ROUTE_EMBED_VERSION}:fake" if fake else ROUTE_EMBED_VERSION  # 版本区分
    index = load_or_build(routes, embed, store_path=Path(store) if store else DEFAULT_STORE, model_version=version)  # 索引
    golden = load_golden(golden_path)  # 金标
    results = compare_policies(rules, index, golden, embed, (DEFAULT_POLICY, CANDIDATE_POLICY))  # 多策略
    report: dict[str, Any] = {  # 报告
        "fingerprint": fingerprint(),  # 配置指纹
        "embedder": "fake" if fake else "bge-m3",  # 编码器
        "golden_size": len(golden),  # 样本数
        "policies": {version_name: summary(result) for version_name, result in results.items()},  # 各策略结果
    }  # 结束
    if out is not None:  # 需要落盘
        target = Path(out)  # 路径
        target.parent.mkdir(parents=True, exist_ok=True)  # 确保目录
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")  # 写
    return report  # 返回


def main(argv: list[str] | None = None) -> int:  # CLI 入口
    parser = argparse.ArgumentParser(description="意图路由评估闭环")  # 解析器
    parser.add_argument("--fake", action="store_true", help="离线哈希编码（冒烟）")  # 开关
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="金标集路径")  # 金标
    parser.add_argument("--out", default="", help="报告 JSON 输出路径")  # 输出
    parser.add_argument("--store", default="", help="路由向量库路径")  # 库
    args = parser.parse_args(argv)  # 解析
    report = run(  # 跑
        fake=bool(args.fake),  # 离线
        golden_path=args.golden,  # 金标
        out=args.out or None,  # 输出
        store=args.store or None,  # 库
    )  # 结束
    print(json.dumps(report, ensure_ascii=False, indent=2))  # 打印
    return 0  # 成功


if __name__ == "__main__":  # 直接运行
    raise SystemExit(main())  # 退出码