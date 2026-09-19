"""WebSocket告警实时推送中心（FR-10）。

租户隔离：tenant_id → 连接集合；广播失败自动清理死连接；
离线不补发（客户端上线后主动拉 REST 列表）。
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


class AlertHub:
    """进程内WebSocket连接注册与广播。"""

    def __init__(self) -> None:
        self._clients: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, websocket: WebSocket,
                      tenant_id: str = DEFAULT_TENANT) -> None:
        await websocket.accept()
        self._clients[tenant_id].add(websocket)

    def disconnect(self, websocket: WebSocket,
                   tenant_id: str = DEFAULT_TENANT) -> None:
        self._clients[tenant_id].discard(websocket)

    def client_count(self, tenant_id: str = DEFAULT_TENANT) -> int:
        return len(self._clients.get(tenant_id, ()))

    async def broadcast(
        self, alert: Alert, tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """向该租户全部在线连接推送单条告警；返回成功推送的连接数。"""
        message = {"type": "alert", "data": alert.model_dump()}
        dead: list[WebSocket] = []
        sent = 0
        for websocket in list(self._clients.get(tenant_id, ())):
            try:
                await websocket.send_json(message)
                sent += 1
            except Exception as exc:  # noqa: BLE001 对端断开/序列化失败
                logger.info("WS推送失败，清理连接: %s", brief(exc, BRIEF_TIGHT))
                dead.append(websocket)
        for websocket in dead:
            self._clients[tenant_id].discard(websocket)
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
        return alive
