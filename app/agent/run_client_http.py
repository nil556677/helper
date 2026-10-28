"""Run RPC 客户端（E0.2）：POST 命令 + SSE 事件流打 worker。

- ``start``：POST /runs；
- ``events``：GET /runs/{id}/events（SSE），产出 ``(seq, event)``——seq 是服务端单调
  序号，供 ``Last-Event-ID`` 续传；
- ``cancel`` / ``approve``：POST。

事件体是 ``events.EventEnvelope.model_dump_json()``，这里用 ``EventEnvelope.model_validate`` 还原。
"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 解析
from collections.abc import AsyncIterator  # 异步迭代

import httpx  # HTTP

from app.agent.events import EventEnvelope  # 事件模型
from app.agent.run_client import RunInput  # 入参

#: SSE 读超时（秒）：必须明显大于服务端心跳（``run_server._HEARTBEAT_S = 15``）。
#: httpx 默认读超时只有 5 秒，而 SSE 在"引擎思考/检索"期间可以几十秒没有事件——
#: 用默认值会在空闲段被 ReadTimeout 误杀，进而让 ``RunSession._feed`` 反复重连、
#: UI 迟迟收不到首事件（真实踩过：8 秒首事件超时误报成 600 秒）。
_READ_TIMEOUT_S = 60.0  # 读超时（连接/写仍用 5s）


class HttpClient:  # 远端客户端
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:  # 构造
        """base_url 形如 ``http://127.0.0.1:8765``；可注入 client（ASGI 测试用）。"""
        self.base_url = base_url.rstrip("/")  # 去掉尾斜杠
        self._client = (  # 客户端
            client  # 测试注入
            if client is not None  # 有注入
            else httpx.AsyncClient(  # 自建：放宽读超时
                timeout=httpx.Timeout(5.0, read=_READ_TIMEOUT_S),  # 连接/写 5s，读 60s
            )
        )  # 客户端结束
        self._owned = client is None  # 是否自己创建的（自己创建才负责关）

    async def aclose(self) -> None:  # 关闭
        if self._owned:  # 自己建的
            await self._client.aclose()  # 关

    async def start(self, run_input: RunInput) -> str:  # 启一轮
        resp = await self._client.post(  # POST
            f"{self.base_url}/runs",  # 路径
            json={  # 请求体
                "question": run_input.question,  # 问题
                "conversation_id": run_input.conversation_id,  # 会话
                "attachments": list(run_input.attachments),  # 附件
            },  # 请求体结束
        )  # 请求
        resp.raise_for_status()  # 非 2xx 抛
        data = resp.json()  # 解析
        return str(data["run_id"])  # run id

    async def events(  # 订阅事件
        self, run_id: str, *, last_event_id: str = ""  # sse 续传点
    ) -> AsyncIterator[tuple[int, EventEnvelope]]:  # (seq, event)
        """产出 ``(seq, event)``；服务端流结束后自然停止。"""
        headers = {}  # 请求头
        if last_event_id:  # 有续传点
            headers["Last-Event-ID"] = str(last_event_id)  # 带上
        async with self._client.stream(  # 流式 GET
            "GET",  # 方法
            f"{self.base_url}/runs/{run_id}/events",  # 路径
            headers=headers,  # 头
        ) as resp:  # 响应
            resp.raise_for_status()  # 非 2xx 抛
            try:  # 正常迭代
                async for seq, event in _parse_sse(resp):  # 逐条解析
                    yield seq, event  # 产出
            except (httpx.RemoteProtocolError, httpx.ReadError):  # 服务端在终态后主动关连接
                return  # 视为正常结束

    async def cancel(self, run_id: str) -> None:  # 取消
        resp = await self._client.post(f"{self.base_url}/runs/{run_id}/cancel")  # POST
        resp.raise_for_status()  # 非 2xx 抛

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        resp = await self._client.post(  # POST
            f"{self.base_url}/runs/{run_id}/approve",  # 路径
            json={"request_id": request_id, "allow": bool(allow)},  # 体
        )  # 请求
        resp.raise_for_status()  # 非 2xx 抛


async def _parse_sse(  # 解析 SSE 行
    resp: httpx.Response,  # 响应
) -> AsyncIterator[tuple[int, EventEnvelope]]:  # (seq, event)
    """逐行解析 SSE；完整（空行分隔）时还原成 Event 并产出。"""
    current_id: int | None = None  # 当前 seq
    data: list[str] = []  # data 行累积
    async for line in resp.aiter_lines():  # 逐行
        if line == "":  # 空行 = 事件终
            if data:  # 有数据
                payload = json.loads("".join(data))  # 解析 JSON
                yield (current_id or 0), EventEnvelope.model_validate(payload)  # 产出
                data = []  # 复位
                current_id = None  # 复位
            continue  # 下一个
        if line.startswith(":"):  # 注释/心跳
            continue  # 跳过
        if line.startswith("data:"):  # data 头
            data.append(line[len("data:"):].lstrip())  # 累积（去一个空格）
        elif line.startswith("id:"):  # id 头
            try:  # 转数字
                current_id = int(line[len("id:"):].strip())  # 序号
            except ValueError:  # 非数字
                current_id = 0  # 归零
