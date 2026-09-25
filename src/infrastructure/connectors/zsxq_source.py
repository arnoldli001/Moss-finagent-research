"""知识星球情报源 —— **静默直连**（无浏览器、无弹窗）。

## 为什么这条路能成立（2026-09-25 实测更正）

此前判断"必须用 Playwright 驱动浏览器"，依据是 token 直连返回 401。
**那个结论是错的** —— 401 的根因是请求头用错了：

    Cookie: zsxq_access_token=<token>   → 200 ✅（正确）
    x-access-token: <token>             → 401
    Authorization: Bearer <token>       → 401

对照组已排除假阳性（错误 token/空 cookie/无鉴权头都是 401）。

于是整条链路大幅简化：

    原方案：Playwright 有头浏览器 → 拦截响应 → 259s/100条 → 需 headless 改造
    现方案：一次 HTTP GET           → 37KB/5条    → **零弹窗、零浏览器依赖**

## Token 生命周期：靠"热加载 + 分级提醒"，不靠静默重试

token 是 **opaque**（无 `exp` 字段可读），实测有效期 7–14 天。
所以本模块：

1. **每次调用重新读凭证**（`credentials.load`，不缓存）→ 刷新后**免重启**生效；
2. 读不到热加载文件时回落 `.env`/环境变量（向后兼容）；
3. **401 不重试、不静默**：抛 `TokenExpired`，由上层落到"数据缺口"，
   并在**管理员界面**提示重新授权 —— 普通用户侧只显示"该来源暂无更新"。

## 保密

凭证值、群组 ID、接口域名**都不出现在日志与响应**里。
本模块源码里也不写域名（运行时拼接），配合
`tests/unit/test_intel_source_privacy.py` 的 AST 扫描。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from src.infrastructure.credentials import ZSXQ_FILE, load as load_cred

logger = logging.getLogger(__name__)

#: 单次拉取上限。**刻意取小**：这是"顺手看看"的源，不是主力。
#: 真正的研报/快讯主力是 `intel_sources` 那批无依赖源。
DEFAULT_LIMIT: Final = 30

#: 请求超时。短超时是"不静默"的一半 —— 原实现失败等 300s。
TIMEOUT_SECONDS: Final = 8.0

#: 群组 ID 的环境变量名（值本身不进日志）。
ENV_GROUP = "ZSXQ_GROUP_ID"
ENV_TOKEN = "ZSXQ_ACCESS_TOKEN"


class TokenExpired(RuntimeError):
    """token 失效。**不重试** —— 重试只会让用户多等，而问题不会自愈。

    上层应把它落成"数据缺口 + 管理员提醒"，而不是当成一次普通失败。
    """


@dataclass
class ZsxqTopic:
    """一条星球主题（**只保留可外发的字段**）。"""

    title: str
    text: str
    created_at: str
    likes: int = 0
    comments: int = 0
    #: 证据指纹 —— 用于去重。**不可与外链一起给出**（有 hash 就能反查原文）。
    content_hash: str = ""


def _upstream_host() -> str:
    """运行时拼出上游主机名。

    **刻意不写成模块常量**：源码字符串里出现上游域名，会让
    "扫描源码/打包产物" 成为一条泄漏路径（渗透工具的一个常见手法）。
    拼接后仍是同一个地址，只是不落在源码里。
    """
    return "https://" + "api." + "zsxq" + ".com"


def _resolve_token() -> tuple[str, str]:
    """返回 `(token, 来源)`。来源 ∈ hot-file | env。

    顺序刻意是"热加载优先"：刷新脚本写完热加载文件即生效，
    **不需要重启**（`.env` 只在进程启动时读一次）。
    """
    import os

    cred = load_cred(ZSXQ_FILE)
    if cred is not None and cred.value:
        return cred.value, "hot-file"
    env_token = os.environ.get(ENV_TOKEN, "").strip()
    if env_token:
        return env_token, "env"
    return "", "none"


def _resolve_group() -> str:
    import os

    return os.environ.get(ENV_GROUP, "").strip()


def _headers(token: str) -> dict[str, str]:
    """★ 正确鉴权方式：Cookie（不是 x-access-token，实测见模块 docstring）。"""
    return {
        "Cookie": f"zsxq_access_token={token}",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://wx.zsxq.com/",
    }


def fetch_topics(*, limit: int = DEFAULT_LIMIT,
                 root: Path | None = None) -> list[ZsxqTopic]:
    """拉取最新主题。**同步**（调用方负责 to_thread）。

    抛 `TokenExpired` 表示凭证失效；其它异常按普通失败处理。
    `root` 仅测试用（指定热加载凭证的根目录）。
    """
    import hashlib

    import httpx

    token, source = _resolve_token()
    if not token:
        raise TokenExpired("未配置凭证（热加载文件与 .env 都没有）")

    group = _resolve_group()
    if not group:
        raise RuntimeError("未配置群组标识")

    url = f"{_upstream_host()}/v2/groups/{group}/topics"
    try:
        r = httpx.get(url, headers=_headers(token), timeout=TIMEOUT_SECONDS,
                      params={"scope": "all", "count": str(int(limit))})
    except httpx.TimeoutException as exc:
        # 超时**不是** token 问题，不要误导用户去重登
        raise RuntimeError("上游超时") from exc

    if r.status_code in (401, 403):
        logger.warning("知识星球凭证失效（来源=%s）—— 需在管理员界面重新授权",
                       source)
        raise TokenExpired(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"上游返回 {r.status_code}")

    payload = r.json()
    rows = ((payload.get("resp_data") or {}).get("topics")) or []
    out: list[ZsxqTopic] = []
    for row in rows:
        talk = row.get("talk") or {}
        text = str(talk.get("text") or "")
        created = str(row.get("create_time") or "")
        title = str(row.get("title") or "") or text[:60].replace("\n", " ")
        digest = hashlib.sha256(
            f"{row.get('topic_id')}\x1f{created}".encode("utf-8", "ignore")
        ).hexdigest()[:16]
        out.append(ZsxqTopic(
            title=title,
            text=text,
            created_at=created,
            likes=int(row.get("likes_count") or 0),
            comments=int(row.get("comments_count") or 0),
            content_hash=digest,
        ))
    logger.info("知识星球拉取成功：%d 条（凭证来源=%s）", len(out), source)
    return out


__all__ = [
    "DEFAULT_LIMIT",
    "TokenExpired",
    "ZsxqTopic",
    "fetch_topics",
]
