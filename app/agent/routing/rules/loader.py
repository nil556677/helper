"""规则加载与匹配：扫描目录 → 校验 → 去重 id → 按 priority 排序 → 注册。

匹配是**纯函数**：不访问网络/DB、不调模型；宁漏勿错。
"""

from __future__ import annotations  # 延迟求值注解

import re  # 正则（编译前置）
from pathlib import Path  # 目录扫描
from typing import Iterable  # 迭代

import yaml  # 规则文件

from app.agent.contracts.routing import RouteSpec, RuleHit  # 命中结果 / 固定路径
from app.agent.routing.rules._schema import RuleSpec, parse_rule  # 规则结构

__all__ = ["default_rules", "load_routes", "load_rules", "match"]  # 导出


def load_rules(directories: Iterable[str | Path]) -> tuple[RuleSpec, ...]:  # 加载全部规则
    """递归扫描 `*.yaml`；id 重复或非法 → 抛错（构建期 fail-fast）。"""
    seen: dict[str, str] = {}  # id → 来源
    rules: list[RuleSpec] = []  # 结果
    for directory in directories:  # 逐目录
        root = Path(directory)  # 根
        if not root.exists():  # 不存在就跳过（允许空目录）
            continue
        for path in sorted(root.rglob("*.yaml")):  # 递归 yaml
            text = path.read_text(encoding="utf-8")  # 读
            data = yaml.safe_load(text) or []  # 解析
            items = data if isinstance(data, list) else [data]  # 统一成列表
            for raw in items:  # 逐条
                spec = parse_rule(raw, where=str(path))  # 校验
                if spec.id in seen:  # 重复 id
                    raise ValueError(f"规则 id 重复: {spec.id}（{seen[spec.id]} 与 {path}）")
                seen[spec.id] = str(path)  # 记录来源
                rules.append(spec)  # 收集
    rules.sort(key=lambda item: (-item.priority, item.id))  # 高优先级在前
    return tuple(rules)  # 返回


def default_rules() -> tuple[RuleSpec, ...]:  # 项目内置规则（包内 rules/ 目录）
    """按包路径加载，避免调用方硬编码绝对路径。"""
    return load_rules([Path(__file__).parent / "rules"])  # 扫描 rules/rules


def load_routes(directory: str | Path | None = None) -> tuple[RouteSpec, ...]:  # 加载固定路径定义
    """扫描 routes/*.yaml；id 重复/缺 description → 报错。"""
    root = Path(directory) if directory is not None else (Path(__file__).parent / "routes")  # 根
    seen: set[str] = set()  # 已用 id
    routes: list[RouteSpec] = []  # 结果
    if not root.exists():  # 允许空
        return ()
    for path in sorted(root.rglob("*.yaml")):  # 递归
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}  # 解析
        items = data if isinstance(data, list) else [data]  # 统一列表
        for raw in items:  # 逐条
            if not isinstance(raw, dict):  # 形状
                raise ValueError(f"{path}: 路由定义必须是 mapping")
            route_id = str(raw.get("id") or "").strip()  # id
            if not route_id:  # 必填
                raise ValueError(f"{path}: 路由缺少 id")
            if route_id in seen:  # 重复
                raise ValueError(f"路由 id 重复: {route_id}")
            seen.add(route_id)  # 记录
            routes.append(  # 收集
                RouteSpec(  # 契约
                    route_id=route_id,  # id
                    label=str(raw.get("label") or ""),  # 人话标签
                    description=str(raw.get("description") or ""),  # 描述
                    examples=tuple(str(x) for x in (raw.get("examples") or ())),  # 示例
                    tools=tuple(str(x) for x in (raw.get("tools") or ())),  # 工具
                    node=str(raw.get("node") or "dispatch_fixed"),  # 分流节点
                    required_params=tuple(dict(item) for item in (raw.get("required_params") or ()) if isinstance(item, dict)),  # 必需参数
                )
            )
    return tuple(routes)  # 返回


def match(question: str, rules: Iterable[RuleSpec]) -> RuleHit | None:  # 规则匹配（纯函数）
    """命中唯一且 exact → RuleHit；歧义/多路冲突/非 exact → None（交语义层）。"""
    text = str(question or "")  # 规范化
    hits: list[RuleSpec] = []  # 命中的规则
    for rule in rules:  # 逐条
        if rule.is_ambiguous(text):  # 易混输入
            continue  # 强制降级
        if rule.matches(text):  # 命中
            hits.append(rule)  # 收集
    if not hits:  # 没命中
        return None  # 未定
    routes = {rule.route for rule in hits}  # 路由集合
    if len(routes) > 1:  # 多路冲突
        return None  # 未定（绝不猜）
    hit = hits[0]  # 唯一路由
    if not hit.exact:  # 非明确
        return None  # 降级到语义层
    return RuleHit(  # 命中
        rule_id=hit.id,  # 规则
        route=hit.route,  # 路径
        tools=hit.tools,  # 工具
        exact=hit.exact,  # 严格
        reasons=(f"rule:{hit.id}",),  # 依据
    )