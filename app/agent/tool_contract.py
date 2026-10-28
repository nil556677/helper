"""E4 工具契约核心（E-B）：白名单 / 版本化 / 包络 / 序列化。

纯数据结构与规则，不依赖图/驱动，可单测。
- ``ToolSpec``：name + version + args/result schema + handler + enabled + permissions；
- ``ToolRegistry``：按 name 注册，能解析最新版本、按 agent allowlist 过滤；
- 结果包络：``ok/code/message/retryable/retry_after_s/data/meta``（协议/执行两级 code）；
- ``safe_serialize``：datetime/Decimal/bytes/set → JSON 安全值。

（路由、bind_tools、interrupt 等编排部分另列，见 E4 落地。）
"""

from __future__ import annotations  # 类型注解延迟求值

import datetime  # 时间
import decimal  # 十进制
from collections.abc import Callable  # 类型
from dataclasses import dataclass  # 数据类
from typing import Any


@dataclass(frozen=True)
class ToolSpec:  # 工具定义
    """白名单 + 版本的载体。model 只看到 ``name``（版本在注册表内部路由）。"""

    name: str  # 稳定标识，如 "search_kb"
    version: int  # 递增；破坏性变更 +1
    args_schema: type  # 入参契约（Pydantic）
    handler: Callable[..., Any]  # 执行函数
    result_schema: type | None = None  # 结果 data 契约
    enabled: bool = True  # 是否可用（禁用 → protocol 错误）
    permissions: tuple[str, ...] = ()  # 允许使用它的 agent 权限


class ToolRegistry:  # 工具注册表（白名单唯一来源）
    """按 name 持有各版本；解析最新 / 按 allowlist 过滤。"""

    def __init__(self) -> None:  # 构造
        self._by_name: dict[str, dict[int, ToolSpec]] = {}  # name → {version: spec}

    def register(self, spec: ToolSpec) -> None:  # 注册
        bucket = self._by_name.setdefault(spec.name, {})  # 取桶
        if spec.version in bucket:  # 已注册同版本
            raise ValueError(f"tool {spec.name}@{spec.version} already registered")  # 防撞
        bucket[spec.version] = spec  # 存

    def resolve(self, name: str, version: int | None = None) -> ToolSpec | None:  # 解析
        bucket = self._by_name.get(name)  # 桶
        if not bucket:  # 没有该名
            return None  # 无
        if version is not None:  # 指定版本
            return bucket.get(version)  # 取
        return bucket[max(bucket)]  # 最新

    def versions(self, name: str) -> list[int]:  # 版本列表
        bucket = self._by_name.get(name, {})  # 桶
        return sorted(bucket)  # 升序

    def allowlist(self, perms: tuple[str, ...]) -> list[ToolSpec]:  # 按权限过滤
        """返回给该 agent 的工具（enabled 且权限匹配；取各自最新版）。"""
        out: list[ToolSpec] = []  # 结果
        for _name, bucket in self._by_name.items():  # 逐工具
            latest = bucket[max(bucket)]  # 最新版
            if not latest.enabled:  # 禁用
                continue  # 跳过
            if perms and latest.permissions and not set(perms) & set(latest.permissions):  # 权限不匹配
                continue  # 跳过
            out.append(latest)  # 收
        return out  # 返回


#: 两级错误码前缀（协议 = 推理 bug 不重试；执行 = 可重试/环境）。
PROTOCOL = "protocol"  # 协议层
EXECUTION = "execution"  # 执行层


def success_envelope(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:  # 成功包络
    return {  # 返回
        "ok": True,  # 成功
        "code": "ok",  # 机器码
        "message": "",  # 可展示
        "retryable": False,  # 成功无需重试
        "retry_after_s": None,  # 无等待
        "data": safe_serialize(data),  # 数据（已序列化）
        "meta": meta or {},  # 扩展
    }  # 返回结束


def error_envelope(  # 错误包络
    code: str,  # 机器码（含 protocol./execution. 前缀）
    message: str,  # 可展示：什么失败 + 为什么 + 怎么恢复
    *,  # 仅关键字
    retryable: bool = False,  # 是否可重试
    retry_after_s: float | None = None,  # 建议等待
    data: Any = None,  # 附加
    details: dict[str, Any] | None = None,  # 诊断（脱敏）
) -> dict[str, Any]:  # 包络
    return {  # 返回
        "ok": False,  # 失败
        "code": code,  # 机器码
        "message": message,  # 可展示
        "retryable": retryable,  # 是否重试
        "retry_after_s": retry_after_s,  # 等待
        "data": safe_serialize(data),  # 附加
        "meta": details or {},  # 诊断
    }  # 返回结束


def safe_serialize(obj: Any) -> Any:  # 递归序列化
    """datetime/Decimal/bytes/set → JSON 安全值；其余原样。"""
    if isinstance(obj, dict):  # 字典
        return {k: safe_serialize(v) for k, v in obj.items()}  # 递归
    if isinstance(obj, (list, tuple)):  # 序列
        return [safe_serialize(v) for v in obj]  # 递归
    if isinstance(obj, set):  # 集合
        return [safe_serialize(v) for v in sorted(obj, key=str)]  # 排序后列表
    if isinstance(obj, (datetime.datetime, datetime.date)):  # 时间
        return obj.isoformat()  # ISO
    if isinstance(obj, decimal.Decimal):  # 十进制
        return str(obj)  # 字符串
    if isinstance(obj, bytes):  # 字节
        return obj.decode("utf-8", errors="replace")  # 解码
    return obj  # 原样
