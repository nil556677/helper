"""定位映射：块内字符区间 → 行号区间 / bbox 区间。

## 在流水线中的位置

解析（P1.1）给出块的 ``text`` 与 ``line_from`` / ``line_coords``；
**切分（P1.3，本包）** 把片段切成块内区间后，用本模块把区间还原成"源文件行号"
和"只覆盖这几行的 bbox"；入库（P1.4）再把结果写进 ``chunks.loc_json``。

## 输入 / 输出

- 输入：``Block`` + 块内字符区间 ``[start, end)``。
- 输出：``(line_from, line_to)`` 或 ``(x0, y0, x1, y1)``；块没有行号/坐标时返回
  ``(None, None)`` / ``block.coords``（沿用块级定位，**不猜**）。

## 关键约定

1. **按换行数映射行号**：依赖 P1.1 的保证——块文本的行与源行一一对应；
2. 行号是**闭区间**（首行、末行都包含），与 ``Block.line_from/line_to`` 同口径；
3. bbox 用 ``line_coords`` **只框片段覆盖的行**，避免高亮把整块都框住；
4. 正常路径不抛异常；区间异常（空/负）时行号按空区间兜底、bbox 退回块级定位。
"""

from __future__ import annotations

from app.core.parse.model import Block


def _line_span(block: Block, start: int, end: int) -> tuple[int | None, int | None]:
    """块内字符区间 → **源文件/页内行号**区间。

    参数：
        block: 带 ``line_from`` 的块（``line_from`` 是块首行的源行号）。
        start / end: 块内字符区间（左闭右开）。
    返回：``(首行, 末行)``（闭区间）；块没有行号时返回 ``(None, None)``。
    说明：块文本的行与源行一一对应（P1.1 保证：行号来自源），
        所以按换行符数一下就能映射，不需要额外索引。
    """
    if block.line_from is None:
        return None, None
    first = block.text[:start].count("\n")
    # end 是开区间：end-1 才是区间最后一个字符。用 max(start, end-1) 兜住空区间，
    # 否则会取到 text[:-1]、把"区间之前的所有行"都算进来
    last = block.text[: max(start, end - 1)].count("\n")
    return block.line_from + first, block.line_from + last


def _coords_span(block: Block, start: int, end: int) -> tuple[float, float, float, float] | None:
    """块内字符区间 → bbox：用 ``line_coords`` 只框住**这段覆盖的那几行**。

    参数：
        block: 带 ``line_coords``（逐行 bbox）与块级 ``coords`` 的块。
        start / end: 块内字符区间（左闭右开）。
    返回：``(x0, y0, x1, y1)``；没有逐行坐标或切出的行为空时退回 ``block.coords``。
    """
    if not block.line_coords:
        # txt/docx 等没有逐行坐标：整块一个框，沿用块级定位
        return block.coords
    first = block.text[:start].count("\n")
    last = block.text[: max(start, end - 1)].count("\n")
    rows = block.line_coords[first : last + 1]
    if not rows:
        return block.coords
    # 并集 = 框住这段涉及的每一行；不是取"首行左上 + 末行右下"的粗略框
    return (
        min(row[0] for row in rows),
        min(row[1] for row in rows),
        max(row[2] for row in rows),
        max(row[3] for row in rows),
    )
