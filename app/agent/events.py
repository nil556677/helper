"""编排层事件门面：统一走契约层 contracts，调用方只需 events.xxx()。

字段名按「见名知意」重定（title / duration_ms / hit_counts / trigger …），调用位置签名尽量不动。
"""

from __future__ import annotations  # 类型注解延迟求值

from typing import Any  # 任意值

from app.agent.contracts import (  # 契约层
    TERMINAL_TYPES,  # 终态集合（向外再导出，测试/调用方用到）
    EventEnvelope,  # 事件信封
    EventType,  # 事件类型
    make,  # 事件构造器
)  # 导入结束

#: 对外的公共名（含再导出；列表里的名字 ruff 视为"已使用"，不会再报 F401）。
__all__ = ["TERMINAL_TYPES", "EventType"]


def ack(run_id: str) -> EventEnvelope:  # 已收到
    """前端立即上屏后就能收到；run_id 进信封。"""
    return make(EventType.ACK, run_id=run_id)  # 事件


def state(before: str, after: str, trigger: str, reason: str = "") -> EventEnvelope:  # 状态迁移
    """run 生命周期变化；trigger 是触发迁移的事件。"""
    return make(  # 事件
        EventType.STATE,  # 类型
        before=before,  # 迁移前
        after=after,  # 迁移后
        trigger=trigger,  # 触发事件
        reason=reason,  # 原因
    )  # 事件结束


def stage(  # 节点/步骤事件
    stage_id: str,  # 步骤 id
    status: str,  # running/done/skipped/failed
    *,
    title: str = "",  # 显示标题
    detail: str = "",  # 明细
    duration_ms: int | None = None,  # 耗时
    group: str = "",  # 用户级分组（空 = 只埋 trace 不展示）
) -> EventEnvelope:  # 事件
    """stage_id 固定，前端按它原地更新；group 由调用处声明，前端不再映射 id。"""
    return make(  # 事件
        EventType.STAGE,  # 类型
        stage_id=stage_id,  # 步骤 id
        status=status,  # 状态
        title=title,  # 标题
        detail=detail,  # 明细
        duration_ms=duration_ms,  # 耗时
        group=group,  # 分组
    )  # 事件结束


def context(turns: int, tokens: int, trimmed: int = 0, **extra: Any) -> EventEnvelope:  # 上下文事件
    """只报数量与分块占用，不报正文；``extra`` 走完了其他可选字段。"""
    return make(EventType.CONTEXT, turns=turns, tokens=tokens, trimmed=trimmed, **extra)  # 事件


def basis(  # 检索依据
    keywords: list[str],  # 查询词
    hit_counts: dict[str, int],  # 各路命中计数
    sources: list[dict[str, Any]],  # 来源
) -> EventEnvelope:  # 事件
    """给用户看的“我是怎么找的”，不是思维链。"""
    return make(  # 事件
        EventType.BASIS,  # 类型
        keywords=keywords,  # 关键词
        hit_counts=hit_counts,  # 计数
        sources=sources,  # 来源
    )  # 事件结束


def settings(  # 本轮设置
    model: str,  # 模型名
    temperature: float,  # 温度
    system_prompt: str,  # 系统提示词
    extra: dict[str, Any] | None = None,  # 额外参数
) -> EventEnvelope:  # 事件
    """供“查看本轮设置”弹窗。"""
    return make(  # 事件
        EventType.SETTINGS,  # 类型
        model=model,  # 模型
        temperature=temperature,  # 温度
        system_prompt=system_prompt,  # 提示词
        extra=dict(extra or {}),  # 额外
    )  # 事件结束


def pii(counts: dict[str, int]) -> EventEnvelope:  # 脱敏事件
    """只报类型与次数，不报原值。"""
    return make(EventType.PII, counts=counts)  # 事件


def tool(  # 工具事件
    name: str,  # 工具名
    status: str,  # 状态
    detail: str = "",  # 明细
    *,
    variant: str = "",  # read/write/edit/bash
    path: str = "",  # 文件
    diff: str = "",  # 改动
    output: str = "",  # 输出
    call_id: str = "",  # 调用 id
) -> EventEnvelope:  # 事件
    """工具调用/结果；file 工具带 variant/path/diff。"""
    return make(  # 事件
        EventType.TOOL,  # 类型
        tool_name=name,  # 工具名
        status=status,  # 状态
        detail=detail,  # 明细
        variant=variant,  # 类别
        path=path,  # 文件
        diff=diff,  # 改动
        output=output,  # 输出
        call_id=call_id,  # id
    )  # 事件结束


def approval(  # 审批
    request_id: str,  # 请求 id
    name: str,  # 操作名
    risk: str,  # 风险说明
    detail: str = "",  # 明细
    diff: str = "",  # 改动预览
) -> EventEnvelope:  # 事件
    """文件写入/编辑/命令执行前询问。"""
    return make(  # 事件
        EventType.APPROVAL,  # 类型
        request_id=request_id,  # 请求 id
        name=name,  # 操作名
        risk=risk,  # 风险
        detail=detail,  # 明细
        diff=diff,  # 改动
    )  # 事件结束


