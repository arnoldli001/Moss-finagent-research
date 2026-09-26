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


def alert_payload(alert: Alert, *, is_admin: bool,
                  user_id: str = "") -> dict:
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
        #: 连接 → 用户 ID。
        #:
        #: ⚠️ 推送的告警对**每个连接**都是"未读"（刚生成就推你），
        #: 但前端要判断"这条我是不是已经读过了" —— 同一个人可能在两个设备
        #: 上开着，A 设备读过的，B 设备的推送不该再算未读。
        #: 所以按连接记 user_id，序列化时带上该用户的状态。
        self._user: dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket,
                      tenant_id: str = DEFAULT_TENANT,
                      *, is_admin: bool = False,
                      user_id: str = "") -> None:
        await websocket.accept()
        self._clients[tenant_id].add(websocket)
        self._admin[websocket] = bool(is_admin)
        self._user[websocket] = str(user_id or "")

    def disconnect(self, websocket: WebSocket,
                   tenant_id: str = DEFAULT_TENANT) -> None:
        self._clients[tenant_id].discard(websocket)
        self._admin.pop(websocket, None)
        self._user.pop(websocket, None)

    def client_count(self, tenant_id: str = DEFAULT_TENANT) -> int:
        return len(self._clients.get(tenant_id, ()))

    def is_admin(self, websocket: WebSocket) -> bool:
        """该连接是否是管理员。**未登记时返回 False**（fail-closed：
        拿不准时按"非管理员"处理，少给信息总比泄源好）。"""
        return self._admin.get(websocket, False)

    def user_id(self, websocket: WebSocket) -> str:
        """该连接属于哪个用户（未登记时空串 → 退回全局行为）。"""
        return self._user.get(websocket, "")

    async def broadcast(
        self, alert: Alert, tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """向该租户全部在线连接推送单条告警；返回成功推送的连接数。"""
        dead: list[WebSocket] = []
        sent = 0
        for websocket in list(self._clients.get(tenant_id, ())):
            # 按连接序列化（见模块 docstring：缓存一份就会把管理员视图发出去）
            payload = alert_payload(alert, is_admin=self.is_admin(websocket),
                                    user_id=self.user_id(websocket))
            try:
                await websocket.send_json({"type": "alert", "data": payload})
                sent += 1
            except Exception as exc:  # noqa: BLE001 对端断开/序列化失败
                logger.info("WS推送失败，清理连接: %s", brief(exc, BRIEF_TIGHT))
                dead.append(websocket)
        for websocket in dead:
            self._clients[tenant_id].discard(websocket)
            self._admin.pop(websocket, None)
            self._user.pop(websocket, None)
        return sent

    async def notify(self, message: dict) -> int:
        """把一个**不含任何业务数据**的应用级通知推给**所有**在线连接。

        ## 为什么这个方法可以跨租户，而 `broadcast` 不行

        `broadcast` 推的是告警正文，所以必须**逐连接**脱敏（管理员看得到来源
        真名与原文链接，普通用户看不到 —— 见模块 docstring 里那次事故）。

        这里推的是"某个后台作业建好了，你该去重新拉一次"这种**信令**：
        载荷只有 `type` 与一个序号/时间戳。信令里没有内容 ⇒ 跨租户广播
        不泄漏任何东西 ⇒ 不需要按连接序列化。

        ⚠️ **这条边界必须靠纪律守住**：往这个方法的 `message` 里塞任何
        业务字段（哪怕只是标题），它就立刻退化成"把管理员的视图发给所有人"
        那条老路，而那种泄漏在代码评审里看不出来。要推内容请用 `broadcast`。

        ## 为什么与 `heartbeat` 共用一个出口

        两者都是"发给全部连接 + 清理死连接"。写成两遍的话，
        将来只给其中一个加上清理逻辑（或加上重试），另一个就成了死连接的
        永久持有者 —— 而表现是"连接数只涨不跌"，查起来毫无线索。
        """
        alive = 0
        for _tenant_id, sockets in list(self._clients.items()):
            dead: list[WebSocket] = []
            for websocket in list(sockets):
                try:
                    await websocket.send_json(message)
                    alive += 1
                except Exception:  # noqa: BLE001 发送失败即视为死连接
                    dead.append(websocket)
            for websocket in dead:
                sockets.discard(websocket)
                self._admin.pop(websocket, None)
                self._user.pop(websocket, None)
        return alive

    async def heartbeat(self) -> int:
        """服务端心跳：防止反向代理回收空闲WS，顺带清理死连接。"""
        return await self.notify({"type": "heartbeat", "ts": now_iso()})
