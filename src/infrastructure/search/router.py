"""搜索源路由：**主源挂了自动走备用**（`CHG-0117`）。

## 它解决什么

用户原话：「**也可以联网查询获取两个数据源，作为备用**」。
单有第二个客户端**不算备用** —— 备用的定义是"主用失效时它真的会被调用"。
本模块就是那句"真的会被调用"的落点，也是 R2（禁止"声明式备用"）的答案：
**谁调它？`discover_candidate_urls()` 调 `router.web_search()`，就在这里。**

## 顺序与判据

* 顺序：`PROVIDER_ORDER`（博查 → 百度）。**为什么博查在前**：额度性质不同 ——
  博查是**一次性总量 1000**，百度是**每天 100**；把"日配额"放在前面会每天
  重置、更容易把备用耗光，而"总量"那个更适合当先手。
  ⚠️ 这是**可调**的，不是硬道理；真要改就改这一个常量。
* **只对"这一家自己不行了"换手**（`blocked_by ∈ {budget, no_key, config_error,
  http, transport, parse}`）。**成功即返回**，不做"两家都问一遍再合并"——
  那会**双倍花钱**，而这里的场景是"找候选网址线索"，不需要交叉验证。
* **两家的失败原因都要带出来**：`reason` 里逐家列出 `blocked_by`，
  这样"两家都没额度"与"两家都连不上"在日志里不会长得一样
  （`AGENTS.md`：没量到 ≠ 量到 0）。
* 全挂时 `blocked_by="all_failed"`，并保留逐家明细。

## 严禁在交互路径调用

两家都是网络+花钱：交互路径有 10s 防撞钟预算（`core.intel_limits`），
这里最坏是"主源超时 15s + 备源超时 20s"。**只有后台路径可以调**。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from src.infrastructure.search.bocha import SearchOutcome

logger = logging.getLogger(__name__)

#: 主 → 备。改顺序只改这一处。
PROVIDER_ORDER: tuple[str, ...] = ("bocha", "baidu")


def _provider(name: str) -> Any:
    """按名字取 provider **模块**（延迟导入：没配的那家不必被 import 进来）。"""
    if name == "bocha":
        from src.infrastructure.search import bocha

        return bocha
    if name == "baidu":
        from src.infrastructure.search import baidu

        return baidu
    raise KeyError(f"未知搜索源 {name!r}（可选：bocha / baidu）")


def web_search(query: str, *, count: int = 5,
               transport: Callable[..., tuple[int, str]] | None = None,
               providers: Sequence[str] | None = None) -> SearchOutcome:
    """主源优先，失败即换备用；返回**带 `served_by`** 的结果。"""
    order = tuple(providers or PROVIDER_ORDER)
    failures: list[str] = []

    for name in order:
        try:
            mod = _provider(name)
        except KeyError as exc:
            failures.append(f"{name}:unknown({exc})")
            continue
        out = mod.web_search(query, count=count, transport=transport)
        if out.ok:
            out.served_by = name
            if failures:
                #: 换过手就要留痕 —— 否则"备用一直在顶班"没人知道
                logger.warning("搜索源换手：%s 不可用（%s），改由 %s 服务",
                               "、".join(f.split(":")[0] for f in failures),
                               "；".join(failures), name)
            return out
        failures.append(f"{name}:{out.blocked_by or 'fail'}")

    return SearchOutcome(
        False,
        reason="全部搜索源不可用 → " + "；".join(failures),
        blocked_by="all_failed")


def providers_status() -> list[dict[str, Any]]:
    """两家各自的闸门状态（供 `/health` 与体检命令展示 —— 读得到就要显示得出来）。"""
    out: list[dict[str, Any]] = []
    for name in PROVIDER_ORDER:
        try:
            mod = _provider(name)
            st = mod.budget_state()
            st.update({"provider": name, "configured": bool(mod.api_key())})
        except Exception as exc:  # noqa: BLE001 状态查询不该把调用方打挂
            st = {"provider": name, "error": f"{type(exc).__name__}: {exc}"}
        out.append(st)
    return out


__all__ = ["PROVIDER_ORDER", "providers_status", "web_search"]
