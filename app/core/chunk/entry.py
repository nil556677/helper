"""便捷入口 + 旧接口（接线完成后删除）。

## 在流水线中的位置

解析（P1.1）→ 结构识别（P1.2）→ **切分（P1.3，本包）** → 入库（P1.4）。
``Splitter`` 是流式接口（大文件用）；本模块把它包成"整份文档一次切完"的便捷函数，
供测试/预览/小文件使用。

## 输入 / 输出

- ``split_blocks``：输入按文档顺序排好的 ``Block`` 列表，
  输出 ``ChunkGroup`` 列表（顺序 = 文档顺序）。
- ``to_json``：输入任意可 JSON 化对象，输出不转义中文的 JSON 字符串（写库用）。

## 关键约定

1. **必须调用 ``flush()``**：最后一个 parent 只有在收尾时才封口；
2. **别把本入口用在大文件上**：它把整份文档的 groups 都攒在内存里，
   大文件应直接用 ``Splitter.feed/flush`` 的流式接口；
3. ``to_json`` 不转义非 ASCII（中文直接落盘/入库，便于排查）。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.parse.model import Block

from .core import Splitter
from .records import ChunkGroup


def split_blocks(blocks: list[Block], *, splitter: Splitter | None = None) -> list[ChunkGroup]:
    """整份切分（小文件/测试/预览用；大文件走 :class:`Splitter` 流式）。

    参数：
        blocks: 按文档顺序排好的结构块（``feed`` 依赖顺序开/闭 parent）。
        splitter: 可注入已配置的切分器（测试用）；不传则按当前模型能力构造。
    返回：全部 ``ChunkGroup``，顺序与文档一致（已封口的在前，收尾封口的在后）。
    说明：必须 ``feed`` 完再 ``flush``——最后一个 parent 不会在 ``feed`` 里封口；
        返回后 parent 已全部封口（``sink`` 仍保留本次诊断，不建议复用实例切第二份文档）。
    """
    engine = splitter or Splitter.from_config()
    groups = engine.feed(blocks)
    return groups + engine.flush()


def to_json(payload: Any) -> str:
    """写库用的 JSON 文本（``loc_json`` / ``section_path`` 共用）。

    参数：
        payload: 任意可 JSON 化对象（dict/list/tuple...）。
    返回：JSON 字符串；``ensure_ascii=False`` 让中文原样保留（体积更小、日志可读）。
    异常：不可 JSON 化的对象会抛 ``TypeError``——这是调用方的编程错误，不在这里吞掉。
    """
    return json.dumps(payload, ensure_ascii=False)
