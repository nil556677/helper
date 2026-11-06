"""配置版本指纹：编码器 / 路由表 / 阈值 → 逐请求可复现的指纹。"""

from __future__ import annotations  # 延迟求值注解

import hashlib  # 稳定哈希

from app.agent.routing.embeddings import ROUTE_EMBED_VERSION, source_text  # 编码器版本 + 源文本
from app.agent.routing.policy import DEFAULT_POLICY, PolicySpec  # 策略
from app.agent.routing.rules.loader import load_routes  # 路由定义

__all__ = ["encoder_version", "fingerprint", "routes_version", "thresholds_version"]  # 导出


def encoder_version() -> str:  # 编码器版本
    return ROUTE_EMBED_VERSION  # 常量


def routes_version() -> str:  # 路由表版本（内容哈希）
    """对 routes 源文本稳定排序后哈希；改描述/示例即变。"""
    routes = load_routes()  # 载入
    joined = "\n".join(sorted(source_text(spec) for spec in routes))  # 稳定拼接
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]  # 短哈希


def thresholds_version(policy: PolicySpec = DEFAULT_POLICY) -> str:  # 阈值版本
    return f"{policy.version}:{policy.direct_top}/{policy.direct_margin}/{policy.miss_top}"  # 可读指纹


def fingerprint(policy: PolicySpec = DEFAULT_POLICY) -> dict[str, str]:  # 汇总指纹
    """三项版本 + 组合短哈希（逐请求日志记录）。"""
    parts = {  # 分项
        "encoder_version": encoder_version(),  # 编码器
        "routes_version": routes_version(),  # 路由表
        "thresholds_version": thresholds_version(policy),  # 阈值
    }  # 结束
    joined = "|".join(parts.values())  # 组合
    parts["config_fingerprint"] = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]  # 组合指纹
    return parts  # 返回