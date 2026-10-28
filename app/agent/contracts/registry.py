"""图/节点/步骤 id 注册表：构图期强制唯一，重复直接抛。"""

from __future__ import annotations  # 类型注解延迟求值


class DuplicateIdError(ValueError):  # 重复 id 异常
    """两个节点/步骤用了同一个 id。"""


class IdRegistry:  # id 注册表
    """收集所有 node_id/stage_id，重复即失败（构图期/首次跑到就报，不留到运行深处）。"""

    def __init__(self) -> None:  # 构造
        self._ids: dict[str, str] = {}  # id -> 来源描述

    def register(self, id_: str, where: str = "") -> None:  # 注册一个 id
        """重复直接抛 DuplicateIdError。"""
        if id_ in self._ids:  # 已存在
            raise DuplicateIdError(  # 抛异常
                f"重复的 id：{id_!r}（已有 {self._ids[id_]!r}，又来 {where!r}）"  # 信息
            )  # 异常结束
        self._ids[id_] = where or "unknown"  # 记录来源

    def claim(self, id_: str, where: str = "") -> None:  # 登记一个 id（同一处可重复）
        """同一个调用位置每轮都会登记一次：来源相同就放过，两处撞车才抛。

        为什么不用 :meth:`register`：步骤 id 是**每次执行**现算的，正常一轮里同一处
        会被调到很多次；判据是「id 相同但来源位置不同」，那才是真重复。
        """
        owner = self._ids.get(id_)  # 已经占了这个 id 的来源
        if owner is None:  # 第一次见
            self._ids[id_] = where or "unknown"  # 记下来源
            return  # 结束
        if owner == (where or "unknown"):  # 同一处再次执行
            return  # 幂等放过
        raise DuplicateIdError(  # 两个不同位置用了同一个 id
            f"重复的 id：{id_!r}（已有 {owner!r}，又来 {where!r}）"  # 信息
        )  # 异常结束

    def source(self, id_: str) -> str:  # 查这个 id 从哪儿来
        """没注册过返回空串。"""
        return self._ids.get(id_, "")  # 来源描述

    def contains(self, id_: str) -> bool:  # 是否已注册
        """查询用。"""
        return id_ in self._ids  # 判断

    def ids(self) -> list[str]:  # 已注册 id 快照
        """返回拷贝，外部改不动内部。"""
        return list(self._ids)  # 键列表
