"""配置分区：引导配置的形状与唯一读写入口，以及**索引期参数与指纹**。

技术方案 §12 把配置分成三处：

- **引导项**也在 SQLite 的 ``settings`` 表（``bootstrap.*``，:mod:`app.core.config.access`）——
  日志级别、调试模式、监听地址；老 ``config.json`` 只做一次性迁移；
- **索引期参数与界面偏好**在 SQLite 的 ``settings`` 表（:mod:`app.core.config.store`），
  **默认值只在** :mod:`app.core.config.params` **写一份**；
- **模型槽位**在 ``llm_slots`` 表（见 core/db/schema.py），由界面来改。

对外只用这几个函数，别绕过 access/store 直接读表。
"""

from app.core.config.access import (
    config_file,
    ensure_default,
    invalidate,
    isolate_bad_file,
    load,
    load_fresh,
    save,
)
from app.core.config.fingerprint import compute as compute_fingerprint
from app.core.config.params import (
    ALL as ALL_PARAMS,
)
from app.core.config.params import (
    GROUPS as PARAM_GROUPS,
)
from app.core.config.params import (
    Aspect,
    Config,
    Param,
)
from app.core.config.schema import BootstrapConfig
from app.core.config.store import (
    clear_fingerprints,
    compute_fingerprints,
    describe_staleness,
    save_values,
    store_fingerprints,
    stored_fingerprints,
)
from app.core.config.store import (
    load as load_params,
)

__all__ = [
    "ALL_PARAMS",
    "PARAM_GROUPS",
    "Aspect",
    "BootstrapConfig",
    "Config",
    "Param",
    "clear_fingerprints",
    "compute_fingerprint",
    "compute_fingerprints",
    "config_file",
    "describe_staleness",
    "ensure_default",
    "invalidate",
    "isolate_bad_file",
    "load",
    "load_fresh",
    "load_params",
    "save",
    "save_values",
    "store_fingerprints",
    "stored_fingerprints",
]
