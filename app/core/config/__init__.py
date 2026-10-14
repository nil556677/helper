"""配置分区：引导配置的形状与唯一读写入口。

技术方案 §12 把配置分成两处：

- **引导项**在 ``config.json``（本包）——只有"打开数据库之前就必须知道"的东西；
- **其余全部**在 SQLite（``llm_slots`` / ``settings`` 两张表，见 core/db/schema.py），由界面来改。

对外只用这几个函数，别绕过 access 直接读 config.json。
"""

from app.core.config.access import (
    config_file,
    data_dir_override,
    ensure_default,
    invalidate,
    isolate_bad_file,
    load,
    load_fresh,
    save,
)
from app.core.config.schema import BootstrapConfig

__all__ = [
    "BootstrapConfig",
    "config_file",
    "data_dir_override",
    "ensure_default",
    "invalidate",
    "isolate_bad_file",
    "load",
    "load_fresh",
    "save",
]
