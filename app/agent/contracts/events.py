"""事件协议：每种 type 一个 Pydantic 载荷模型 + 统一信封。

字段名按「见名知意」重定，不沿用旧 `events.py` 的缩写（`ms` / `kind` / `id` 等）。
载荷用**判别联合**：每个载荷自带 ``type``，由 Pydantic 在构造期选模型、拒绝错配。
"""

from __future__ import annotations  # 类型注解延迟求值

import time  # 默认时间戳
from typing import Annotated, Any, Literal  # 任意类型 / 字面量 / 判别联合标注

from pydantic import BaseModel, ConfigDict, Field, model_validator  # 校验模型

from app.agent.contracts.kinds import EventType  # 事件类型


class _Payload(BaseModel):  # 所有载荷的基类
    """统一策略：未知字段直接报错（拼写错误不再被静默吞掉）。"""

    model_config = ConfigDict(extra="forbid")  # 拒绝多余字段


class AckPayload(_Payload):  # 已收到
    """前端立即上屏后就能收到。

    run id 由信封的 ``run_id`` 承载，载荷本身不需要业务字段（只带判别字段）。
    """

    type: Literal[EventType.ACK] = EventType.ACK  # 判别字段


class StatePayload(_Payload):  # run 状态迁移
    """生命周期变化。"""

    type: Literal[EventType.STATE] = EventType.STATE  # 判别字段
    before: str  # 迁移前状态
    after: str  # 迁移后状态
    trigger: str  # 触发迁移的事件
    reason: str = ""  # 人话原因


class StagePayload(_Payload):  # 节点/步骤事件
    """前端按 stage_id 原地更新。"""

    type: Literal[EventType.STAGE] = EventType.STAGE  # 判别字段
    stage_id: str  # 步骤 id（自动派生）
    status: str  # running/done/skipped/failed
    title: str = ""  # 显示标题
    detail: str = ""  # 明细
    duration_ms: int | None = None  # 耗时
    group: str = ""  # 用户级分组（prepare/search/filter/tool/generate；空 = 只埋 trace 不展示）
    agent_id: str = ""  # 归属哪个 Agent（多 Agent 委派；空 = 主链路）
    parent_run_id: str = ""  # 父 run（并行子 Agent 用它组树）
    branch: str = ""  # 并行分支标识（同父多路区分）


class BasisPayload(_Payload):  # 检索依据
    """给用户看的“我是怎么找的”，不是思维链。"""

    type: Literal[EventType.BASIS] = EventType.BASIS  # 判别字段
    keywords: list[str]  # 查询词
    hit_counts: dict[str, int]  # 各路命中计数
    sources: list[dict[str, Any]]  # 来源


class SettingsPayload(_Payload):  # 本轮设置
    """供“查看本轮设置”弹窗。"""

    type: Literal[EventType.SETTINGS] = EventType.SETTINGS  # 判别字段
    model: str  # 模型名
    temperature: float  # 温度
    system_prompt: str  # 系统提示词
    extra: dict[str, Any] = Field(default_factory=dict)  # 额外参数


class ReasoningPayload(_Payload):  # 思考增量
    """模型返回的 reasoning 内容。"""

    type: Literal[EventType.REASONING] = EventType.REASONING  # 判别字段
    text: str  # 思考文本


class ConflictPayload(_Payload):  # 资料冲突
    """资料答案与通用知识不一致时提示用户核对。"""

    type: Literal[EventType.CONFLICT] = EventType.CONFLICT  # 判别字段
    message: str  # 冲突说明


class ApprovalPayload(_Payload):  # 等待审批
    """危险操作执行前询问。"""

    type: Literal[EventType.APPROVAL] = EventType.APPROVAL  # 判别字段
    request_id: str  # 请求 id
    name: str  # 操作名
    risk: str  # 风险说明
    detail: str = ""  # 明细
    diff: str = ""  # 改动预览


