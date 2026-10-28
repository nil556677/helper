"""M3 查询改写：归一化、触发、校验（硬要素/新增数字/解释性输出）、降级与缓存。"""

from __future__ import annotations  # 类型注解延迟求值

import pytest  # 测试框架

from app.rag import normalize as normalize_mod  # 归一化
from app.rag import rewrite as rewrite_mod  # 改写


@pytest.fixture(autouse=True)
def _clear_cache() -> None:  # 每个用例清缓存
    """缓存是模块级的，不清会串。"""
    rewrite_mod.clear_cache()  # 清


class _Reply:  # 假回复
    """只带 content。"""

    def __init__(self, content: str) -> None:  # 构造
        self.content = content  # 文本


class _Model:  # 假模型
    """记录调用次数；可抛错。"""

    model_name = "fake-llm"  # 模型名（进缓存键）

    def __init__(self, content: str = "[]", error: BaseException | None = None) -> None:  # 构造
        self.content = content  # 回复
        self.error = error  # 待抛异常
        self.calls = 0  # 调用次数

    def invoke(self, messages):  # 调用
        """返回假回复或抛。"""
        self.calls += 1  # 计数
        if self.error is not None:  # 要抛
            raise self.error  # 抛
        return _Reply(self.content)  # 回复


def _factory(model: _Model):  # 工厂
    """固定返回同一个模型。"""
    def build():  # 签名
        return model  # 返回
    return build  # 返回函数


# ---------------- 归一化 ----------------

def test_normalize_fullwidth_and_space() -> None:  # 正
    """全角字母/标点与多余空白被统一。"""
    assert normalize_mod.normalize("ＡＢＣ　合同") == "ABC 合同"  # NFKC + 空白


def test_normalize_date() -> None:  # 正
    """中文日期归一成 2024-03-15。"""
    assert "2024-03-15" in normalize_mod.normalize("2024 年 3 月 15 日签订")  # 归一


def test_normalize_chinese_number() -> None:  # 正
    """中文数字转阿拉伯。"""
    assert "30000" in normalize_mod.normalize("叁万元")  # 转换


# ---------------- 触发 ----------------

def test_trigger_multi_turn() -> None:  # 正
    """多轮一定触发（要用上一轮补主语）。"""
    assert rewrite_mod.trigger("它的违约金呢", [{"role": "user", "content": "合同"}]) == "multi_turn"  # 触发


def test_trigger_pronoun_without_history() -> None:  # 正
    """有指代词也触发。"""
    assert rewrite_mod.trigger("这个怎么算", []) == "pronoun"  # 触发


def test_trigger_none_for_specific_question() -> None:  # 反
    """首轮、有实体、无疑问 → 不改写（省一次模型）。"""
    assert rewrite_mod.trigger("第 3.2 条的违约金是多少", []) == ""  # 不触发


def test_no_trigger_does_not_call_model() -> None:  # R-05 反
    """不触发时一次模型都不该调。"""
    model = _Model()  # 假模型
    result = rewrite_mod.rewrite("第 3.2 条的违约金是多少", model_factory=_factory(model))  # 改写
    assert result.queries == ["第 3.2 条的违约金是多少"]  # 只有原句
    assert result.reason == "skipped"  # 跳过
    assert model.calls == 0  # 没调用


# ---------------- 校验 ----------------

def test_rewrite_keeps_original_first() -> None:  # R-01 正
    """第 1 条恒为原问题，变体在后。"""
    model = _Model('["它的违约金呢", "XX 采购合同 违约金"]')  # 模型输出
    result = rewrite_mod.rewrite("它的违约金呢", history=[{"role": "user", "content": "XX 采购合同"}], model_factory=_factory(model))  # 改写
    assert result.queries[0] == "它的违约金呢"  # 锚
    assert "XX 采购合同 违约金" in result.queries  # 变体
    assert result.reason == "used"  # 用了


