"""动态字段（领域无关）：提取 / 归一化 / 校验 / 编译，是所有模块共用的一套能力。

用法：

```python
from app.core.attrs import build_attrs, enrich, FIELD_DICT, ATTRS_VERSION

build_attrs("人民币壹佰万元")           # {"attrs_num": [{"key":"amount","value":1000000.0,...}]}
enrich(payload, text)                   # 合并进 payload 并打 attrs_version
```

**不在这里做的事**：不写库（写库在 ``app.core.vector.writer``）、不 import 界面/服务层。
"""

from __future__ import annotations

from app.core.attrs.api import apply_attrs, build_attrs, enrich, extract
from app.core.attrs.compiler import facet_spec, filter_spec, json_schema
from app.core.attrs.field_dict import FIELD_DICT, AttrSpec, field_dict_err
from app.core.attrs.version import ATTRS_VERSION

__all__ = [
    "ATTRS_VERSION",
    "FIELD_DICT",
    "AttrSpec",
    "apply_attrs",
    "build_attrs",
    "enrich",
    "extract",
    "facet_spec",
    "field_dict_err",
    "filter_spec",
    "json_schema",
]
