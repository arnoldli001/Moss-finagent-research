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

### 阻断会**升级并漂移**（2026-09 二次实测，务必读完再依赖东财）

同一天晚些时候复测，可用面进一步收窄：

| 时点 | 观测 |
|---|---|
| T1 | 域名断，**IP 直连 200**（`120.79.123.125`），`akshare` 东财接口全通 |
| T2 | 域名断；`N.push2his.eastmoney.com` 这一族的 **8 个节点全部** IP 直连也断 |
| T2 | `datacenter-web.eastmoney.com`（非 push2 主机）**始终正常** |

也就是说这层阻断**不存在稳定绕过**：它能从"只断 SNI"漂移到"连 IP + Host 头一起断"。
因此本机制的定位是**尽力而为的加分项**，不是可靠数据源：

- 项目主力链路（腾讯 / Tushare / 新浪 / eltdx）**完全不依赖它**；
- 东财相关的连接器一律排在链尾，且必须能被后续源兜住；
- `probe_report()` 里 `ip=None` 就是"当前不可用"的权威判据，可用于面板展示。

这解释了此前一连串"东财 akshare 全挂"的现象：
`stock_individual_fund_flow` / `stock_*_fund_flow_hist` / `stock_board_*_cons_em` /
`stock_zh_a_hist_min_em` 用的是 `push2his`/`push2`，全体失败；而
`stock_board_concept_info_ths`（同花顺 `d.10jqka.com.cn`）与腾讯 `web.ifzq.gtimg.cn`
一直正常 —— 与实现无关，纯网络侧。

## 规避方式（本模块做的事）—— 由 `MOSS_EM_DIRECT` 控制

```powershell
$env:MOSS_EM_DIRECT="1"   # 1/true/yes/on 开启；0/空 关闭
```

> **本部署显式打开**：东财是「ETF 前复权全历史」「分钟K 长序列」免 token 的免费
> 来源，不加这层就白丢一个源。但请连同上面的「阻断会漂移」一起读 ——
> 它可能在任意时刻整段失效，所以**下游必须有自己的兜底**，不能把它当唯一来源。
> 不想用的部署把 `MOSS_EM_DIRECT` 设为 `0`，其余数据源完全不受影响。

开启后的行为：

1. 正常发请求（域名、证书校验全开，与原来完全一致）；
2. 只有出现 SNI 阻断特征（`RemoteDisconnected` / 空回复 / `SSLError` 空回复）才：
   - 解析该域名的 IPv4（**多轮解析取并集**，见 `ipv4_addresses`），用**同一次
     真实请求**逐个探测哪个 IP 可用（不是打根路径 —— 实测有节点根路径 404
     正常、API 路径被断）；
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
from pathlib import Path
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

_enabled_flag = False   # 真实值由下面的 reload_flag() 在模块末尾算出（见那里的说明）

# 每个域名 → 探测结果：{"ip": 可用IP或None, "candidates": [...], "tried": [...], "at": 时间}
_probe_result: dict[str, dict[str, Any]] = {}
# 每个域名 → **刚发真实请求失败过**的 IP 列表（见 `mark_bad`）。
# 与 `_probe_result` 分开：探测结果描述"上次探测到了什么"，
# 这个表描述"刚刚哪个节点真的不行了"，后者优先级更高。
_bad_ips: dict[str, list[str]] = {}
_lock = threading.Lock()
# 单个请求内最多试几个 IP：CDN 节点质量参差（实测有的 IP 通、有的握手即断），
# 但也不能无限试 —— 每个 IP 都要付一次超时成本。
#
# ⚠️ 实测教训（2026-09）：原来写 2，而 DNS 有时**只返回一个** IPv4
# （`117.184.38.143`，该节点握手即断）→ 探测必然失败，`probe_report()` 里
# 记为"无可用 IP"，开关打开也等于没开。东财的 CDN 有多个节点，
# 单次 `getaddrinfo` 未必给全，所以：
#   1. 候选上限抬到 4；
#   2. `ipv4_addresses` 多次解析并取并集，把被 DNS 轮转藏起来的节点找出来。
MAX_IP_TRIES = 4
# 探测单个 IP 的超时（秒）。用**真实请求**探测，所以要短。
PROBE_TIMEOUT = 6.0
# DNS 解析次数：东财用 DNS 轮转分摊负载，单次解析常常只给 1 个 A 记录。
DNS_RESOLVE_ROUNDS = 3


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _read_flag_from_dotenv() -> bool:
    """从项目根 `.env` 读 `MOSS_EM_DIRECT`（环境变量未设时的兜底）。

    ## 为什么需要（实测踩坑 2026-09-22）
    本机制原来**只读 `os.environ`**。而项目的配置习惯是把开关写进 `.env`
    （`.env.example` 就是这么写的，`pydantic-settings` 也只是把它读进
    `Settings`，**不会**注入 `os.environ`）。于是出现最难查的一类现象：
    `.env` 里明明写着 `MOSS_EM_DIRECT=1`，`install()` 却报"未启用"，
    东财接口照旧全挂 —— 配置看起来是对的，行为却是错的。

    读取规则与 `.env` 的其它消费方一致：取第一处非注释的 `键=值`，
    值去掉引号。文件不存在/不可读时按未开启处理（**不抛异常**：
    这是取数路径上的开关，读配置失败不该让整个进程起不来）。
    """
    try:
        path = Path(__file__).resolve().parents[2] / ".env"
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            if key.strip() == "MOSS_EM_DIRECT":
                return _truthy(raw.strip().strip("'\""))
    except OSError:  # pragma: no cover - 文件不可读时按关闭处理
        return False
    return False