def clarify(  # 澄清待办
    thread_id: str,  # 恢复用线程
    title: str = "需要确认",  # 标题
    description: str = "",  # 说明
    options: list[dict] | None = None,  # 选项
    risk: str = "low",  # 风险
) -> EventEnvelope:  # 事件
    """HITL 暂停：前端渲染选项并回传选择。"""
    return make(  # 事件
        EventType.CLARIFY,  # 类型
        thread_id=thread_id,  # 线程
        title=title,  # 标题
        description=description,  # 说明
        options=list(options or []),  # 选项
        risk=risk,  # 风险
    )  # 事件结束


def delta(text: str) -> EventEnvelope:  # 答案增量
    """一个 token/chunk。"""
    return make(EventType.DELTA, text=text)  # 事件


def reasoning(text: str) -> EventEnvelope:  # 思考增量
    """模型返回的 reasoning 内容；没有就不发。"""
    return make(EventType.REASONING, text=text)  # 事件


def conflict(message: str) -> EventEnvelope:  # 资料冲突
    """资料答案与通用知识不一致时提示用户核对。"""
    return make(EventType.CONFLICT, message=message)  # 事件


def citation(items: list[dict[str, Any]]) -> EventEnvelope:  # 引用
    """结构化引用，避免前端正则抠角标。"""
    return make(EventType.CITATION, items=items)  # 事件


def replace(text: str) -> EventEnvelope:  # 整段替换
    """校验不通过时把已流出的正文替换掉（只能出现一次，且在 done 之前）。"""
    return make(EventType.REPLACE, text=text)  # 事件


def verify_report(  # 校验结果
    valid: list[int],  # 有效编号
    invalid: list[int],  # 非法编号
    missing: list[int],  # 已失效编号
    coverage: float,  # 引用覆盖率
    reasons: list[str] | None = None,  # 原因码
    unsupported: list[int] | None = None,  # 没被证据支撑的句子序号
) -> EventEnvelope:  # 事件
    """L1/L3/L2 的结论：前端据此把角标标灰/标失效。"""
    return make(  # 事件
        EventType.VERIFY,  # 类型
        valid=valid,  # 有效
        invalid=invalid,  # 非法
        missing=missing,  # 失效
        unsupported=list(unsupported or []),  # 未支撑句
        coverage=coverage,  # 覆盖率
        passed=bool(valid) and not missing,  # 是否通过
        reasons=sorted(set(reasons or [])),  # 原因
    )  # 事件结束


def usage(  # 用量
    prompt_tokens: int,  # 输入
    completion_tokens: int,  # 输出
    total_tokens: int | None = None,  # 总数
    extra: dict[str, Any] | None = None,  # 缓存等明细
) -> EventEnvelope:  # 事件
    """token 统计；缓存字段放进 extra。"""
    total = prompt_tokens + completion_tokens if total_tokens is None else total_tokens  # 总数
    return make(  # 事件
        EventType.USAGE,  # 类型
        prompt_tokens=prompt_tokens,  # 输入
        completion_tokens=completion_tokens,  # 输出
        total_tokens=total,  # 总数
        extra=dict(extra or {}),  # 明细
    )  # 事件结束


def done(ttft_ms: int, total_ms: int, usage_data: dict[str, Any] | None = None) -> EventEnvelope:  # 完成
    """正常结束。"""
    return make(  # 事件
        EventType.DONE,  # 类型
        ttft_ms=ttft_ms,  # 首 token
        total_ms=total_ms,  # 总耗时
        usage=dict(usage_data or {}),  # 用量
    )  # 事件结束


def note(text: str, kind: str = "plain") -> EventEnvelope:  # 说明
    """没有答案/未标注出处时的一句话。"""
    return make(EventType.NOTE, text=text, note_kind=kind)  # 事件


def cancelled(reason: str = "user") -> EventEnvelope:  # 已停止
    """用户取消或客户端断开。"""
    return make(EventType.CANCELLED, reason=reason)  # 事件


def error(code: str, message: str, stage_id: str = "") -> EventEnvelope:  # 失败
    """失败必须带位置，方便定位。"""
    return make(EventType.ERROR, code=code, message=message, stage=stage_id)  # 事件


def retry(  # 重试事件
    attempt: int,  # 第几次重试
    max_attempts: int,  # 最多重试几次
    error_kind: str,  # 错误类别
    *,
    error_message: str = "",  # 原始信息
    delay_ms: int = 0,  # 等多久再试
    scope: str = "model",  # 谁的请求
) -> EventEnvelope:  # 事件
    """每次重试一条：前端显示「第 2/3 次重试：429 限流，1.2s 后继续」。"""
    return make(  # 构造
        EventType.RETRY,  # 类型
        attempt=attempt,  # 第几次
        max_attempts=max_attempts,  # 总共
        error_kind=error_kind,  # 类别
        error_message=error_message,  # 信息
        delay_ms=delay_ms,  # 等待
        scope=scope,  # 作用域
    )  # 构造结束
