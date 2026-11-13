"""PDF 引用逐行高亮回归：``line_coords`` 必须随文字块一起产出。

背景：``pdf.iter_units`` 在补页号时会**重建**文字块，重建时漏搬 ``line_coords``（逐行 bbox），
导致所有 PDF 段落块的 ``line_coords`` 恒为空、``chunk/loc._coords_span`` 只能退回块级
``coords``——引用高亮永远框整段而不是命中行。这里用"每行 bbox 与文本行数对齐"这条不变量
锁住重建不丢字段。
"""

from __future__ import annotations  # 延迟值注解

import fitz  # 造 PDF（硬依赖）

from app.core.parse import pdf  # 被测
from app.core.parse.model import TYPE_TEXT  # 文字块类型


def test_text_block_carries_line_coords(tmp_path) -> None:  # 不变量
    """每个有文本的文字块，line_coords 行数必须等于 text 行数（loc 高亮只框命中行的前提）。"""
    path = tmp_path / "a.pdf"  # 目标文件
    doc = fitz.open()  # 新建文档
    page = doc.new_page()  # 一页
    page.insert_text((72, 72), "Hello first line")  # 第一行
    page.insert_text((72, 92), "Hello second line")  # 第二行
    doc.save(str(path))  # 存盘
    doc.close()  # 关

    units = list(pdf.iter_units(path))  # 解析
    text_blocks = [b for u in units for b in u.blocks if b.content_type == TYPE_TEXT]  # 文字块
    assert text_blocks, "应至少产出一个文字块"  # 非空（避免 vacuous pass）
    for block in text_blocks:  # 逐个
        if not block.text:  # 空文本跳过
            continue  # 继续
        assert len(block.line_coords) == len(block.text.splitlines()), (  # 行对齐
            "line_coords 与 text 行数不符：引用高亮会退回整块",
            repr(block.text),  # 文本
            len(block.line_coords),  # 实际 coord 数
        )  # 断言结束
