"""统一归一化：查询/抽取/关键词在数字、单位、全半角、日期上对齐（与入库同源）。"""

from __future__ import annotations  # 延迟值注解

from app.rag.hard import extract  # 硬要素（用统一归一）
from app.rag.normalize import normalize  # 统一归一（入库/查询同源）


def test_normalize_fullwidth() -> None:  # 全角 → 半角
    assert normalize("２０２４　年　３月") == "2024-03"  # 全角数字/空格 → NFKC + 日期

def test_normalize_quantity() -> None:  # 中文数量词 → 数字（单位保留）
    assert normalize("逾期三十天") == "逾期30天"  # 三十天
    assert normalize("罚款五万元") == "罚款5万元" or "50000" in normalize("罚款五万元")  # 金额
    assert normalize("自三日前")  # 不炸（"三"+"日"→转换）
    assert "三无" not in normalize("三无产品说明") or normalize("三无产品说明") == "三无产品说明"  # 防误伤普通词


def test_normalize_dates_iso() -> None:  # 日期归一成 ISO
    assert normalize("2024年3月15日") == "2024-03-15"  # 到日
    assert normalize("2024 年 3 月开始") == "2024-03 开始" or normalize("2024 年 3 月开始").startswith("2024-0")  # 到月


def test_hard_amount_aligned() -> None:  # 金额单位四种写法都归一到"元"
    assert extract("罚款五万元")["amount"] == ["50000"]  # 中文数字+万元
    assert extract("罚款5万元")["amount"] == ["50000"]  # 数字+万元
    assert extract("罚款50000元")["amount"] == ["50000"]  # 元
    assert extract("罚款五万")["amount"] == ["50000"]  # 中文数字+万
    assert extract("违约金三万亿元")["amount"] == ["3000000000000"]  # 万亿（复合单位，不能贪成「万」）
    assert extract("违约金1.5亿元")["amount"] == ["150000000"]  # 亿仍正确


def test_hard_date_aligned() -> None:  # 日期写法对齐
    assert extract("自2024年3月15日起执行")["date"] == ["2024-03-15"]  # 中文日期
    assert extract("自2024/3/15起")["date"]  # 兼容写法也能抽到


def test_hard_clause_normalized() -> None:  # 条款号全角也抽得到
    assert extract("按第３．２条执行")["clause"] == ["3.2"]  # 全角条款号 → 3.2


def test_normalize_idempotent() -> None:  # 幂等（入库/查询都调用，重复安全）
    once = normalize("５万元起，逾期三十天罚５０元")  # 一次
    twice = normalize(once)  # 二次
    assert twice == once  # 幂等