class ContextPayload(_Payload):  # 上下文装载/裁剪/压缩
    """只报数量与分块占用，不报正文。"""

    type: Literal[EventType.CONTEXT] = EventType.CONTEXT  # 判别字段
    turns: int  # 轮数
    tokens: int  # token 估算
    trimmed: int = 0  # 被裁剪数量
    window: int = 0  # 生效窗口
    tiers: dict[str, int] = Field(default_factory=dict)  # 大板块占用
    blocks: dict[str, int] = Field(default_factory=dict)  # 小板块占用
    subblocks: dict[str, dict[str, int]] = Field(default_factory=dict)  # 最细一层（V1 按工具、F3 按轮次）
    compacted: int = 0  # 压缩条数
    compacted_saved: int = 0  # 压缩释放 token
    trimmed_saved: int = 0  # 裁剪释放 token
    prompt_versions: dict[str, int] = Field(default_factory=dict)  # 用到的模板版本


class PiiPayload(_Payload):  # 脱敏统计
    """只报类型与次数，不报原值。"""

    type: Literal[EventType.PII] = EventType.PII  # 判别字段
    counts: dict[str, int]  # 类型 -> 次数


class ToolPayload(_Payload):  # 工具调用/结果
    """file 工具带 variant/path/diff。"""

    type: Literal[EventType.TOOL] = EventType.TOOL  # 判别字段
    tool_name: str  # 工具名
    status: str  # 状态
    detail: str = ""  # 明细
    variant: str = ""  # read/write/edit/bash
    path: str = ""  # 文件
    diff: str = ""  # 改动
    output: str = ""  # 输出
    call_id: str = ""  # 调用 id


class DeltaPayload(_Payload):  # 答案增量
    """一个 token/chunk。"""

    type: Literal[EventType.DELTA] = EventType.DELTA  # 判别字段
    text: str  # 文本


class ReplacePayload(_Payload):  # 整段替换
    """校验不通过时替换已流出的正文。"""

    type: Literal[EventType.REPLACE] = EventType.REPLACE  # 判别字段
    text: str  # 替换文本


class CitationPayload(_Payload):  # 引用
    """结构化引用，避免前端正则抠角标。"""

    type: Literal[EventType.CITATION] = EventType.CITATION  # 判别字段
    items: list[dict[str, Any]]  # 引用列表


class UsagePayload(_Payload):  # token 用量
    """缓存字段有才带。"""

    type: Literal[EventType.USAGE] = EventType.USAGE  # 判别字段
    prompt_tokens: int  # 输入
    completion_tokens: int  # 输出
    total_tokens: int | None = None  # 总数
    extra: dict[str, Any] = Field(default_factory=dict)  # 缓存等明细


class NotePayload(_Payload):  # 一句话说明
    """无答案/未标出处时的一句话。"""

    type: Literal[EventType.NOTE] = EventType.NOTE  # 判别字段
    text: str  # 说明文本
    note_kind: str = "plain"  # plain/error


class VerifyPayload(_Payload):  # 引用校验结论
    """L1/L3 的结论：前端据此把角标标灰/标“资料已不可用”。"""

    type: Literal[EventType.VERIFY] = EventType.VERIFY  # 判别字段
    valid: list[int]  # 有效编号
    invalid: list[int]  # 非法编号
    missing: list[int]  # 已失效编号
    unsupported: list[int]  # 未支撑句
    coverage: float  # 引用覆盖率
    passed: bool  # 是否通过
    reasons: list[str]  # 原因码


class DonePayload(_Payload):  # 完成
    """正常结束。"""

    type: Literal[EventType.DONE] = EventType.DONE  # 判别字段
    ttft_ms: int  # 首 token 耗时
    total_ms: int  # 总耗时
    usage: dict[str, Any] = Field(default_factory=dict)  # 用量


class CancelledPayload(_Payload):  # 已停止
    """用户取消或客户端断开。"""

    type: Literal[EventType.CANCELLED] = EventType.CANCELLED  # 判别字段
    reason: str = "user"  # 停止原因


class ErrorPayload(_Payload):  # 失败
    """失败必须带位置，方便定位。"""

    type: Literal[EventType.ERROR] = EventType.ERROR  # 判别字段
    code: str  # 错误码
    message: str  # 错误信息
    stage: str = ""  # 出错位置


class RetryPayload(_Payload):  # 重试
    """第几次、什么错误、等多久；前端据此显示「正在重试」。"""

    type: Literal[EventType.RETRY] = EventType.RETRY  # 判别字段
    attempt: int  # 第几次重试（1..max_attempts）
    max_attempts: int  # 最多重试几次（不含首次请求）
    error_kind: str  # 错误类别（rate_limit/server/timeout/connection/config/unknown）
    error_message: str = ""  # 原始信息（截断）
    delay_ms: int = 0  # 等多久再试
    scope: str = "model"  # 谁的请求（model/structured/embedding…）


