"""资料导入：文件 → 可检索片段。

**本包只暴露下面这些入口**（导入 / 重建 / 删除）；内部模块见分工表：

| 模块 | 职责 |
| --- | --- |
| ``importer`` | 导入主流程（哈希 → 判重 → 落副本 → 版本链 → 揭示）与删除资料 |
| ``rebuilder`` | 用副本重建（重新解析 + 切分）与只重算词法 |
| ``streaming`` | 四阶段流式：解析 → 结构 → 切分 → 写入（含嵌入） |
| ``progress`` | 进度回调类型 |
| ``code_extract`` | 零 LLM 的精确字段抽取（编号 / 日期 / 金额 / 条款号） |
| （动态字段） | 实现在 ``app/core/attrs/``（领域无关），本包不再保留旧 import 路径 |
| ``version_switch`` | 版本切换 / 回滚 |

切分引擎、语义兜底、存储分别在 **feature 级**的 ``../splitter/``、``../semantic/``、``../store/``
（本包只负责编排）。
"""

from app.rag.ingest.importer import (
    delete_documents,
    import_bytes,
    import_file,
    import_path,
)
from app.rag.ingest.rebuilder import rebuild_document, rebuild_lexical
from app.rag.ingest.streaming import ImportResult

__all__ = [
    "ImportResult",
    "delete_documents",
    "import_bytes",
    "import_file",
    "import_path",
    "rebuild_document",
    "rebuild_lexical",
]
