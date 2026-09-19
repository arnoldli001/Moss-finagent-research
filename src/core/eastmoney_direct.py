"""东财 push2* API 的 **SNI 阻断**规避（本机网络实测 2026-09-17）。

## 现象与证据（照抄可复现）

| 观测 | 结果 |
|---|---|
| DNS 解析 `push2his.eastmoney.com` | 正常（多个 CDN IPv4 + IPv6） |
| TCP 连 443 | 正常（全部 IP 都通） |
| `https://push2his.eastmoney.com/api/...` | ❌ `RemoteDisconnected`（握手后立刻断） |
| `https://<同一个IP>/api/...` + `Host: push2his.eastmoney.com` | ✅ HTTP 200 |
| `datacenter-web.eastmoney.com` / `quote.eastmoney.com`（非 push2 主机） | ✅ 正常 |
| 加浏览器 UA / Referer / `ut` token / IPv4-only / 直连 IP 但 SNI 仍是域名 | ❌ 全部无效 |
| 控制变量（同 IP、同参数、同 UA，只改 URL 里写域名还是写 IP） | 域名=断，IP=通 |

**结论**：链路中间设备按 **TLS SNI** 匹配 `push2*.eastmoney.com`（证书里其实含
`*.push2his.eastmoney.com`，证书本身没问题），握手后立刻断链。TCP 与 DNS 都正常，
所以**不是**网络不通、**不是**反爬 headers、**不是**限频（实测同一 IP 连发多次也都 200）。

这解释了此前一连串"东财 akshare 全挂"的现象：
`stock_individual_fund_flow` / `stock_*_fund_flow_hist` / `stock_board_*_cons_em` /
`stock_zh_a_hist_min_em` 用的是 `push2his`/`push2`，全体失败；而
`stock_board_concept_info_ths`（同花顺 `d.10jqka.com.cn`）与腾讯 `web.ifzq.gtimg.cn`
一直正常 —— 与实现无关，纯网络侧。

## 规避方式（本模块做的事）—— **默认关闭**，按需开启

为什么默认关闭：实测这层阻断**按时间窗反复**（几分钟前"IP 直连"还能 200，
同一 IP 过一会儿也全断），也就是说**不存在稳定绕过**。给一个必然失败的请求
额外加"探测 + 重试"只会让失败更慢，因此机制做成显式开关：

```powershell
$env:MOSS_EM_DIRECT="1"   # 开启规避（默认关闭）
```

开启后的行为：

1. 正常发请求（域名、证书校验全开，与原来完全一致）；
2. 只有出现 SNI 阻断特征（`RemoteDisconnected` / 空回复 / `SSLError` 空回复）才：
   - 解析该域名的 IPv4，用**同一次真实请求**逐个探测哪个 IP 可用
     （不是打根路径 —— 实测有节点根路径 404 正常、API 路径被断）；
   - 改用 `https://<IP><path>`，`Host` 头设回真实域名，`verify=False`
     **仅在这一步**生效；
3. 探测不到可用 IP 就保持失败 —— **不伪造数据、不静默降级**。

`probe_report()` 会给出"哪个域名试过哪些 IP、结果如何"，可直接用于排障展示。

## 代价与边界（必须写清楚）

- `verify=False` 意味着**这一步不校验证书**：链路仍是 TLS 加密，但不再防中间人。
  被阻断的主机上没有可用的"既校验证书又是正确 SNI"的组合（实测：任何含
  `eastmoney.com` 的 SNI 都会被断），因此这是二选一。
- 只影响 `eastmoney` 域；其它数据源（Tushare / 同花顺 / 腾讯）**完全不走这里**。
- 项目主链路（做T、资金流监控）主力数据源本来就是 Tushare/同花顺/本地仓库，
  东财只是备用口径之一，因此本机制是"锦上添花"，缺了也不影响功能。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# SNI 阻断的典型错误文本（requests/urllib3 会把不同失败包成下面这些）
_BLOCK_HINTS = (
    "RemoteDisconnected",
    "Connection aborted",
    "Connection reset",
    "Max retries exceeded",
    "EOF occurred in violation of protocol",
    "UNEXPECTED_EOF_WHILE_READING",
)

_enabled_flag = os.environ.get("MOSS_EM_DIRECT", "0").strip().lower() in (
    "1", "true", "yes", "on")

# 每个域名 → 探测结果：{"ip": 可用IP或None, "candidates": [...], "at": 时间}
_probe_result: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
# 单个请求内最多试几个 IP：CDN 节点质量参差（实测有的 IP 通、有的握手即断），
# 但也不能无限试 —— 每个 IP 都要付一次超时成本。
MAX_IP_TRIES = 2
# 探测单个 IP 的超时（秒）。用**真实请求**探测，所以要短。
PROBE_TIMEOUT = 6.0


def enabled() -> bool:
    return _enabled_flag


def _is_eastmoney(host: str) -> bool:
    return host.endswith("eastmoney.com")


def _looks_blocked(exc: BaseException | None) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(hint in text for hint in _BLOCK_HINTS)


def ipv4_addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:  # noqa: BLE001 DNS 失败就没什么可试的
        return []
    seen: list[str] = []
    for item in infos:
        if item[0] != socket.AF_INET:
            continue
        address = item[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def probe_report() -> dict[str, Any]:
    """已探测的域名 → 结果（供排障/接口展示；不参与取数）。"""
    with _lock:
        return {host: dict(value) for host, value in _probe_result.items()}


def _direct_url(parts: Any, address: str) -> str:
    return urlunsplit((parts.scheme, address + (
        f":{parts.port}" if parts.port else ""), parts.path,
        parts.query, parts.fragment))


def _direct_call(session: Any, method: str, parts: Any, address: str,
                 headers: dict[str, str], kwargs: dict[str, Any]) -> Any:
    """用直连 IP 发一次（verify=False —— 见模块 docstring 的边界说明）。"""
    return session.request(method, _direct_url(parts, address), headers=headers,
                           verify=False, **kwargs)


def _resolve_direct_ip(session: Any, method: str, parts: Any,
                       kwargs: dict[str, Any]) -> str | None:
    """找出该域名下**能真正跑通这次请求**的 IP（缓存在进程内）。

    关键设计：探测用的是**同一次真实请求**（同 method/params/headers），而不是
    打根路径 —— 实测有的 CDN 节点根路径 404 正常、但 API 路径会被断，
    用根路径探测会误判成"这个 IP 可用"。
    """
    host = parts.hostname or ""
    with _lock:
        cached = _probe_result.get(host)
    if cached is not None and cached.get("ip"):
        return str(cached["ip"])
    candidates = ipv4_addresses(host)
    headers = {**(kwargs.get("headers") or {}), "Host": host}
    probe_kwargs = {key: value for key, value in kwargs.items() if key != "headers"}
    probe_kwargs.pop("verify", None)
    probe_kwargs["timeout"] = min(PROBE_TIMEOUT, float(probe_kwargs.get("timeout") or 8))
    found: str | None = None
    for address in candidates[:MAX_IP_TRIES]:
        try:
            response = _direct_call(session, method, parts, address, headers,
                                    probe_kwargs)
        except Exception:  # noqa: BLE001 这个 IP 不行，换下一个
            continue
        if response.status_code < 500:
            found = address
            break
    with _lock:
        _probe_result[host] = {"ip": found, "candidates": candidates,
                               "at": time.time()}
    if found:
        logger.info("东财 SNI 阻断规避：%s 经直连 %s 可用（候选 %s）",
                    host, found, candidates)
    else:
        logger.warning("东财 SNI 阻断规避：%s 无可用 IP（候选 %s），保持失败（不伪造数据）",
                       host, candidates)
    return found


class EastmoneyDirectSession:
    """基类：只提供"被阻断时回退直连 IP"的 `request` 实现。

    真正安装的是 `install()` 里动态合成的 `DirectSession(EastmoneyDirectSession,
    requests.Session)` —— 继承原生 Session，因此 `isinstance` 与上下文管理都正常；
    这里不直接继承是为了避免在模块导入期就绑定 `requests.Session`（那样
    `install()` 的替换会作用到错误的目标）。
    """

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return super().request(method, url, **kwargs)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 只有"像是被阻断"才回退
            parts = urlsplit(url)
            host = parts.hostname or ""
            if not (_enabled_flag and _is_eastmoney(host) and _looks_blocked(exc)):
                raise
            address = _resolve_direct_ip(self, method, parts, kwargs)
            if not address:
                raise
            headers = {**(kwargs.pop("headers", None) or {}), "Host": host}
            kwargs.pop("verify", None)      # 必须覆盖：直连 IP 时证书名不匹配
            try:
                return _direct_call(self, method, parts, address, headers, kwargs)
            except Exception as direct_exc:  # noqa: BLE001 首选 IP 这次不行
                if not _looks_blocked(direct_exc):
                    raise
                # 缓存作废，下次请求重新探测；本次如实抛出（不伪造数据）
                with _lock:
                    _probe_result.pop(host, None)
                raise direct_exc from direct_exc


@lru_cache(maxsize=1)
def _flagged_note() -> str:
    return ("东财 push2* 在本机网络被按 TLS SNI 周期性阻断（域名请求握手后即断，"
            "IP 直连时通时断），已启用「直连 IP + Host 头」回退（MOSS_EM_DIRECT=0 关闭）")


def install() -> bool:
    """把 `requests.Session` 换成上面的壳（幂等）。

    用**模块级替换**而不是让每个调用点自己 new 一个：东财调用的发起者大多是
    akshare 内部代码（例如 `stock_individual_fund_flow` 直接 `requests.get`），
    我们改不到它们的调用点 —— 只能换掉它们会用到的那层。

    壳类继承原生 `Session`，因此 `requests.get/post` 内部的
    `with sessions.Session() as session:`、`isinstance` 判断都照常工作；
    模块级 `requests.request(...)` 不经过 `Session`，保持原生行为（那部分不是
    东财取数的入口，没有必要改动）。
    """
    import requests

    if not _enabled_flag:
        logger.info(
            "东财直连回退未启用（默认关闭；被 SNI 阻断时可用 MOSS_EM_DIRECT=1 打开）")
        return False
    if getattr(requests.Session, "_moss_em_direct", False):
        return False

    class DirectSession(EastmoneyDirectSession, requests.Session):
        _moss_em_direct = True

    DirectSession.__name__ = "Session"
    DirectSession.__qualname__ = "Session"
    requests.Session = DirectSession  # type: ignore[assignment,misc]
    logger.info(_flagged_note())
    return True
