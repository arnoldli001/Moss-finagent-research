"""四跳取数的**跳级命中计数**（单一事实源）—— 回答「这次是第几跳答出来的」。

## 为什么必须存在（这是可观测性缺口，不是洁癖）

`src/orchestration/supervisor.py` 的 `query_data_for_agent()` 是一条**四跳优先级链**
（① 本次已采集的 `validated_points` → ② 本地库 → ③ 连接器 → ④ 联网兜底），
设计原则是「每一跳都只在**上一跳真的没给出数据**时才发生；顺序即策略」。

在此之前**没有任何埋点记录"这次是第几跳答出来的"**，于是两个指标**给不出来**：

    · 本地命中率（第 1/2 跳答出来的比例）
    · 联网兜底触发率（第 4 跳被走到的比例）

一个只讲「我们有四跳」却答不出「每跳命中多少」的系统，等于没有量化依据 ——
四跳的**顺序**（也就是策略本体）改对了还是改坏了，没有任何数据能证明。
本模块就是那两个分母/分子的唯一来源。

## 为什么是"模块级单例 + 全局计数"而不是把计数器挂在调用方

同样的理由写在 `src/orchestration/supervisor.py` 那段注释里：同一条判据/同一个
计数写在多处，必然有一处被漏改（本项目实测过 `describe()` 零个生产调用方、
以及"同一个 key 写在 3 处只改一处 ⇒ 情报 5 个端点对所有人 403"）。
所以全仓库**只有这一份**跳级计数，`bump()` 是唯一写入口、`snapshot()` 是唯一读出口。

## 为什么把「四跳都没给数据」也当成一个计数项

它不是任何一跳的命中，而是**缺口**。把它混进某一跳（例如记进 `hop4_network`）
会让"联网兜底触发了 N 次"与"联网兜底答出来了 N 次"在数上分不开 ——
而这两件事的下一步动作完全不同：前者说明**本地覆盖不足**，
后者说明**连兜底都拿不到**（该去看源/口径，而不是补库）。

## 空态：「还没量到」与「量到 0」必须分得开

`counters` 里一律是整数（0 是有意义的数，且方便求比例）；
调用方要区分「冷启动、一次都没量到」与「量到了、确实是 0」时读 `latest_hop`：
`UNMEASURED`（=「未量到」，与 `network_fallback.UNMEASURED` **同一个常量**，
不另写一份字面量）= 还没量到；某个 `hop*` 常量 = 最近一次由那一跳答出来。
`UNMEASURED_HOPS` 是"没有一次走完全部四跳"的那个计数的键名（**不含 hop 前缀**，
免得被误读成"第五跳"）。

## 线程/协程安全：`threading.Lock`（选定，不是"顺手"）

选择 `threading.Lock` 而不是"依赖字典的原子性"，三个理由：

1. **一致性而不是单键原子性**：`snapshot()` 要**同时**读 5 个计数与 `latest_hop`。
   CPython 的 `dict` 单键读写在 GIL 下是原子的，但"一组值凑成一个自洽快照"
   不是 —— 读的过程中夹进一次 `bump()`，就会得到「total 涨了、某一跳没涨」
   这种自相矛盾的快照，而它看起来完全正常（本项目最贵的那类缺陷）。
2. **同一份判据要跨线程成立**：`bump()` 会在事件循环里被调用
   （`query_data_for_agent` 是协程），而 `/health` 的读取要经过
   `run_infra(...)` 的**线程池**（见 `src/core/executors.py`）。协程之间不需要锁，
   但它们与线程池之间需要内存可见性保证。
3. **成本**：`bump()` 只是 5 个整数自增，锁的代价（无争用时约 0.1 µs 级）
   相对一次取数（最小的一跳也是内存过滤，量级 µs~ms）可忽略；
   它不在任何"每次请求跑几万次"的热循环里（一次 `query_data` 只 bump 一次）。

## 顺手可得的两个派生量（`total` / `latest_hop`）

`total` = 四跳路径走过多少次（**不是** HTTP 请求数：A17 一次工具调用 = 一次）；
有了它，"命中率"的分母不必让调用方自己把几项加起来（自己加必然有人加漏——
把 `misses` 忘了就是一个偏高的命中率）。`latest_hop` 见上。

## 第三跳**内部**的子路径明细（`connector_subpaths`，2026-09-30 接上）

本模块只回答「哪一跳答出来的」。第三跳（`ConnectorRouter`）内部还是一条三级短路
（TTL 缓存 → 本地库 → 真联网），**同一次 `hop3_connector` 的代价可以差三个数量级**
（毫秒级的缓存命中 vs 十几秒的联网）——「本地命中率」「为什么这次联网了」
此前**一个数都没有**（缺口由 `src/domain/agents/data/collector/path_stats.py`
的「诚实边界」一节登记并点名修法：由 `ConnectorRouter` 自报子路径）。

现在那份自报随 `snapshot()` 的 `connector_subpaths` 一起出去（唯一事实源是
`src/infrastructure/connectors/subpath_stats.py`）。三点边界写清楚：

1. ⚠️ **两个 total 不是一回事，不许相加**：本模块的 `total` 是四跳链的决策次数
   （`query_data_for_agent` 每次调用算一次），子路径的 `total` 是
   `ConnectorRouter.fetch()` 的调用次数 —— 而绝大多数 fetch（分时面板、回测 API、
   定时作业、A17 工具）**根本不走四跳链**。混算出来的"命中率"不是任何一个链路的。
2. 子路径只接在**既有读出口**上（`/health` 的 `query_data_hops` 已经在读这个
   `snapshot()`）：不新开端点、不新增往返 —— `/health` 是 20 秒轮询的热路径。
3. 两段**各自降级**：子路径计数坏了只让 `connector_subpaths.available=False`，
   本模块那四跳计数照常给（反之亦然）。读不到时**绝不给 0**。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Final

#: 「没量到」的唯一说法：与 `network_fallback.UNMEASURED` **共用同一个常量**。
#:
#: 为什么 import 而不是再写一遍字面量：本项目的硬约束是
#: 「读不到数据时显示『未量到/无法统计』，绝不用 0 糊过去」，而这条判据
#: 在链路结果、`/health`、拒绝理由里会各自出现 —— 字面量写歪一个
#: （「无数据」/「暂缺」），下游的 `in` 判据就**静默不命中**。
#: 方向上也成立：`network_fallback` 本来就 `import src.core.budget`，
#: 这里 `core → infrastructure.catalog` 不构成新的环（`hop_stats` 不 import supervisor）。
from src.infrastructure.catalog.network_fallback import UNMEASURED

#: 第 1 跳：本次已采集的 `validated_points`（**内存**，最便宜的一跳）。
HOP1_VALIDATED: Final[str] = "hop1_validated"

#: 第 2 跳：本地库 / 列索引（`LocalDataExecutor.metric_series`）。
HOP2_LOCAL: Final[str] = "hop2_local"

#: 第 3 跳：连接器链（A01 的 `ConnectorRouter`）。
HOP3_CONNECTOR: Final[str] = "hop3_connector"

#: 第 4 跳：联网兜底（`network_fallback`，默认白名单为空 ⇒ fail-closed）。
HOP4_NETWORK: Final[str] = "hop4_network"

#: 四跳走完、**一次都没给出数据**（缺口，不是命中）。
#:
#: ⚠️ 判据是"走到了第四跳且 `measured=False`"，**不是**"四跳都返回了空字符串" ——
#: 第四跳的"没量到"有 5 种原因（护栏拒绝 / 源上也没有 / 取数实现未装配 / 异常 /
#: 一个带数值的点都没有），它们都必须计入缺口，否则缺口率会被算小。
HOP_NONE: Final[str] = "misses"

#: 计数键的**稳定顺序**（`/health` 的渲染顺序、文档、断言都读它，不各写一份）。
HOP_KINDS: Final[tuple[str, ...]] = (
    HOP1_VALIDATED, HOP2_LOCAL, HOP3_CONNECTOR, HOP4_NETWORK, HOP_NONE,
)

#: `latest_hop` 的初值：**还没量到**（不是 0、也不是任何一跳）。
UNMEASURED_HOPS: Final[str] = UNMEASURED


@dataclass
class HopStats:
    """跳级计数的可变状态（**唯一实例**是下面的 `_STATS`）。"""

    #: 每一跳"由它答出来"的次数；`HOP_NONE` 是缺口那一项。初值全 0。
    counters: dict[str, int]
    #: 最近一次由哪一跳答出来（`UNMEASURED_HOPS` = 还没量到）。
    latest_hop: str = UNMEASURED_HOPS
    #: 四跳路径被走过的总次数（含缺口）—— 命中率的分母由它给，不由调用方相加。
    total: int = 0


#: 进程级状态（单一事实源）。锁保护的是**整组值**的一致性，不只是单键。
_STATS: Final[HopStats] = HopStats(counters={k: 0 for k in HOP_KINDS})
_LOCK: Final[threading.Lock] = threading.Lock()


def bump(hop: str) -> None:
    """记一次"由 `hop` 这一跳答出来"（`HOP_NONE` = 四跳都没给数据）。

    **必须在真正决定由哪一跳返回的那一处调用**（`query_data_for_agent`）。
    挂在只有测试才走的辅助函数上等于没接 —— 本项目实测过这种形状
    （`describe()` 判据接在没人走的路上，零个生产调用方，而且不报错）。

    未知的 `hop` 会 `KeyError`（**不是**静默忽略）：静默忽略会让一个拼错的
    打点参数表现成"这一跳永远 0 命中"，而那读起来正好是"这一跳没用"。
    """
    with _LOCK:
        _STATS.counters[hop] += 1
        _STATS.latest_hop = hop
        _STATS.total += 1


def connector_subpaths() -> dict[str, Any]:
    """第三跳（`ConnectorRouter`）**内部**的子路径明细 —— 回答「第三跳这次走的是哪条近路」。

    ## 为什么挂在这里（而不是新开端点 / 新加一个读口）

    `hop_stats` 只回答「哪一跳答出来的」；第三跳内部还有一条三级短路
    （TTL 缓存 → 本地库 → 真联网），那次服务的成本与原因都不同 ——
    「本地命中率」「为什么这次联网了」此前**一个数都没有**
    （缺口登记在 `src/domain/agents/data/collector/path_stats.py` 的「诚实边界」）。
    唯一事实源是 `src/infrastructure/connectors/subpath_stats.py`；这里只是**转出去**。

    挂在这一处而不是 `/health` 里新加一段，是因为 `_query_data_hops()` 已经在读
    `snapshot()`：把它带进这份快照 ⇒ **`src/api/routes/research.py` 一行都不用改**，
    而「第三跳的子路径」正好就该显示在第三跳那一项的旁边（同一个聚合层级）。
    ⚠️ 两个 total **不是一回事**，别相加：本模块的 `total` 是四跳链的决策次数，
    子路径的 `total` 是 `ConnectorRouter.fetch()` 的调用次数（绝大多数 fetch
    根本不走四跳链）。

    **绝不抛**：`/health` 是前端 20 秒轮询的热路径，子路径计数坏了不该把整张
    健康检查（连同四跳那份**没问题**的计数）一起打挂 —— 所以只降级这一段，
    并且**不给 0**：读不到时 `available: False`，全 0 会被读成"一次都没联网"（假绿）。
    """
    try:
        from src.infrastructure.connectors import subpath_stats

        return {"available": True, **subpath_stats.snapshot()}
    except Exception as exc:  # noqa: BLE001 健康检查不能因此崩
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def snapshot() -> dict[str, Any]:
    """只读快照（`/health` 与运维页读它）。

    **绝不抛、绝不改状态**：`/health` 是前端 20 秒轮询的热路径，
    计数器出问题不该把整张健康检查打挂（同 `_data_sources` 那几段的纪律）。

    返回的 `counters` 是**副本**：调用方改它不会污染真计数
    （本项目实测过"返回内部 dict，调用方清一下就把计数清了"那类自伤）。

    `connector_subpaths` 是第三跳内部的子路径明细（见 `connector_subpaths()`）：
    它与四跳计数**同源同出口**，所以 `/health` 那边不需要为它加任何一行。
    """
    with _LOCK:
        snap = {
            "counters": dict(_STATS.counters),
            "latest_hop": _STATS.latest_hop,
            "total": _STATS.total,
            "kinds": list(HOP_KINDS),
            "unmeasured": UNMEASURED_HOPS,
        }
    # ⚠️ 在锁**外**拼：子路径快照有自己的锁（`subpath_stats._LOCK`），
    # 两把锁不许嵌套持有 —— 那会造出一个只在这个组合下才出现的死锁面。
    snap["connector_subpaths"] = connector_subpaths()
    return snap


def reset_for_test() -> None:
    """把计数清回冷启动态（**只给测试用**；生产代码里不许出现调用）。"""
    with _LOCK:
        for key in HOP_KINDS:
            _STATS.counters[key] = 0
        _STATS.latest_hop = UNMEASURED_HOPS
        _STATS.total = 0


__all__ = [
    "HOP1_VALIDATED",
    "HOP2_LOCAL",
    "HOP3_CONNECTOR",
    "HOP4_NETWORK",
    "HOP_KINDS",
    "HOP_NONE",
    "UNMEASURED",
    "UNMEASURED_HOPS",
    "HopStats",
    "bump",
    "connector_subpaths",
    "reset_for_test",
    "snapshot",
]
