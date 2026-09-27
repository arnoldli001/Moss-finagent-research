"""**客户端真实 IP 的唯一解析入口**（代理链感知）。

## 为什么必须只有一份实现

原来 `_client_ip` 在**三个文件里各写了一遍**：

    src/api/login_gate.py      登录门槛的审计与拦截日志
    src/api/routes/auth.py     登录/注册/找回的 IP 限流与审计
    src/api/routes/admin.py    管理操作的审计留痕

三份都在读 `CF-Connecting-IP`，都在缺值时退回 `request.client.host`。
它们**已经开始分叉**（`login_gate` 那份额外处理了 `client` 为 None 的情况），
而"客户端 IP"是**安全判据**：限流计数、图形码判定、审计追溯全靠它。
三份实现里任何一份走偏，症状都是"某个接口的限流莫名失效" ——
不会有报错，只会有人能多试几次密码。

## 两种部署形态（这是本模块存在的第二个理由）

    ① Cloudflare Tunnel（现状，保留备用）
       浏览器 → CF 边缘 → cloudflared → 127.0.0.1:8110
       CF 会**覆盖** `CF-Connecting-IP`，那份是可信的。

    ② 香港 VPS + frp + nginx（2026-09-26 新增）
       浏览器 → VPS nginx(终止 TLS) → frps → frpc → 127.0.0.1:8110
       nginx 添 `X-Forwarded-For`，**没有** `CF-Connecting-IP`。

形态②下如果还只认 `CF-Connecting-IP`，所有用户都会退化成
`request.client.host` = `127.0.0.1` —— 也就是**所有人共用一个 IP 计数**：
登录限流形同虚设、图形码永远不会触发、审计日志里全是本机地址。

## 安全前提（**别把这段删掉**）

`X-Forwarded-For` 是**客户端可以自己伪造**的请求头。所以：

    ★ 只有当"直连我们的那一跳"是**可信代理**时，才采信 XFF。

"直连我们的那一跳"就是 `request.client.host`。在本项目的部署里它是：

    · `127.0.0.1` —— cloudflared / frpc 都在本机
    · 可信代理清单里的地址（`MOSS_TRUSTED_PROXIES`）

不满足就**一律退回** `request.client.host`：让"看起来像"的伪造请求
只是损失一个真实 IP，而不是获得一个假身份。

同理 `CF-Connecting-IP` **只在公网环境采信**（`is_public`：prod/pilot）——
那才是"请求必然经过 CF 边缘"的前提。本地 dev 直连时采信它，
等于给本机任意程序一个伪造 IP 的口子。

## XFF 怎么取（从右往左，取第一个不可信的）

`X-Forwarded-For: 客户端, 代理1, 代理2` 是**追加**语义：越靠左越"早"、
越靠右越"近"。左侧部分完全由客户端控制，**不能信**。

所以从**最右**开始往左走，跳过所有可信代理，停在第一个不可信的地址 ——
那才是链路上第一个真实来源。反向取（取最左那个"客户端声称的"）等于
直接把伪造权交给攻击者。
"""

from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)

#: 不可信时的兜底（拿不到任何地址）。
UNKNOWN_IP = "unknown"

#: 本机地址。cloudflared 与 frpc 都把请求从本机发过来，所以它天然可信。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})


def trusted_proxies() -> frozenset[str]:
    """可信代理地址集合（配置项 `MOSS_TRUSTED_PROXIES`，逗号分隔）。

    读取失败时**只回退到 loopback**（保守方向）：宁可少采信一个真实 IP，
    也不要因为配置读不到而放行伪造。这与"脱敏失败按更严那一边处理"同一原则。
    """
    try:
        from src.core.config import get_settings

        raw = str(getattr(get_settings(), "trusted_proxies", "") or "")
    except Exception:  # noqa: BLE001 配置读不到不该让取 IP 失败
        raw = ""
    extra = {p.strip() for p in raw.split(",") if p.strip()}
    return _LOOPBACK | extra


def client_ip(request: Request) -> str:
    """真实客户端 IP。**全项目唯一的实现**，三处调用点都指到这里。"""
    peer = request.client.host if request.client else ""
    trusted = trusted_proxies()
    # 直连我们的那一跳不可信 → 它说的任何"我是谁"都不作数
    if peer not in trusted:
        return peer or UNKNOWN_IP

    # ① Cloudflare Tunnel：CF 边缘**覆盖**这个头，可信（仅公网环境）
    try:
        from src.core.config import get_settings

        if get_settings().is_public:
            cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
            if cf_ip:
                return cf_ip
    except Exception:  # noqa: BLE001 配置读不到就往下走 XFF 分支
        pass

    # ② nginx 反代（香港方案）：从右往左取第一个不可信地址
    xff = request.headers.get("X-Forwarded-For") or ""
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(hops):
            if hop not in trusted:
                return hop
    # ③ 都没有 → 直连peer（在可信链可信的前提下，它是最后一个代理）
    return peer or UNKNOWN_IP


__all__ = ["UNKNOWN_IP", "client_ip", "trusted_proxies"]
