"""订单仓储：后台配置 MySQL 连接；同步+异步双模式；表名在代码里写死。

配置只到"库"：host/port/user/password/database。具体表（订单/订单商品）由本模块写死，
不暴露给配置——避免配置里把业务表结构泄漏出去。
"""

from __future__ import annotations  # 延迟值注解

import asyncio  # 丢线程
import os  # 环境变量兜底
from dataclasses import dataclass  # 订单对象
from typing import Any  # 任意值

from app.agent.tools.tooling import ToolError  # 业务错
from app.agent.contracts import ErrorCode  # 错误码

__all__ = ["Order", "OrderItem", "OrderRepo", "get_order_repo", "set_order_repo"]  # 导出

#: 业务表名（写死在工具代码里，配置不感知）。
ORDER_TABLE = "orders"  # 订单主表
ITEM_TABLE = "order_items"  # 订单商品表


@dataclass
class OrderItem:  # 订单商品
    """订单里的商品行。"""

    order_id: str  # 订单号
    sku: str  # 商品编码
    name: str  # 商品名
    qty: int  # 数量
    price: str  # 单价
    amount: str  # 小计

    def to_data(self) -> dict[str, Any]:  # 出参
        return {  # 固定形状
            "order_id": self.order_id,  # 订单
            "sku": self.sku,  # 编码
            "name": self.name,  # 名称
            "qty": self.qty,  # 数量
            "price": self.price,  # 单价
            "amount": self.amount,  # 小计
        }  # 结束


@dataclass
class Order:  # 订单
    """订单主数据（客户后台 MySQL 的一行）。"""

    order_id: str  # 订单号
    status: str  # 状态
    amount: str  # 金额（文本，避免精度）
    pay_method: str  # 支付方式
    created_at: str  # 下单时间
    logistics_no: str = ""  # 运单号（物流工具画像）
    tenant_id: str = ""  # 租户（权限用）

    def to_data(self, items: list[OrderItem] | None = None) -> dict[str, Any]:  # 出参
        return {  # 固定形状
            "order_id": self.order_id,  # 订单
            "status": self.status,  # 状态
            "amount": self.amount,  # 金额
            "pay_method": self.pay_method,  # 支付方式
            "created_at": self.created_at,  # 下单时间
            "logistics_no": self.logistics_no,  # 运单号
            "items": [it.to_data() for it in (items or [])],  # 商品明细
            "text": f"订单 {self.order_id} 状态 {self.status}，金额 {self.amount}，{self.pay_method}，{self.created_at} 下单",  # 一句话
        }  # 结束


class OrderRepo:  # 订单仓储接口（同步+异步）
    """真实实现读后台配置的 MySQL；未配置/连不上抛 ToolError。"""

    async def get_order_detail(self, order_id: str) -> tuple[Order | None, list[OrderItem]]:  # 异步：订单+商品
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在设置里填写 MySQL 连接）")  # 未配置

    def get_order_detail_sync(self, order_id: str) -> tuple[Order | None, list[OrderItem]]:  # 同步：订单+商品
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在设置里填写 MySQL 连接）")  # 未配置

    async def get_orders_by_phone(self, phone: str) -> list[Order]:  # 异步：手机号
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在设置里填写 MySQL 连接）")  # 未配置


def _mysql_config() -> dict[str, Any]:  # 读后台配置
    """从 settings/环境读 MySQL 连接（到库为止，不含表）。"""
    try:  # 配置可能未就绪
        from app.core.config.store import load  # 配置

        values = load().values  # 全部
    except Exception:  # 读失败
        values = {}  # 空
    return {  # 映射（平台配置优先，env 兜底）
        "host": values.get("agent.mysql.host") or os.environ.get("AGENT_MYSQL_HOST") or "",  # 主机
        "port": int(values.get("agent.mysql.port") or os.environ.get("AGENT_MYSQL_PORT") or 3306),  # 端口
        "user": values.get("agent.mysql.user") or os.environ.get("AGENT_MYSQL_USER") or "",  # 账号
        "password": values.get("agent.mysql.password") or os.environ.get("AGENT_MYSQL_PASSWORD") or "",  # 密码
        "database": values.get("agent.mysql.database") or os.environ.get("AGENT_MYSQL_DATABASE") or "",  # 库
    }  # 结束


