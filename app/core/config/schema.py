"""引导配置的形状（pydantic 校验）。

技术方案 §12 把配置分成**两处**，这里是"引导项"那一处——只有"打开数据库之前就必须知道"的
东西（日志级别、数据目录覆盖）。其余全部（大模型四槽位、api key、检索参数、界面偏好）
都在 SQLite 里，由界面来改。

**为什么引导项也要校验**：``config.json`` 是用户能手改的文件。坏值必须在**加载期**被拒并隔离，
而不是等到某一步才炸——那时往往已经在界面渲染路径上，表现就是"窗口打不开"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

#: 允许的日志级别（与 core/logging 的用法一致）。
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


#: 默认监听地址：只绑回环（技术方案 §5.1 红线 4）。NiceGUI 默认是 0.0.0.0，所以必须显式给。
DEFAULT_HOST = "127.0.0.1"

#: 算回环的写法（配置里填这些都不会把界面暴露出去）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def is_loopback(host: str) -> bool:
    """这个监听地址是否只对本机开放。

    为什么要有这个判断：界面**没有任何认证**，一旦绑到非回环地址（``0.0.0.0``、局域网 IP），
    同一个网络里谁都能打开它、翻资料与设置。默认值必须是回环；配置里能改，但改了就属于
    "你自己知道在干什么"，启动时要打一条 WARNING 留痕（见 ``app/__main__._listen_host``）。
    """
    normalized = host.strip().strip("[]").lower()
    return normalized in LOOPBACK_HOSTS or normalized.startswith("127.")


class BootstrapConfig(BaseModel):
    """``config.json`` 的全部内容——就这两项。

    ``extra="forbid"``：键名拼错要立刻报错。"用户以为改了、其实没生效"是最难查的一类问题。
    ````_`` 开头的键由加载器当注释剥掉（见 access._strip_notes），不参与校验。
    """

    model_config = ConfigDict(extra="forbid")

    log_level: LogLevel = "INFO"

    #: 界面服务的监听地址。默认回环；**填非回环会告警**（界面没有认证，绑出去等于给整个局域网看）。
    #: 只填地址，不要带 ``http://`` 或路径。留空视为默认值。
    host: str = DEFAULT_HOST

    #: 数据目录覆盖；None = 用安装目录下的 ``data/``。
    #: 注意优先级：环境变量 ``HELPER_DATA_DIR`` > 这一项 > 默认值。
    data_dir: str | None = None

    @field_validator("host")
    @classmethod
    def _clean_host(cls, value: str) -> str:
        """空值回落默认；带协议/路径这种"填错了"的值要在加载期就报错。"""
        cleaned = value.strip()
        if not cleaned:
            return DEFAULT_HOST
        if "://" in cleaned or "/" in cleaned:
            raise ValueError("host 只填地址（例如 127.0.0.1），不要带协议或路径")
        return cleaned