def test_rewrite_drops_variant_missing_hard_element() -> None:  # R-07 反
    """变体把 `3.2` 改丢 → 丢弃该条，只留原句。"""
    model = _Model('["第 3.2 条怎么写", "违约金怎么写"]')  # 变体丢了 3.2
    result = rewrite_mod.rewrite("第 3.2 条怎么写", history=[{"role": "user", "content": "合同"}], model_factory=_factory(model))  # 改写
    assert result.queries == ["第 3.2 条怎么写"]  # 只剩原句
    assert not result.changed  # 没变体


def test_rewrite_drops_variant_with_new_number() -> None:  # R-08 反
    """变体凭空多出数字 → 丢弃（防瞎编金额）。"""
    model = _Model('["违约金 30 万", "违约金 50 万"]')  # 多出 50
    result = rewrite_mod.rewrite("违约金 30 万怎么算", history=[{"role": "user", "content": "合同"}], model_factory=_factory(model))  # 改写
    assert all("50" not in query for query in result.queries)  # 没 50


def test_rewrite_rejects_explanatory_output() -> None:  # R-09 反
    """模型输出解释/拒答 → 整批丢弃。"""
    model = _Model("抱歉，我无法完成这个任务。")  # 不是 JSON
    result = rewrite_mod.rewrite("它的违约金呢", history=[{"role": "user", "content": "合同"}], model_factory=_factory(model))  # 改写
    assert result.queries == ["它的违约金呢"]  # 回退
    assert result.reason == "rejected"  # 原因


def test_rewrite_caps_to_three() -> None:  # R-10 反
    """最多 3 条（含原句）。"""
    model = _Model('["这个问题怎么处理", "甲", "乙", "丙", "丁"]')  # 5 条
    result = rewrite_mod.rewrite("这个问题怎么处理", history=[], model_factory=_factory(model))  # 改写
    assert len(result.queries) <= 3  # 封顶
    assert result.queries[0] == "这个问题怎么处理"  # 锚


# ---------------- 降级 ----------------

def test_rewrite_timeout_falls_back() -> None:  # R-11 反
    """超时 → 用原句，记原因。"""
    model = _Model(error=TimeoutError("慢"))  # 超时
    result = rewrite_mod.rewrite("它的违约金呢", history=[{"role": "user", "content": "合同"}], model_factory=_factory(model))  # 改写
    assert result.queries == ["它的违约金呢"]  # 原句
    assert result.reason == "timeout"  # 原因


def test_rewrite_failure_falls_back() -> None:  # R-12 反
    """429/5xx 之类 → 用原句。"""
    model = _Model(error=RuntimeError("429"))  # 失败
    result = rewrite_mod.rewrite("它的违约金呢", history=[{"role": "user", "content": "合同"}], model_factory=_factory(model))  # 改写
    assert result.reason == "failed"  # 原因
    assert result.queries == ["它的违约金呢"]  # 原句


def test_rewrite_unconfigured_skips() -> None:  # R-13 反
    """槽位没配 → 跳过，链路正常。"""
    result = rewrite_mod.rewrite("它的违约金呢", history=[{"role": "user", "content": "合同"}], model_factory=lambda: None)  # 工厂返回 None
    assert result.reason == "unconfigured"  # 没配
    assert result.queries == ["它的违约金呢"]  # 原句


def test_rewrite_cache_hit_skips_model() -> None:  # R-14 正
    """同一问题第二次问 → 命中缓存，不再调模型。"""
    model = _Model('["它的违约金呢", "XX 合同 违约金"]')  # 模型
    history = [{"role": "user", "content": "XX 合同"}]  # 历史
    first = rewrite_mod.rewrite("它的违约金呢", history=history, model_factory=_factory(model))  # 第一次
    second = rewrite_mod.rewrite("它的违约金呢", history=history, model_factory=_factory(model))  # 第二次
    assert first.queries == second.queries  # 结果一致
    assert model.calls == 1  # 只调一次