def enabled() -> bool:
    return _enabled_flag


def reload_flag() -> bool:
    """重新读取开关（环境变量优先，其次项目 `.env`），返回是否已开启。

    单独抽出来是为了可测：`_enabled_flag` 在模块导入期就固定了，
    测试要覆盖"环境变量 vs .env"两种来源必须能重算它。
    """
    global _enabled_flag
    _enabled_flag = _truthy(os.environ.get("MOSS_EM_DIRECT")) or _read_flag_from_dotenv()
    return _enabled_flag


def _is_eastmoney(host: str) -> bool:
    return host.endswith("eastmoney.com")


def _looks_blocked(exc: BaseException | None) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(hint in text for hint in _BLOCK_HINTS)


def ipv4_addresses(host: str) -> list[str]:
    """该域名的全部 IPv4 候选（**多轮解析取并集**，去重保序）。

    为什么不止解析一次：东财用 DNS 轮转，实测单次 `getaddrinfo` 常常只返回
    **一个** A 记录（`117.184.38.143`），而那个节点正好握手即断 ——
    于是探测判定"无可用 IP"，规避机制形同虚设。多解析几轮能把轮转藏起来的
    其它节点找出来（实测可用节点 `120.79.123.125` 就是这样才出现的）。
    """
    seen: list[str] = []
    for _ in range(max(1, DNS_RESOLVE_ROUNDS)):
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except Exception:  # noqa: BLE001 DNS 失败就没什么可试的
            continue
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

    关键设计（三条，都是实测踩出来的）：

    1. 探测用的是**同一次真实请求**（同 method/params/headers），而不是打根路径
       —— 实测有的 CDN 节点根路径 404 正常、但 API 路径会被断，
       用根路径探测会误判成"这个 IP 可用"。
    2. **缓存命中后仍要校验可用性**：缓存的 IP 会随节点故障失效。原来只要缓存里
       有 `ip` 就直接返回，于是那个 IP 一旦开始握手即断，后续每次请求都拿它去撞、
       每次都失败，而 `probe_report()` 里却一直显示"可用" —— 假阳性。
       现在缓存命中时会跳过**已被本次失败标记为坏**的 IP（见 `_BAD_IPS`）。
    3. **单次解析拿不全就多解析几轮**：东财 DNS 轮转，见 `ipv4_addresses`。
    """
    host = parts.hostname or ""
    with _lock:
        cached = _probe_result.get(host)
        bad = set(_bad_ips.get(host) or ())
    if cached is not None and cached.get("ip") and str(cached["ip"]) not in bad:
        return str(cached["ip"])
    fresh = ipv4_addresses(host)
    # 已知节点全部并入候选：缓存里记录过的（可能这次 DNS 没再给）+ 本次解析到的。
    # 顺序上把**没被标记为坏**的放前面，坏节点留作最后无路可走时的尝试。
    known = list(cached.get("candidates") or ()) if cached else []
    merged: list[str] = []
    for address in (*fresh, *known):
        if address not in merged:
            merged.append(address)
    candidates = [a for a in merged if a not in bad] + [a for a in merged if a in bad]
    headers = {**(kwargs.get("headers") or {}), "Host": host}
    probe_kwargs = {key: value for key, value in kwargs.items() if key != "headers"}
    probe_kwargs.pop("verify", None)
    probe_kwargs["timeout"] = min(PROBE_TIMEOUT, float(probe_kwargs.get("timeout") or 8))
    found: str | None = None
    tried: list[str] = []
    for address in candidates[:MAX_IP_TRIES]:
        tried.append(address)
        try:
            response = _direct_call(session, method, parts, address, headers,
                                    probe_kwargs)
        except Exception:  # noqa: BLE001 这个 IP 不行，换下一个
            continue
        if response.status_code < 500:
            found = address
            break
    with _lock:
        _probe_result[host] = {"ip": found, "candidates": merged,
                               "tried": tried, "at": time.time()}
    if found:
        logger.info("东财 SNI 阻断规避：%s 经直连 %s 可用（候选 %s）",
                    host, found, merged)
    else:
        logger.warning("东财 SNI 阻断规避：%s 无可用 IP（候选 %s），保持失败（不伪造数据）",
                       host, merged)
    return found


def mark_bad(host: str, address: str) -> None:
    """把一个"刚刚真发请求失败"的直连 IP 标记为坏，迫使下次重新探测。

    由调用方（session 壳）在直连请求失败时调用。只标记、不永久封禁：
    该 IP 仍留在候选列表里（排在最后），CDN 节点恢复后会被再次选中。
    """
    with _lock:
        bucket = _bad_ips.setdefault(host, [])
        if address not in bucket:
            bucket.append(address)
            # 上限防无界增长：CDN 节点数量有限，超出的丢掉最旧的
            if len(bucket) > 8:
                bucket.pop(0)


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
                # 这个节点真的不行了：标记它，让下次探测绕开（**不能**只清
                # `_probe_result` —— 清了之后重新探测仍可能选中同一个坏节点，
                # 因为 DNS 轮转常常只给这一个 IP，于是每次都撞同一面墙）。
                mark_bad(host, address)
                with _lock:
                    _probe_result.pop(host, None)
                raise direct_exc from direct_exc


@lru_cache(maxsize=1)
def _flagged_note() -> str:
    return ("东财 push2* 在本机网络被按 TLS SNI 周期性阻断（域名请求握手后即断，"
            "IP 直连时通时断），已启用「直连 IP + Host 头」回退（MOSS_EM_DIRECT=0 关闭）")


def install() -> bool:
    """把 `requests.Session` **与模块级 `requests.get/post`** 换成上面的壳（幂等）。

    ## 为什么必须连模块级函数一起换（2026-09 修复）
    原来只替换了 `requests.Session`，理由是"akshare 都走 Session"。**实测不成立**：
    akshare 的东财接口绝大多数直接调模块级 `requests.get` ——

        stock_zh_a_hist_min_em   → requests.get(...)
        stock_zh_a_hist          → requests.get(...)
        fund_etf_hist_em         → requests.get(...)
        stock_zh_a_spot_em       → fetch_paginated_data → request_with_retry → requests.get(...)

    模块级 `requests.get` **不经过 Session 类**，所以壳根本没生效：打开开关后
    实测 `probe_report()` 仍是空的 `{}`（说明一次探测都没发生），
    东财分钟接口照样 `RemoteDisconnected`。补上这一层后，同一台机器实测：

        ak.stock_zh_a_hist(600036, qfq)        290ms  176 行
        ak.stock_zh_a_hist_min_em(600036, '5') 315ms 1488 行
        ak.fund_etf_hist_em(510300, qfq)       300ms 1631 行（2020 至今）

    ## 实现方式
    模块级函数只是 `with sessions.Session() as s: return s.request(...)` 的薄包装
    （见 requests/api.py），所以这里把它重定向到 ShellSession 即可复用同一套
    "被阻断才回退直连 IP"的逻辑，不必复制一份。

    ## 为什么用**模块级替换**而不是让每个调用点自己 new 一个
    东财调用的发起者大多是 akshare 内部代码，我们改不到它们的调用点 ——
    只能换掉它们会用到的那层。
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

    # 模块级 get/post 也必须换（见 docstring 的实测记录）。
    # 这里显式用 `_session_cls` 绑定**已经被换掉的那个类**，
    # 否则一旦有人先 import 了 requests.api 就会写回原生 Session。
    _session_cls = DirectSession

    def _module_get(url: str, params: Any = None, **kwargs: Any) -> Any:
        with _session_cls() as session:
            return session.get(url, params=params, **kwargs)

    def _module_post(url: str, data: Any = None, json: Any = None,
                     **kwargs: Any) -> Any:
        with _session_cls() as session:
            return session.post(url, data=data, json=json, **kwargs)

    _module_get.__name__ = "get"
    _module_post.__name__ = "post"
    requests.get = _module_get    # type: ignore[assignment]
    requests.post = _module_post  # type: ignore[assignment]
    requests.api.get = _module_get    # type: ignore[assignment]
    requests.api.post = _module_post  # type: ignore[assignment]

    logger.info(_flagged_note())
    return True


# 模块导入期的最终开关值。放在文件末尾是**必须**的：
# `reload_flag()` 依赖 `_read_flag_from_dotenv`，而后者定义在文件中段 ——
# 如果在文件头部就算 `_enabled_flag`，会撞 NameError（Python 是顺序执行的）。
reload_flag()
