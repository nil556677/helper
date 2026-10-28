"""脱敏中间件：出站到模型/工具/日志前掩码，不把原值写进事件。"""

from __future__ import annotations  # 类型注解延迟求值

import re  # 正则
from dataclasses import replace  # 复制请求
from typing import Any  # 任意值

from app.agent.context import RunContext  # 运行上下文
from app.agent.events import pii as pii_event  # 事件
from app.agent.middleware.base import (  # 中间件契约
    Middleware,
    ModelCall,
    ModelRequest,
    ModelResponse,
)
from app.agent.streaming import aclose  # 关闭上游流

#: 默认规则；顺序有意义（身份证/银行卡在前，避免被手机号规则切碎）。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (  # 规则表
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),  # 身份证
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),  # 银行卡
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),  # 手机号
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),  # 邮箱
)  # 规则表结束


#: 可能出现"号码/邮箱"里的连接符（字母数字另算）：这些字符连在一起时不能从中间切。
_PII_CHARS = frozenset("._+-@")  # 除字母数字外的连接符


def _safe_cut(buffer: str) -> int:  # 找安全切口
    """返回可安全脱敏并放行的前缀长度。

    判据：最后一个"不可能属于号码/邮箱"的字符之后。中文、标点、空白都是安全边界，
    所以中文正文几乎不会被延迟；只有一长串数字/字母/邮箱字符才会被整段扣住。
    """
    for index in range(len(buffer) - 1, -1, -1):  # 从尾部往前找
        char = buffer[index]  # 当前字符
        if char.isascii() and (char.isalnum() or char in _PII_CHARS):  # 可能是号码的一部分
            continue  # 继续往前
        return index + 1  # 切在它后面
    return 0  # 整段都可能是号码


def redact(text: str) -> tuple[str, dict[str, int]]:  # 脱敏
    """返回（脱敏文本，类型计数）；不保留原值。"""
    counts: dict[str, int] = {}  # 计数
    result = str(text or "")  # 复制
    for name, pattern in _RULES:  # 逐条规则
        result, hits = pattern.subn(f"[REDACTED:{name}]", result)  # 替换
        if hits:  # 有命中
            counts[name] = counts.get(name, 0) + hits  # 累计
    return result, counts  # 返回


def _redact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:  # 批量脱敏
    """只处理 content 是字符串的消息。"""
    counts: dict[str, int] = {}  # 总计数
    out: list[dict[str, Any]] = []  # 结果
    for message in messages:  # 逐条
        item = dict(message)  # 复制
        content = item.get("content")  # 取正文
        if isinstance(content, str):  # 字符串才处理
            cleaned, hits = redact(content)  # 脱敏
            item["content"] = cleaned  # 写回
            for key, value in hits.items():  # 合并计数
                counts[key] = counts.get(key, 0) + value  # 累计
        out.append(item)  # 追加
    return out, counts  # 返回


class PIIMiddleware(Middleware):  # 脱敏
    """本地库保留原文；只拦出站内容。"""

    name = "pii"  # 名称

    async def wrap_model_call(  # 包模型调用
        self,  # 实例
        ctx: RunContext,  # 上下文
        request: ModelRequest,  # 请求
        call_next: ModelCall,  # 下一层
    ) -> ModelResponse:  # 响应
        """入站/出站都脱敏；事件只报类型与次数。"""
        messages, counts = _redact_messages(request.messages)  # 消息脱敏
        system, system_counts = redact(request.system)  # system 脱敏
        for key, value in system_counts.items():  # 合并 system 计数
            counts[key] = counts.get(key, 0) + value  # 累计
        if counts:  # 有命中
            ctx.redactions.append(dict(counts))  # 记统计
            ctx.emit(pii_event(dict(counts)))  # 发事件
        response = await call_next(replace(request, messages=messages, system=system))  # 调模型
        if response.stream is not None:  # 流式
            async def redacted_stream():  # 包一层流
                """按安全边界攒一段再脱敏，避免号码被 chunk 切断而漏掉。"""
                out_counts: dict[str, int] = {}  # 出站计数
                buffer = ""  # 还没到安全边界的尾巴
                try:  # 正常/异常都要关上游
                    async for chunk in response.stream:  # 逐 token
                        buffer += chunk  # 先攒
                        cut = _safe_cut(buffer)  # 能安全放行的前缀
                        if not cut:  # 整段都可能是号码/邮箱
                            continue  # 继续攒
                        head, buffer = buffer[:cut], buffer[cut:]  # 切开
                        cleaned, hits = redact(head)  # 脱敏前半段
                        for key, value in hits.items():  # 合并统计
                            out_counts[key] = out_counts.get(key, 0) + value  # 累计
                        if cleaned:  # 有内容
                            yield cleaned  # 放行
                    if buffer:  # 收尾剩下的尾巴
                        cleaned, hits = redact(buffer)  # 脱敏
                        for key, value in hits.items():  # 合并统计
                            out_counts[key] = out_counts.get(key, 0) + value  # 累计
                        if cleaned:  # 有内容
                            yield cleaned  # 放行
                finally:  # 取消或客户端断开
                    await aclose(response.stream)  # 关掉上游
                if out_counts:  # 有命中
                    ctx.redactions.append(dict(out_counts))  # 记统计
                    ctx.emit(pii_event(dict(out_counts)))  # 发事件

            return replace(response, stream=redacted_stream(), text="")  # 替换流
        cleaned, out_counts = redact(response.text)  # 输出脱敏
        if out_counts:  # 输出有命中
            ctx.redactions.append(dict(out_counts))  # 记统计
            ctx.emit(pii_event(dict(out_counts)))  # 发事件
        return replace(response, text=cleaned)  # 返回脱敏后的响应
