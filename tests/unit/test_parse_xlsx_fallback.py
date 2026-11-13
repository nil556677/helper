"""Excel 解析回退的回归测试：可选后端 ExStruct 失败时确实回退 openpyxl。

背景：旧 ``xlsx.iter_units`` 在 ``return xlsx_exstruct.iter_units(path)`` 处包 try——
生成器函数体要到首次 ``next()`` 才执行，try 只包住"构造生成器"那一瞬（必成功），
真正的解析异常穿透、回退从不生效。这里锁住"首块失败一定回退主路径"这条契约。
"""

from __future__ import annotations  # 延迟值注解

import openpyxl  # 造测试表格
import pytest  # 框架

from app.core.parse import (
    xlsx,  # 被测主模块
    xlsx_exstruct,  # 可选后端
)
from app.core.parse.model import TYPE_TABLE, Block, Unit  # 块模型


@pytest.fixture
def sample_xlsx(tmp_path):  # 造一个真实表格文件
    path = tmp_path / "a.xlsx"  # 文件名
    wb = openpyxl.Workbook()  # 工作簿
    ws = wb.active  # 活动表
    ws.title = "数据"  # 表名
    ws.append(["名称", "数量"])  # 表头
    ws.append(["苹果", 3])  # 数据行
    wb.save(path)  # 存盘
    return path  # 返回路径


def test_exstruct_failure_falls_back_to_openpyxl(sample_xlsx, monkeypatch) -> None:  # 回退
    """ExStruct 首块就炸 → 整体回退 openpyxl，仍能产出表格内容。"""
    monkeypatch.setattr(xlsx_exstruct, "available", lambda: True)  # 强制启用可选后端

    def _broken(path):  # 一个首块即抛错的假生成器
        raise ValueError("模拟 ExStruct 解析失败")  # 抛
        yield  # pragma: no cover（不可达）

    monkeypatch.setattr(xlsx_exstruct, "iter_units", _broken)  # 注入坏后端
    units = list(xlsx.iter_units(sample_xlsx))  # 收集
    texts = [b.text for u in units for b in u.blocks]  # 全部块文本
    assert any("苹果" in t for t in texts)  # openpyxl 回退产出了数据（回退生效）


def test_exstruct_success_is_used(sample_xlsx, monkeypatch) -> None:  # 成功走可选后端
    """ExStruct 能出首块 → 用它，不复用 openpyxl（成功路径不被破坏）。"""
    monkeypatch.setattr(xlsx_exstruct, "available", lambda: True)  # 启用

    def _fake(path):  # 一个成功产出一块的假后端
        yield Unit(blocks=[Block(text="EXSTRUCT", content_type=TYPE_TABLE)])  # 标记块

    monkeypatch.setattr(xlsx_exstruct, "iter_units", _fake)  # 注入
    units = list(xlsx.iter_units(sample_xlsx))  # 收集
    assert units[0].blocks[0].text == "EXSTRUCT"  # 用了 ExStruct（而非 openpyxl）


def test_backend_disabled_uses_openpyxl(sample_xlsx, monkeypatch) -> None:  # 默认路径
    """可选后端未启用（默认）→ 走 openpyxl 主路径。"""
    monkeypatch.setattr(xlsx_exstruct, "available", lambda: False)  # 关掉
    units = list(xlsx.iter_units(sample_xlsx))  # 收集
    texts = [b.text for u in units for b in u.blocks]  # 块文本
    assert any("苹果" in t for t in texts)  # openpyxl 产出数据