PAYLOAD_BY_TYPE: dict[EventType, type[BaseModel]] = {  # 事件类型 -> 载荷模型
    EventType.ACK: AckPayload,  # 已收到
    EventType.STATE: StatePayload,  # 状态迁移
    EventType.STAGE: StagePayload,  # 步骤
    EventType.BASIS: BasisPayload,  # 检索依据
    EventType.SETTINGS: SettingsPayload,  # 本轮设置
    EventType.REASONING: ReasoningPayload,  # 思考流
    EventType.CONFLICT: ConflictPayload,  # 资料冲突
    EventType.APPROVAL: ApprovalPayload,  # 审批
    EventType.CONTEXT: ContextPayload,  # 上下文
    EventType.PII: PiiPayload,  # 脱敏
    EventType.TOOL: ToolPayload,  # 工具
    EventType.DELTA: DeltaPayload,  # 增量
    EventType.REPLACE: ReplacePayload,  # 整段替换
    EventType.CITATION: CitationPayload,  # 引用
    EventType.USAGE: UsagePayload,  # 用量
    EventType.NOTE: NotePayload,  # 说明
    EventType.VERIFY: VerifyPayload,  # 校验结论
    EventType.DONE: DonePayload,  # 完成
    EventType.CANCELLED: CancelledPayload,  # 已停止
    EventType.ERROR: ErrorPayload,  # 失败
    EventType.RETRY: RetryPayload,  # 重试
}  # 映射结束

#: 载荷判别联合：Pydantic 按每个载荷的 ``type`` 自动选模型（类型检查也能收窄）。
PayloadUnion = Annotated[  # 判别联合
    AckPayload  # 已收到
    | StatePayload  # 状态迁移
    | StagePayload  # 步骤
    | BasisPayload  # 检索依据
    | SettingsPayload  # 本轮设置
    | ReasoningPayload  # 思考流
    | ConflictPayload  # 资料冲突
    | ApprovalPayload  # 审批
    | ContextPayload  # 上下文
    | PiiPayload  # 脱敏
    | ToolPayload  # 工具
    | DeltaPayload  # 增量
    | ReplacePayload  # 整段替换
    | CitationPayload  # 引用
    | UsagePayload  # 用量
    | NotePayload  # 说明
    | VerifyPayload  # 校验结论
    | DonePayload  # 完成
    | CancelledPayload  # 已停止
    | ErrorPayload  # 失败
    | RetryPayload,  # 重试
    Field(discriminator="type"),  # 用载荷自带的 type 选模型
]  # 联合结束


class EventEnvelope(BaseModel):  # 事件信封
    """跨边界的唯一事件形状：版本 + 判别字段 + 类型化载荷。"""

    model_config = ConfigDict(extra="forbid")  # 信封也拒绝未知字段

    schema_version: int = 1  # 协议版本
    event_id: str  # 事件 id
    run_id: str = ""  # 归属哪一轮（流式出口会补盖，契约层允许先空）
    timestamp: float = Field(default_factory=time.time)  # 发生时间
    type: EventType  # 判别字段（必须与 payload.type 一致）
    payload: PayloadUnion  # 对应类型的载荷

    @model_validator(mode="before")  # 构造前补齐判别字段
    @classmethod
    def _fill_payload_type(cls, data: Any) -> Any:  # 校验器
        """兼容“payload 只给 dict”的写法：把信封 type 填进缺省判别字段。"""
        if not isinstance(data, dict):  # 只处理字典输入
            return data  # 原样
        payload = data.get("payload")  # 载荷
        event_type = data.get("type")  # 信封类型
        if isinstance(payload, dict) and "type" not in payload and event_type is not None:  # 缺判别字段
            return {**data, "payload": {**payload, "type": event_type}}  # 补上后再校验
        return data  # 原样

    @model_validator(mode="after")  # 校验 type 与 payload 匹配
    def _check_payload_type(self) -> EventEnvelope:  # 校验器
        """信封 type 必须与载荷 type 一致（判别联合只保证了载荷自身合法）。"""
        if self.payload.type is not self.type:  # 两处判别不一致
            raise ValueError(f"信封 type={self.type.value} 与 payload.type={self.payload.type.value} 不一致")  # 拒绝
        return self  # 通过
