"""UI 侧的 HttpClient：POST 命令 + SSE 事件流打 worker（挂 `/api/v1`）。

- ``start``：POST /api/v1/runs；
- ``events``：GET /api/v1/runs/{id}/events（SSE），产出 ``(seq, event)``；
- ``cancel`` / ``approve``：POST。
响应统一为 envelope（code/message/request_id/data），见 :func:`_envelope_ok`。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 解析
from collections.abc import AsyncIterator  # 异步迭代
from typing import Any  # 任意值

import httpx  # HTTP

from app.agent.events import EventEnvelope  # 事件模型
from app.agent.run.types import RunInput  # 入参

#: API 版本前缀（与服务端 app.http.app.API_PREFIX 一致）。
API_PREFIX = "/api/v1"

#: SSE 读超时（秒）：须明显大于服务端心跳（15s）。默认 5s 会被引擎
#: 思考/检索几十秒无事件误杀，放宽到 60s。
_READ_TIMEOUT_S = 60.0  # 读超时


def _envelope_ok(body: dict[str, Any]) -> dict[str, Any]:  # 校验成功体
    """成功时返回 ``data``；业务失败抛 ValueError。"""
    if int(body.get("code") or 0) != 0:  # 业务失败
        raise ValueError(str(body.get("message") or "业务失败"))  # 抛
    return dict(body.get("data") or {})  # 返回 data


class HttpClient:  # 远端客户端
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:  # 构造
        """base_url 形如 ``http://127.0.0.1:8765``；可注入 client（ASGI 测试用）。"""
        self.base_url = base_url.rstrip("/")  # 去尾斜杠
        self._client = (  # 客户端
            client  # 测试注入
            if client is not None  # 有注入
            else httpx.AsyncClient(  # 自建：放宽读超时
                timeout=httpx.Timeout(5.0, read=_READ_TIMEOUT_S),  # 连接/写 5s，读 60s
            )
        )  # 结束
        self._owned = client is None  # 自建才负责关

    async def aclose(self) -> None:  # 关闭
        if self._owned:  # 自建
            await self._client.aclose()  # 关

    async def start(self, run_input: RunInput) -> str:  # 启一轮
        resp = await self._client.post(  # POST
            f"{self.base_url}{API_PREFIX}/runs",  # 路径
            json={  # 请求体（RunCreateRequest 形状）
                "question": run_input.question,  # 问题
                "conversation_id": run_input.conversation_id,  # 会话
                "attachments": list(run_input.attachments),  # 附件
            },  # 结束
        )  # 请求
        resp.raise_for_status()  # 非 2xx 抛
        data = _envelope_ok(resp.json())  # 校验成功体
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
            f"{self.base_url}{API_PREFIX}/runs/{run_id}/events",  # 路径
            headers=headers,  # 头
        ) as resp:  # 响应
            resp.raise_for_status()  # 非 2xx 抛
            try:  # 正常迭代
                async for seq, event in _parse_sse(resp):  # 逐条解析
                    yield seq, event  # 产出
            except (httpx.RemoteProtocolError, httpx.ReadError):  # 终态后关连接
                return  # 视为正常结束

    async def cancel(self, run_id: str) -> None:  # 取消
        resp = await self._client.post(f"{self.base_url}{API_PREFIX}/runs/{run_id}/cancel")  # POST
        resp.raise_for_status()  # 非 2xx 抛
        _envelope_ok(resp.json())  # 校验

    async def approve(self, run_id: str, request_id: str, allow: bool) -> None:  # 审批
        resp = await self._client.post(  # POST
            f"{self.base_url}{API_PREFIX}/runs/{run_id}/approve",  # 路径
            json={"request_id": request_id, "allow": bool(allow)},  # 体
        )  # 请求
        resp.raise_for_status()  # 非 2xx 抛
        _envelope_ok(resp.json())  # 校验

    async def resume(self, run_id: str, value: Any) -> None:  # HITL 恢复
        resp = await self._client.post(  # POST
            f"{self.base_url}{API_PREFIX}/runs/{run_id}/resume",  # 路径
            json={"value": value},  # 体
        )  # 请求
        resp.raise_for_status()  # 非 2xx 抛
        _envelope_ok(resp.json())  # 校验


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
            data.append(line[len("data:"):].lstrip())  # 累积
        elif line.startswith("id:"):  # id 头
            try:  # 转数字
                current_id = int(line[len("id:"):].strip())  # 序号
            except ValueError:  # 非数字
                current_id = 0  # 归零
