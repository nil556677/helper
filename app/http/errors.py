"""统一业务异常与错误码。

- 业务代码只 ``raise ApiError(...)``；
- 全局异常处理器把任何 ``ApiError``/校验错误转成统一 envelope（见 ``app.http.app``）。
"""
from __future__ import annotations  # 类型注解延迟求值

#: 业务错误码（0=成功；非 0 是错误）。
E_BAD_REQUEST = 40001  # 参数错误
E_NOT_FOUND = 40401  # 会话/消息不存在
E_RUN_NOT_FOUND = 40402  # run 不存在
E_WORKER_BUSY = 40901  # worker 未就绪
E_DUPLICATE = 40902  # 重复提交
E_TOO_MANY = 42901  # 并发超限
E_INTERNAL = 50001  # 内部错误


class ApiError(Exception):  # 业务异常
    """带 code/status 的业务异常；全局 handler 据此返回统一 envelope。"""

    def __init__(  # 构造
        self,  # 实例
        code: int = E_INTERNAL,  # 业务码
        message: str = "内部错误",  # 人话
        status: int = 500,  # HTTP 状态码
    ) -> None:  # 无返回
        super().__init__(message)  # 交父类
        self.code = code  # 业务码
        self.message = message  # 消息
        self.status = status  # HTTP 状态码
