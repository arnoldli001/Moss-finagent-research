"""WebSocket告警实时推送中心（FR-10）。

租户隔离：tenant_id → 连接集合；广播失败自动清理死连接；
离线不补发（客户端上线后主动拉 REST 列表）。

## ⚠️ 脱敏必须**按连接**做，不能按租户做

`tenant_id` 是**套餐等级**（admin / vip / trial），不是"一个人"。
一个 vip 租户下可以有很多在线用户，而其中**只有管理员**该看到
`source_name` / `source_url`。

原来的写法是：

    message = {"type": "alert", "data": alert.model_dump()}
    for websocket in self._clients[tenant_id]: await websocket.send_json(message)

**一条消息序列化一次、发给所有人** —— 于是任何 vip 用户都拿到了带真名与
原文链接的告警（`source_name: "东方财富-全球财经"`、
`source_url: "https://finance.eastmoney.com/a/..."`），按 F12 即见。

现在每个连接单独序列化（序列化是按连接的，这是**必须付的代价**：
按租户缓存一份就等于把管理员的视图发给了所有人）。
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import WebSocket

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.models import DEFAULT_TENANT, Alert
from src.domain.alerts.normalize import now_iso

logger = logging.getLogger(__name__)


def alert_payload(alert: Alert, *, is_admin: bool) -> dict:
    """单条告警 → 面向某类观看者的 payload。

    真正的脱敏规则在 `src.api.routes.alerts.alert_to_public`（那里同时服务
    REST 端点，两处必须一致）。这里只做转发，避免规则写两份漂移。
    """
    from src.api.routes.alerts import alert_to_public

    return alert_to_public(alert, is_admin=is_admin)


class AlertHub:
    """进程内WebSocket连接注册与广播。"""

    def __init__(self) -> None:
        self._clients: dict[str, set[WebSocket]] = defaultdict(set)
        #: 连接 → 是不是管理员（决定它能不能看到来源真名与链接）
        self._admin: dict[WebSocket, bool] = {}

    async def connect(self, websocket: WebSocket,
                      tenant_id: str = DEFAULT_TENANT,
                      *, is_admin: bool = False) -> None:
        await websocket.accept()
        self._clients[tenant_id].add(websocket)
        self._admin[websocket] = bool(is_admin)

    def disconnect(self, websocket: WebSocket,
                   tenant_id: str = DEFAULT_TENANT) -> None:
        self._clients[tenant_id].discard(websocket)
        self._admin.pop(websocket, None)

    def client_count(self, tenant_id: str = DEFAULT_TENANT) -> int:
        return len(self._clients.get(tenant_id, ()))

    def is_admin(self, websocket: WebSocket) -> bool:
        """该连接是否是管理员。**未登记时返回 False**（fail-closed：
        拿不准时按"非管理员"处理，少给信息总比泄源好）。"""
        return self._admin.get(websocket, False)

    async def broadcast(
        self, alert: Alert, tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """向该租户全部在线连接推送单条告警；返回成功推送的连接数。"""
        dead: list[WebSocket] = []
        sent = 0
        for websocket in list(self._clients.get(tenant_id, ())):
            # 按连接序列化（见模块 docstring：缓存一份就会把管理员视图发出去）
            payload = alert_payload(alert, is_admin=self.is_admin(websocket))
            try:
                await websocket.send_json({"type": "alert", "data": payload})
                sent += 1
            except Exception as exc:  # noqa: BLE001 对端断开/序列化失败
                logger.info("WS推送失败，清理连接: %s", brief(exc, BRIEF_TIGHT))
                dead.append(websocket)
        for websocket in dead:
            self._clients[tenant_id].discard(websocket)
            self._admin.pop(websocket, None)
        return sent

    async def heartbeat(self) -> int:
        """服务端心跳：防止反向代理回收空闲WS，顺带清理死连接。"""
        message = {"type": "heartbeat", "ts": now_iso()}
        alive = 0
        for _tenant_id, sockets in list(self._clients.items()):
            dead: list[WebSocket] = []
            for websocket in list(sockets):
                try:
                    await websocket.send_json(message)
                    alive += 1
                except Exception:  # noqa: BLE001 心跳失败即视为死连接
                    dead.append(websocket)
            for websocket in dead:
                sockets.discard(websocket)
                self._admin.pop(websocket, None)
        return alive
