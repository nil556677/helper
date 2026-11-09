"""订单仓储：后台配置 MySQL 连接；测试可注入假仓储（不真连库）。"""

from __future__ import annotations  # 延迟值注解

from dataclasses import dataclass  # 订单对象
from typing import Any  # 任意值

from app.agent.tools.tooling import ToolError  # 业务错
from app.agent.contracts import ErrorCode  # 错误码

__all__ = ["Order", "OrderRepo", "get_order_repo", "set_order_repo"]  # 导出


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

    def to_data(self) -> dict[str, Any]:  # 出参
        return {  # 固定形状
            "order_id": self.order_id,  # 订单
            "status": self.status,  # 状态
            "amount": self.amount,  # 金额
            "pay_method": self.pay_method,  # 支付方式
            "created_at": self.created_at,  # 下单时间
            "logistics_no": self.logistics_no,  # 运单号
            "text": f"订单 {self.order_id} 状态 {self.status}，金额 {self.amount}，{self.pay_method}，{self.created_at} 下单",  # 一句话
        }  # 结束


class OrderRepo:  # 订单仓储接口
    """真实实现读后台配置的 MySQL；未配置/连不上抛 ToolError。"""

    async def get_order(self, order_id: str) -> Order | None:  # 按单号
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在后台填写 MySQL 地址/账号/密码）")  # 未配置

    async def get_orders_by_phone(self, phone: str) -> list[Order]:  # 按手机号
        raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在后台填写 MySQL 地址/账号/密码）")  # 未配置


def _mysql_config() -> dict[str, Any]:  # 读后台配置
    """从 settings 读 MySQL 连接（agent.mysql.*）；取不到返回空。"""
    try:  # 配置可能未就绪
        from app.core.config.store import load  # 配置

        values = load().values  # 全部
    except Exception:  # 读失败
        return {}  # 空
    return {  # 映射
        "host": values.get("agent.mysql.host") or "",  # 主机
        "port": int(values.get("agent.mysql.port") or 3306),  # 端口
        "user": values.get("agent.mysql.user") or "",  # 账号
        "password": values.get("agent.mysql.password") or "",  # 密码
        "database": values.get("agent.mysql.database") or "",  # 库
        "table": values.get("agent.mysql.table") or "orders",  # 表
    }  # 结束


class MySqlOrderRepo(OrderRepo):  # 后台配置的 MySQL 实现
    """用 pymysql 查客户订单表；未装驱动/未配置 → ToolError。"""

    async def get_order(self, order_id: str) -> Order | None:  # 按单号
        rows = await self._query("order_id = %s", (order_id,))  # 查
        return rows[0] if rows else None  # 首条

    async def get_orders_by_phone(self, phone: str) -> list[Order]:  # 按手机号
        return await self._query("phone = %s", (phone,))  # 查

    async def _query(self, where: str, params: tuple[Any, ...]) -> list[Order]:  # 通用查
        cfg = _mysql_config()  # 配置
        if not cfg.get("host") or not cfg.get("user") or not cfg.get("database"):  # 未配置
            raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库未配置（请在后台填写 MySQL 地址/账号/密码）")  # 报错
        try:  # 驱动可能没装
            import pymysql  # MySQL 驱动
        except Exception:  # 未安装
            raise ToolError(str(ErrorCode.EXECUTION_FAILED), "订单库驱动未安装（pymysql）")  # 报错
        import asyncio  # 线程

        def _run() -> list[Order]:  # 同步查
            conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                                   password=cfg["password"], database=cfg["database"], charset="utf8mb4",
                                   connect_timeout=5, read_timeout=8)  # 连
            try:  # 查
                with conn.cursor(pymysql.cursors.DictCursor) as cur:  # 游标
                    cur.execute(f"SELECT * FROM {cfg['table']} WHERE {where} LIMIT 20", params)  # SQL
                    data = cur.fetchall()  # 全部
            finally:  # 关
                conn.close()  # 关闭
            return [  # 映射
                Order(
                    order_id=str(row.get("order_id") or ""), status=str(row.get("status") or ""),
                    amount=str(row.get("amount") or ""), pay_method=str(row.get("pay_method") or ""),
                    created_at=str(row.get("created_at") or ""), logistics_no=str(row.get("logistics_no") or ""),
                    tenant_id=str(row.get("tenant_id") or ""),
                )  # 一行
                for row in data  # 逐行
            ]  # 返回

        return await asyncio.to_thread(_run)  # 丢线程


_repo: OrderRepo | None = None  # 注入位（测试/装配覆盖）


def set_order_repo(repo: OrderRepo | None) -> None:  # 注入仓储
    """测试注入假仓储；生产置 None 用后台配置的真实实现。"""
    global _repo  # 改模块级
    _repo = repo  # 存


def get_order_repo() -> OrderRepo:  # 取仓储
    """优先注入；否则后台配置的 MySQL 实现。"""
    global _repo  # 改模块级
    if _repo is None:  # 未注入
        _repo = MySqlOrderRepo()  # 后台配置实现
    return _repo  # 返回