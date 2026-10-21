"""切分尺寸的**唯一推导处**：全部由嵌入模型能力推导，不再是用户配置项。

为什么不再配置：这些数字不是"用户偏好"，而是由**向量模型**决定的——
子块必须塞得进模型的固定输入宽度（``app/core/embed/model.py:PAD_TO``，随模型确定），
其余尺寸都是围绕它的固定比例。让用户手填只会出现"``child_size > pad_to`` →
尾部被静默截断"这类静默错误，而且默认值写两处必然漂移。

## 在流水线中的位置

解析 → 结构识别 → **切分（本包）** → 入库。切分器构造时调用 :func:`derive`，
把模型常量 ``PAD_TO`` 变成一套尺寸；下游 ``core.Splitter`` 只消费 :class:`ChunkSizes`。

## 输入 / 输出

- 输入：``pad_to``（嵌入模型的固定输入宽度，单位 token）。
- 输出：:class:`ChunkSizes`（五个自洽的 token 尺寸）。

## 关键约定

1. **唯一出处**：别在别处再写默认尺寸，否则两处必然漂移；
2. **子块必须 ≤ pad_to**：``child_size = pad_to − 安全余量``，余量给章节标题前缀留位；
3. 比例（overlap / children-per-parent / min-child）都是围绕 ``child_size`` 的固定倍数，
   改比例等于改变"证据块装几个检索单位"与"边界重复多少"的行为。

推导链（全部由模型的 ``PAD_TO`` 一个数得出）::

    child_size     = PAD_TO − SAFETY_MARGIN_TOKENS   # 子块必须塞得进嵌入宽度
    child_overlap  = child_size × 15%
    min_child_size = child_size × 40%
    parent_size    = child_size × 5                  # 一个证据块装 5 个检索单位
    parent_overlap = parent_size × 10%

以模型默认 ``PAD_TO=320`` 为例：child=300、overlap=45、min=120、parent=1500、parent_overlap=150。
"""

from __future__ import annotations

from dataclasses import dataclass

from ._shared import _MIN_CHILD_RATIO

#: 子块相对嵌入宽度的安全余量（token）：给章节标题前缀留位置，避免贴边。
SAFETY_MARGIN_TOKENS = 20

#: 子块重叠占 ``child_size`` 的比例（边界句两边都在，防"答案正好在缝里"）。
CHILD_OVERLAP_RATIO = 0.15

#: 一个 parent 装几个 child（证据块 = 若干个检索单位）。
CHILDREN_PER_PARENT = 5

#: parent 重叠占 ``parent_size`` 的比例。
PARENT_OVERLAP_RATIO = 0.10


@dataclass(frozen=True)
class ChunkSizes:
    """一套推导出来的切分尺寸（单位都是 bge-m3 token）。

    字段：
        parent_size: parent（送给模型的证据块）token 上限；
        parent_overlap: 超长节再分 parent 时的整句重叠；
        child_size: child（检索的最小单位）token 上限，保证 ≤ ``PAD_TO``；
        child_overlap: 相邻 child 之间的整句重叠；
        min_child_size: child 的软下限（只用于"同一块内合并短尾块"）。

    各字段的推导关系见模块 docstring；本类只是**结果容器**，不做校验/换算。
    """

    parent_size: int
    parent_overlap: int
    child_size: int
    child_overlap: int
    min_child_size: int


def derive(pad_to: int) -> ChunkSizes:
    """由嵌入宽度推导全部切分尺寸。

    参数：
        pad_to: 嵌入的固定输入宽度（token）；``≤ 0`` 不抛异常，内部按 1 处理。
    返回：一套自洽的 :class:`ChunkSizes`（子块一定 ≤ ``pad_to``）。
    """
    pad = max(1, int(pad_to))
    # 至少留 1 个 token，避免 pad_to 极小时子块为 0
    child = max(1, pad - SAFETY_MARGIN_TOKENS)
    parent = child * CHILDREN_PER_PARENT
    return ChunkSizes(
        # 证据块 = 5 个检索单位；超长节再分时的重叠取 10%
        parent_size=parent,
        parent_overlap=round(parent * PARENT_OVERLAP_RATIO),
        # 15% 重叠：边界句两边都在，防"答案正好在缝里"
        child_size=child,
        child_overlap=round(child * CHILD_OVERLAP_RATIO),
        # 软下限 40%：太短的尾块在同一块内并回前一块（不跨块）
        min_child_size=max(1, round(child * _MIN_CHILD_RATIO)),
    )
