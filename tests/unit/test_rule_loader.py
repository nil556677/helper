"""规则加载与匹配：目录发现、宁漏勿错、歧义/冲突降级、重复 id 报错。"""

from __future__ import annotations  # 延迟求值注解

from pathlib import Path  # 临时目录

import pytest  # 测试框架
import yaml  # 写规则文件

from app.agent.routing.rules.loader import default_rules, load_routes, load_rules, match  # 加载器


def test_default_rules_and_routes_load() -> None:  # 内置规则/路径可加载
    rules = default_rules()  # 规则
    assert {rule.id for rule in rules} >= {"direct.time", "direct.weather"}  # 至少两条
    routes = load_routes()  # 路由
    assert {route.route_id for route in routes} >= {"time", "weather"}  # 至少两条


def test_match_hits_exact_rule() -> None:  # 明确命中
    rules = default_rules()  # 规则
    hit = match("现在几点", rules)  # 时间
    assert hit is not None and hit.route == "time"  # 命中 time
    hit = match("北京天气怎么样", rules)  # 天气
    assert hit is not None and hit.route == "weather"  # 命中 weather


def test_match_ambiguous_examples_fall_through() -> None:  # 易混输入强制降级
    rules = default_rules()  # 规则
    assert match("几点开始", rules) is None  # 不命中


def test_match_conflict_returns_none() -> None:  # 多路冲突不猜
    rules = load_rules([Path(__file__).parent])  # 用临时构造
    tmp = Path("__tmp_rules__")  # 临时目录
    try:  # 写两条冲突规则
        tmp.mkdir(exist_ok=True)  # 建目录
        (tmp / "a.yaml").write_text(  # 规则 A
            yaml.safe_dump([{"id": "a", "priority": 1, "exact": True, "match": {"any": ["冲突"]}, "action": {"route": "a"}}], allow_unicode=True),
            encoding="utf-8",
        )
        (tmp / "b.yaml").write_text(  # 规则 B
            yaml.safe_dump([{"id": "b", "priority": 1, "exact": True, "match": {"any": ["冲突"]}, "action": {"route": "b"}}], allow_unicode=True),
            encoding="utf-8",
        )
        conflict_rules = load_rules([tmp])  # 加载
        assert match("冲突", conflict_rules) is None  # 冲突 → 未定
    finally:  # 清理
        for item in tmp.glob("*.yaml"):  # 逐文件
            item.unlink()  # 删
        tmp.rmdir()  # 删目录
    del rules  # 未用


def test_non_exact_rule_falls_through() -> None:  # 非 exact 不拦截
    tmp = Path("__tmp_rules2__")  # 临时
    try:  # 写非 exact 规则
        tmp.mkdir(exist_ok=True)  # 建
        (tmp / "c.yaml").write_text(  # 文件
            yaml.safe_dump([{"id": "c", "exact": False, "match": {"any": ["模糊"]}, "action": {"route": "c"}}], allow_unicode=True),
            encoding="utf-8",
        )
        assert match("模糊", load_rules([tmp])) is None  # 降级
    finally:  # 清理
        for item in tmp.glob("*.yaml"):  # 文件
            item.unlink()  # 删
        tmp.rmdir()  # 目录


def test_duplicate_rule_id_raises() -> None:  # 重复 id 报错
    tmp = Path("__tmp_rules3__")  # 临时
    try:  # 两条同 id
        tmp.mkdir(exist_ok=True)  # 建
        for name in ("x.yaml", "y.yaml"):  # 两个文件
            (tmp / name).write_text(  # 写
                yaml.safe_dump([{"id": "same", "exact": True, "match": {"any": ["x"]}, "action": {"route": "r"}}], allow_unicode=True),
                encoding="utf-8",
            )
        with pytest.raises(ValueError):  # 期望抛
            load_rules([tmp])  # 加载
    finally:  # 清理
        for item in tmp.glob("*.yaml"):  # 文件
            item.unlink()  # 删
        tmp.rmdir()  # 目录