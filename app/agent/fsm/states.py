"""Run 生命周期状态与事件（纯枚举，不依赖图/DB/UI）。"""

from __future__ import annotations  # 类型注解延迟求值

from enum import StrEnum  # 字符串枚举


class RunState(StrEnum):  # 一轮运行的生命周期
    """粗粒度状态：给 UI、trace、取消、超时共用。"""

    CREATED = "created"  # 已创建
    CONTEXT_LOADING = "context_loading"  # 装载历史/上下文
    RETRIEVING = "retrieving"  # 检索 + 精排 + 证据
    GENERATING = "generating"  # 模型生成
    FINALIZING = "finalizing"  # 落库/收尾
    WAITING = "waiting"  # 停在 interrupt
    CANCELLING = "cancelling"  # 已请求取消
    DONE = "done"  # 正常完成
    CANCELLED = "cancelled"  # 用户取消
    FAILED = "failed"  # 失败
    INTERRUPTED = "interrupted"  # 进程中断收敛


class RunEvent(StrEnum):  # 驱动状态迁移的事件
    """只表达发生了什么，不携带业务数据。"""

    START = "start"  # 开始
    CONTEXT_LOADED = "context_loaded"  # 上下文就绪
    RETRIEVAL_DONE = "retrieval_done"  # 检索完成且有证据
    NO_EVIDENCE = "no_evidence"  # 检索完成但无证据
    GENERATION_DONE = "generation_done"  # 生成完成
    FINALIZED = "finalized"  # 收尾完成
    INTERRUPT = "interrupt"  # 暂停等待用户（澄清/审批）
    CANCEL_REQUEST = "cancel_request"  # 请求取消
    CANCEL_ACK = "cancel_ack"  # 上游已停
    FAIL = "fail"  # 失败
    RESUME = "resume"  # 补参恢复
    RECONCILE = "reconcile"  # 启动收敛


class StageState(StrEnum):  # 单个节点/步骤的状态
    """节点状态由 with_stage 维护，不进 RunMachine。"""

    PENDING = "pending"  # 未开始
    RUNNING = "running"  # 执行中
    DONE = "done"  # 完成
    SKIPPED = "skipped"  # 跳过
    FAILED = "failed"  # 失败


TERMINAL_STATES = frozenset(  # 终态集合
    {  # 集合开始
        RunState.DONE,  # 正常完成
        RunState.CANCELLED,  # 已取消
        RunState.FAILED,  # 失败
        RunState.INTERRUPTED,  # 中断
    }  # 集合结束
)  # frozenset 结束
