"""ONNX 模型的**单例生命周期**（embed 与 rerank 共用，避免各写一份）。

原来两个模型文件各有一份几乎一样的 `_State`/`is_loaded`/`set_load_hook`/`unload`/
`_ensure_loaded`（加锁、加载中标志、加载钩子、惰性加载）。抽到这里后：

- 模型各自的实现只需给一个 `_build()`，把"路径/选项/分词器/额外字段"填完；
- 那套"进程内单例 + 惰性 + 线程安全 + 加载钩子"是公共的，只写一次。

**约定**：
- `ensure()` 在锁内调用一次 `_build()`；随后复用。`_build()` 失败会往上抛（域异常，
  如 `EmbedError`/`RerankError`），`ensure()` 不吞、直接透传。
- `unload()` 只给测试用（生产路径不卸载，两个模型都这样定）。
- 加载钩子先于 `_build()` 触发且只触发一次；钩子自己抛异常不会拖垮加载。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from loguru import logger


class LoadedModel:
    """一次加载得到的（session + tokenizer）。

    子类（模型的 `_build`）可以在它上面额外挂字段（比如精排的 ``separator_id``）。
    """

    def __init__(self, session: object, tokenizer: object) -> None:
        self.session = session
        self.tokenizer = tokenizer


class ModelSlot:
    """进程内单例 + 惰性加载 + 线程安全 + 加载钩子（模型共用的那套生命周期）。

    用法：子类实现 :meth:`_build`（真正加载，见模块说明）；然后：
        ``slot.ensure()`` 取（必要时加载）``LoadedModel``；
        ``slot.is_loaded`` / ``set_load_hook`` / ``unload`` 暴露给界面与测试。
    """

    def __init__(self) -> None:
        self._value: LoadedModel | None = None
        self._lock = threading.RLock()
        self._loading = False
        self._on_load: Callable[[], None] | None = None

    @property
    def is_loaded(self) -> bool:
        """是否已加载（界面据此决定要不要显示「正在加载模型…」）。"""
        return self._value is not None

    def set_load_hook(self, hook: Callable[[], None] | None) -> None:
        """注册"即将开始加载"的钩子（加载在锁内、只调一次；传 None 取消）。"""
        self._on_load = hook

    def unload(self) -> None:
        """卸掉模型（**只给测试用**，见模块说明）。"""
        with self._lock:
            self._value = None

    def ensure(self) -> LoadedModel:
        """取（必要时加载）单例。加载只发生一次；失败抛域异常。"""
        with self._lock:
            if self._value is not None:
                return self._value
            if not self._loading:
                self._loading = True
                try:
                    if self._on_load is not None:
                        # 钩子先于加载触发：界面据此立刻显示「正在加载模型…」
                        try:
                            self._on_load()
                        except Exception as exc:
                            logger.warning("模型加载钩子抛异常（已忽略）：{}", exc)
                    self._value = self._build()
                finally:
                    self._loading = False
            if self._value is None:  # pragma: no cover - 只有 _build 返回 None（不该发生）才会
                raise RuntimeError("模型没能加载起来")
            return self._value

    def _build(self) -> LoadedModel:
        """实际加载（在锁内调用一次）。子类实现，可抛域异常。"""
        raise NotImplementedError
