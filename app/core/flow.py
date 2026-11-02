"""全路径流程日志：每一步打一条带调用点绝对路径 + 行号 + 函数名的日志。

用途：真实测试聊天链路时，日志里能直接看到"这一步由哪个文件的哪一行执行"，
方便按路径定位代码（见 AGENTS 的"方法/类要带路径"约定）。
"""
from __future__ import annotations  # 类型注解延迟求值

import inspect  # 取调用点信息

from loguru import logger  # 日志


def flow(tag: str, *, level: str = "DEBUG", **fields: object) -> None:  # 打一条全路径流程日志
    """在调用处打一条 ``flow:<tag> | <绝对路径>:<行号> <函数>`` 的日志。

    参数：
        tag: 步骤名（如 ``http.create_run``）。
        level: 日志级别（默认 DEBUG）。
        fields: 额外键值（拼到日志尾部）。
    """
    frame = inspect.currentframe()  # 当前帧
    caller = frame.f_back if frame is not None else None  # 调用者帧
    if caller is None:  # 取不到
        logger.log(level, "flow:{} | <?>", tag)  # 兜底
        return  # 结束
    code = caller.f_code  # 代码对象
    path = code.co_filename  # 绝对路径（Python 的 co_filename 是完整路径）
    line = caller.f_lineno  # 行号
    func = code.co_name  # 函数名
    extra = " ".join(f"{key}={value!r}" for key, value in fields.items())  # 额外字段
    logger.log(level, "flow:{} | {}:{} {} {}", tag, path, line, func, extra)  # 打出
