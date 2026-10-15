"""本地模型分区：模型文件的完整性校验、加载路径。

**为什么先做文件校验而不是加载器**：模型在 P2 才真正用于嵌入/精排，但它现在就在
``models/`` 里了（566 MB + 570 MB），一旦被截断/下错，到使用时才发现就晚了。
这里先把"文件齐全、大小、哈希"管起来，加载器落地时在首次加载前调 :func:`require`。
"""

from app.core.models.files import (
    MANIFEST,
    ModelFile,
    ModelFileError,
    path_of,
    problems,
    require,
    update_manifest_for,
)

__all__ = [
    "MANIFEST",
    "ModelFile",
    "ModelFileError",
    "path_of",
    "problems",
    "require",
    "update_manifest_for",
]