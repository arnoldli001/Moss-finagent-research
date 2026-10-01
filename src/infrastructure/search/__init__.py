"""联网搜索能力（外部源）。

## 两家搜索源 + 一个路由

| 模块 | 角色 | 额度 | 凭据 |
|---|---|---|---|
| `bocha.py` | **主**（`PROVIDER_ORDER[0]`） | 一次性**总量 1000** 次 | `bocha_search` |
| `baidu.py` | **备** | **每天 100** 次 | `baidusearch` |
| `router.py` | **主挂了走备**（R2 的落点：备用的定义是"真会被调用"） | —— | —— |

生产入口是 **`router.web_search()`**（`source_reroute.discover_candidate_urls()`
用的就是它）。两家 provider 的 `web_search()` 是**单源**入口，主要供测试与排查用。

⚠️ 接线纪律：搜索**花钱且慢**（主 15s + 备 20s 最坏），只允许**后台路径**调用
（换源 / 补采），**绝不挂交互路径**（交互预算 10s，见 `core.intel_limits`）。
"""
from __future__ import annotations

from src.infrastructure.search import baidu, bocha, router  # noqa: F401
from src.infrastructure.search.bocha import (  # noqa: F401
    MAX_CALLS_PER_DAY,
    MAX_CALLS_TOTAL,
    SearchHit,
    SearchOutcome,
)
from src.infrastructure.search.router import (  # noqa: F401
    PROVIDER_ORDER,
    providers_status,
    web_search,
)

__all__ = [
    "MAX_CALLS_PER_DAY",
    "MAX_CALLS_TOTAL",
    "PROVIDER_ORDER",
    "SearchHit",
    "SearchOutcome",
    "baidu",
    "bocha",
    "providers_status",
    "router",
    "web_search",
]
