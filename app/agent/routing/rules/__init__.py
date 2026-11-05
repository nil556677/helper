"""规则目录：`rules/` 放匹配规则，`routes/` 放固定路径定义（均由 loader 扫描）。"""

from __future__ import annotations  # 延迟求值注解

__all__: list[str] = []  # 由 loader 导出