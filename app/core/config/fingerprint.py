"""索引指纹：**把"用什么参数建的索引"变成两个可比较的字符串**。

为什么是**两个**指纹（而不是一个）：变了的东西不同，要做的事也不同——

- **向量指纹**变了（换嵌入模型 / 改固定宽度 / 改切片尺寸）→ 必须**全量重建**（小时级）；
- **词法指纹**变了（改词库 / 改切法）→ 只需**重算词法列**（秒级：重切 `chunks.tokens`）。

一个指纹会把"改词库"也报成"全量重建 8 小时"，那等于逼用户不做这件事
（技术方案 §9.13 的运维表就是按这个区分写的）。

**指纹里必须含形状参数**（P0.2 的实测结论）：`PAD_TO`（模型固定输入宽度）/ `embed.batch_size`
看着像"性能参数"，其实决定向量值——宽度差 1 个 token 向量就差 1.4%。
漏了它们的后果不是"慢一点"，而是"新块与老块的向量不在同一个空间、分数失去意义"。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from app.core.config.params import (
    PER_ITEM_PREFIX,
)

#: 指纹的算法版本。**改了拼法（比如加一项）就 +1**：这样老库的指纹一定对不上、
#: 会提示重建，而不是"看起来一致、其实口径变了"。
FINGERPRINT_ALGO = 1


def compute(items: Mapping[str, str]) -> str:
    """算一个稳定指纹。

    参数：
        items: ``{键: 值}``（值都是字符串，见 ``Config.fingerprint_items``）。
    返回：``"v1-<sha256 前 16 位>"``。

    为什么要带算法版本前缀：指纹是**跨版本比较**的东西。老程序算出的串与新程序算出的串
    如果格式不同却碰巧相等，索引就会被误判成"没过期"。带版本号能杜绝这种巧合。
    排序后序列化：字典顺序不影响指纹（否则换 Python 版本就可能变）。
    """
    payload = json.dumps(
        {"algo": FINGERPRINT_ALGO, "items": dict(sorted(items.items()))},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"v{FINGERPRINT_ALGO}-{digest[:16]}"


def diff(current_items: Mapping[str, str], stored_items: Mapping[str, str]) -> list[str]:
    """列出**变了哪些项**（给用户看的"到底什么变了"）。

    参数：
        current_items: 当前程序算的逐项值。
        stored_items: 建索引时存下的逐项值。
    返回：变了或新增的键（排序）；值一致的不列。
    """
    changed: list[str] = []
    for key, value in sorted(current_items.items()):
        if stored_items.get(key) != value:
            changed.append(key)
    # 被删掉的参数也要报（老库里有、新程序不认了）
    changed.extend(sorted(key for key in stored_items if key not in current_items))
    return changed


def item_key(name: str) -> str:
    """``index_meta`` 里逐项留痕的键名（前缀 + 参数键）。"""
    return f"{PER_ITEM_PREFIX}{name}"