def _connect() -> Any:  # 建连接（同步）
    """按配置建 pymysql 连接；缺配置/无驱动 → ToolError。"""
    cfg = _mysql_config()  # 配置
    if not cfg.get("host") or not cfg.get("user") or not cfg.get("database"):  # 未配置
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在设置里填写 MySQL 连接）")  # 报错
    try:  # 驱动可能没装
        import pymysql  # MySQL 驱动
    except Exception:  # 未安装
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库驱动未安装（pymysql）")  # 报错
    return pymysql.connect(  # 连库
        host=cfg["host"], port=cfg["port"], user=cfg["user"],  # 主机/端口/账号
        password=cfg["password"], database=cfg["database"], charset="utf8mb4",  # 密码/库/字符集
        connect_timeout=5, read_timeout=8,  # 超时
    )  # 连接


class MySqlOrderRepo(OrderRepo):  # 后台配置的 MySQL 实现
    """同步实现（真实连接）；异步接口用 `to_thread` 圈同步。"""

    async def get_order_detail(self, order_id: str) -> tuple[Order | None, list[OrderItem]]:  # 异步
        return await asyncio.to_thread(self.get_order_detail_sync, order_id)  # 丢线程

    async def get_orders_by_phone(self, phone: str) -> list[Order]:  # 异步
        return await asyncio.to_thread(self._orders_by_phone_sync, phone)  # 丢线程

    def get_order_detail_sync(self, order_id: str) -> tuple[Order | None, list[OrderItem]]:  # 同步
        row = self._fetch_one(f"{ORDER_TABLE} WHERE order_id=%s", (order_id,))  # 主单
        if row is None:  # 无订单
            return None, []  # 空
        items = self._fetch_many(f"{ITEM_TABLE} WHERE order_id=%s", (order_id,))  # 商品
        return self._to_order(row), [self._to_item(i) for i in items]  # 组装

    def _orders_by_phone_sync(self, phone: str) -> list[Order]:  # 同步手机号
        return [self._to_order(r) for r in self._fetch_many(f"{ORDER_TABLE} WHERE phone=%s LIMIT 20", (phone,))]  # 查

    def _fetch_one(self, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:  # 查一行
        rows = self._run(f"SELECT * FROM {where} LIMIT 1", params)  # 执行
        return rows[0] if rows else None  # 首行

    def _fetch_many(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:  # 查多行
        return self._run(f"SELECT * FROM {where} LIMIT 20", params)  # 执行

    def _run(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:  # 同步执行
        conn = _connect()  # 连接
        try:  # 查
            import pymysql  # 游标

            with conn.cursor(pymysql.cursors.DictCursor) as cur:  # 游标
                cur.execute(sql, params)  # 执行（参数占位防注入，表名来自常量）
                return list(cur.fetchall())  # 行
        finally:  # 关
            conn.close()  # 关闭

    @staticmethod
    def _to_order(row: dict[str, Any]) -> Order:  # 行 → 订单
        return Order(  # 订单
            order_id=str(row.get("order_id") or ""), status=str(row.get("status") or ""),  # 单号/状态
            amount=str(row.get("amount") or ""), pay_method=str(row.get("pay_method") or ""),  # 金额/支付
            created_at=str(row.get("created_at") or ""), logistics_no=str(row.get("logistics_no") or ""),  # 时间/运单
            tenant_id=str(row.get("tenant_id") or ""),  # 租户
        )  # 结束

    @staticmethod
    def _to_item(row: dict[str, Any]) -> OrderItem:  # 行 → 商品
        return OrderItem(  # 商品
            order_id=str(row.get("order_id") or ""), sku=str(row.get("sku") or ""),  # 单号/编码
            name=str(row.get("name") or ""), qty=int(row.get("qty") or 0),  # 名称/数量
            price=str(row.get("price") or ""), amount=str(row.get("amount") or ""),  # 单价/小计
        )  # 结束


_repo: OrderRepo | None = None  # 注入位（测试/装配覆盖）


def set_order_repo(repo: OrderRepo | None) -> None:  # 注入仓储
    """测试注入假仓储；生产置 None 用后台配置。"""
    global _repo  # 改模块级
    _repo = repo  # 存


def get_order_repo() -> OrderRepo:  # 取仓储
    global _repo  # 改模块级
    if _repo is None:  # 未注入
        _repo = MySqlOrderRepo()  # 后台配置实现
    return _repo  # 返回