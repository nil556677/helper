"""动态字段提取的**版本号**：换了字段字典/提取器/归一化规则就 +1。

与 ``PARSER_VERSION`` / ``CHUNK_POLICY_VERSION`` / ``EMBEDDING_VERSION`` 同构：
- 写进 payload 的 ``attrs_version``；
- 进 ``index_meta`` 指纹，改了就提示"重建动态字段"（只回填 attrs，不重嵌入）。

**递增时机**：改 ``field_dict.py``（加/删/改字段或 slot）、改 ``extractors.py`` 的规则、
改 ``normalize.py`` 的归一化口径。
"""

from __future__ import annotations

#: 当前动态字段提取版本（v1：字段上移到 core/attrs，提取/归一化/校验统一）。
ATTRS_VERSION = 1
