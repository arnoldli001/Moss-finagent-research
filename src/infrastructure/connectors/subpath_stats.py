"""第三跳**内部**的子路径计数（单一事实源）—— 回答「这次 fetch 走的是哪条近路」。

## 为什么必须存在（这是可观测性缺口，不是洁癖）

`ConnectorRouter.fetch()` 内部是一条**三级短路**：
TTL 缓存（内存）→ 本地持久化库（DB 快照）→ 连接器链（**真联网**）。
而调用方（A01 采集侧）只看到「返回了 / 没返回」一个结果。于是三件事**给不出来**：

    · 本地命中率 —— 第三跳内部有多少次**根本没联网**（这是「本地优先」这条策略
      唯一能被量出来的证据）
    · 联网触发率 —— 真正打网络的比例（成本的分子；也是「为什么这次联网了」的答案）
    · 为什么慢 —— 是走了连接器（真等网络），还是**冷却 / 防撞钟预算**把它挡下了

`src/domain/agents/data/collector/path_stats.py` 的「诚实边界」一节早已把这件事
登记成缺口，并点名了修法：**「要真正的逐跳细分，得由 `ConnectorRouter` 自报它
走过的子路径」**。本模块就是那个自报口 —— 采集侧不猜，路由自己说。

## 为什么不并进 `src/core/hop_stats.py`（两个数不是一回事）

1. **分母不同**：`hop_stats.total` 计的是**四跳链**的一次决策
   （`query_data_for_agent` 一次调用一份），而本模块的 `total` 是
   **每一次 `ConnectorRouter.fetch`** —— 而绝大多数 fetch **根本不走四跳链**
   （分时面板、回测 API、定时作业、A17 的 `query_data` 工具都直接调 `fetch`）。
   并进去的那一刻，`hop_stats` 的读法（逐跳命中数 ÷ total）就会给出一个
   **看起来完全正常**的错误命中率 —— 这正是本仓库最贵的那类缺陷。
2. **聚合层级不同**：`hop_stats` 的键回答「**哪一跳**答出来的」；这里的键回答
   「第三跳**内部**由哪条近路服务」。前者是四跳的策略顺序，后者是第三跳的实现细节。
3. **键集合不同**：`HOP_KINDS` 是「四跳 + 缺口」的稳定顺序（文档、渲染、判据都读它）。
   塞进子路径键之后，「四跳」这个名字本身就不再成立。

选**新模块 + 一个读出口**（而不是散在 router 里）的理由就是本仓库那条硬约束：
同一条判据写在多处，必然有一处被漏改（实测过「同一个 key 写在 3 处、只改一处
⇒ 情报 5 个端点对所有人 403」）。所以全仓库**只有这一份**子路径计数，
`record()` 是唯一写入口、`snapshot()` 是唯一读出口。

## 判据键 = 第三跳内部**能证实**的结论（逐次互斥，一个不多一个不少）

| 键 | 含义 | 与谁相反（下一步动作不同） |
|---|---|---|
| `ttl_cache` | 进程内 TTL 缓存命中（**没碰库、没联网**） | 最便宜的一条 |
| `db_snapshot` | 本地库命中且**没联网**（当日窗口或科学口径新鲜） | 「本地覆盖够」⇒ 该少联网 |
| `db_snapshot_fallback` | **联网被走过**（失败或比库旧），最终仍由库给出 | 「源坏了/慢了」⇒ 修源 |
| `connector_network` | 连接器链**真的被走到了**（真联网） | 唯一的付费路径 |
| `cooldown_refused` | 全部匹配源都在失败冷却中 ⇒ 直接返回空，**一次网都没发** | 源已知坏，等冷却 |
| `budget_refused` | 防撞钟（`deadline_sec` 墙钟预算）用尽 ⇒ 终止 | 活太重 ⇒ 挪去定时作业 |
| `not_supported` | 没有任何连接器认这个指标名（契约不满足） | 改指标名，不是补源 |
| `miss` | 走了连接器链、**全失败**（真缺口） | 该去修链 |

**逐次互斥**（一次 fetch 只记一个键）是「本地命中率 / 联网触发率」能算出来的前提：
只要有人在多个分支各记一次，比例就会虚高且看不出来
（`hop_stats` 的判据 ① 防的是同一形状）。这份互斥不是靠约定，而是靠结构：
链上各处只调 `Outcome.mark()`（纯赋值），**写入计数只发生在 `fetch()` 的 finally 一处**。

## 逐次归属：`Outcome` 由**调用方持有**（`fetch(..., outcome=...)`，2026-10-01 接上）

上面的分布是**聚合**的：它回答"整个进程里各条近路各走了多少次"，但回答不了
"**这一次**采集走的是哪条"—— 而后者才是「本地命中率」能在**采集侧**（A01 的审计与
日志行）落地的形态；没有它，采集侧要么给不出这个数，要么只能去读"最近一次"。

所以 `ConnectorRouter.fetch()` 支持一个**可选出参**：调用方**自己新建**一个
`Outcome` 传进来，router 在 `finally` 里把本次的子路径 mark 到**那个对象**上。
不传时行为与加它之前逐字一致（内部建一个一次性对象，聚合计数照旧）。

⚠️ **为什么必须是"调用方自己持有"，而不是"读全局最近一次"**：并发下最近一次是
**别人的**结果 —— 它读起来完全正常，却是错的（本模块存在的全部理由就是消灭这类数）。
`Outcome` 是每次调用一个的局部对象，不共享、不上锁；共享一个对象、或去读
`latest_subpath` 冒充本次，等于把那个错数又造回来。

**异常 / 取消的语义**（如实表达，绝不用 `miss` 冒充）：

| 情形 | 调用方读到的 | 聚合计数 |
|---|---|---|
| 正常返回 | 8 个键之一（`resolved is True`） | 记一条 |
| 在决定之前就抛了 | `UNRECORDED`（`resolved is False`）= **本次没定** | 记进 `unexpected` |
| 取消且还没定 | `UNRECORDED` | **一次都不记**（取消是上层放弃等待） |
| 已定下、随后被取消 | 那个键（事实不变） | 照记（数据确实由这条近路给出） |

### 为什么比最初设想的清单多了一个 `db_snapshot_fallback`

「联网走过了、但数据最终由库给出」与「库直接命中、没联网」**处置相反**：
前者说明这个指标的源失败/变慢（该去看源），后者说明本地覆盖够（该少联网）。
混成一个 `db_snapshot`，「本地命中率」就会把**白联了一次网**的那些次算成命中 ——
而"白联一次网"恰恰是「为什么慢」这个问题的答案。这条纪律与
「不许把冷却拒绝混进网络失败」是同一条：**处置不同就必须分得开**。

### 为什么链上返回空列表仍记 `connector_network`

它答的是「**走没走**网络」（联网触发率/成本口径不能少算这一次）；
「网络**给没给**数据」由 `miss`（链上全失败）承载。两个键不是同一维度，
但都只在这两条互斥的出口上记，不会同一 fetch 记两次。

## 空态：「还没量到」与「量到 0」必须分得开

`counters` 里一律是整数（0 是有意义的数，比例的分母要它）；
冷启动时 `latest_subpath` 是 `UNMEASURED`（=「未量到」，与
`network_fallback.UNMEASURED`、`hop_stats.UNMEASURED`、
`path_stats.UNMEASURED` **同一个常量**，不另写一份字面量）= 还没量到；
某个 `SUBPATH_*` = 最近一次由那条近路服务。

`unexpected` 是**缺陷计数**，不是一条子路径：记「这次 fetch 走完了却一处都没
如实登记子路径」（漏埋点 / 取值拼错）。它存在的理由见下面「绝不抛」一节。

## 绝不新增 I/O、**绝不抛**（与 `hop_stats` 的一处刻意不同）

全是进程内整数自增：不写盘、不发网络请求。取数是主链路，任何「为了观测多走
一趟」都是把观测成本压到用户身上。

⚠️ `record()` **不认识的值不抛，只记进 `unexpected`** —— 这一处刻意与
`hop_stats.bump()` / `path_stats.record()` 的「未知键 KeyError」不同，理由只有一条：
本模块的写入点在 `fetch()` 的 **finally** 里，而 finally 里抛出的异常会
**顶掉真正的那个异常**（一次取数失败会显示成「计数键非法」）。那比
「某个键恒 0」更坏：前者掩盖现场，后者至少还在读数上留着。所以未知值走
`unexpected` —— **不静默**（`/health` 读得到它涨了），也**不炸热路径**。

## 线程/协程安全：`threading.Lock`（与 `hop_stats` 同一个选择，理由不重复）

1. **一致性而不是单键原子性**：`snapshot()` 要同时读出「一组计数 + `latest_subpath`
   + `total`」。CPython 的 dict 单键读写在 GIL 下是原子的，但「一组值凑成一份自洽
   快照」不是 —— 读的中途夹进一次 `record()`，就会得到「total 涨了、某一键没涨」
   这种**看起来完全正常**的矛盾快照。
2. **同一份判据要跨线程成立**：`mark()`/`record()` 在事件循环里被调用
   （`fetch()` 是协程），而读侧 `/health` 要经过 `run_infra(...)` 的**线程池**
   （见 `src/core/executors.py`）。协程之间不需要锁，但它们与线程池之间需要
   内存可见性保证。
3. **成本**：每次 fetch 只加一次整数，锁的代价（无争用约 0.1 µs 级）相对一次
   取数（就算最快的一跳也是内存过滤，量级 µs~ms）可忽略。

`Outcome` 反过来**刻意不上锁**：它是每次 `fetch()` 一个的局部对象（不传时由
`fetch()` 自己建、传时由调用方建），不共享 —— 没有共享就没有竞争，
锁只会掩盖「有人把它当全局用了」这种设计错误。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Final

#: 「没量到」的唯一说法：与 `hop_stats` / `path_stats` / `network_fallback`
#: **共用同一个常量**，不另写一份字面量。
#:
#: 为什么 import 而不是再写一遍：本仓库的硬约束是「读不到数据时显示
#: 『未量到/无法统计』，绝不用 0 糊过去」，而这条判据会出现在 `/health`、
#: 运维页、拒绝理由三处 —— 字面量写歪一个，下游的 `in` 判据就**静默不命中**。
#: 方向上也不构成新的环：`core.hop_stats` 已经这样 import 了
#: （`network_fallback` 只 import `core.budget` 与 `llm.*`，不 import connectors）。
from src.infrastructure.catalog.network_fallback import UNMEASURED

#: 进程内 TTL 缓存命中（毫秒级，**没碰库、没联网**）。
SUBPATH_TTL_CACHE: Final[str] = "ttl_cache"

#: 本地持久化库命中，且**没有联网**（`_within_reuse_window` 或 `_is_db_fresh`）。
SUBPATH_DB_SNAPSHOT: Final[str] = "db_snapshot"

#: 联网链**被走过**（失败、或返回的数据比库旧），最终仍由本地库给出。
SUBPATH_DB_SNAPSHOT_FALLBACK: Final[str] = "db_snapshot_fallback"

#: 连接器链**真的被走到了**（真联网）—— 含链上返回空列表。
SUBPATH_CONNECTOR_NETWORK: Final[str] = "connector_network"

#: 全部匹配源都在失败冷却中 ⇒ 直接返回空，**一次网络请求都没发**。
SUBPATH_COOLDOWN_REFUSED: Final[str] = "cooldown_refused"

#: 防撞钟（`deadline_sec` 墙钟预算）用尽 ⇒ 终止这条指标的剩余尝试。
SUBPATH_BUDGET_REFUSED: Final[str] = "budget_refused"

#: 没有任何连接器认这个指标名（契约不满足，不是源故障）。
SUBPATH_NOT_SUPPORTED: Final[str] = "not_supported"

#: 走了连接器链、**链上全失败**（真缺口）。
SUBPATH_MISS: Final[str] = "miss"

#: 计数键的**稳定顺序**（`/health` 渲染顺序、文档、断言都读它，不各写一份）。
SUBPATH_KINDS: Final[tuple[str, ...]] = (
    SUBPATH_TTL_CACHE,
    SUBPATH_DB_SNAPSHOT,
    SUBPATH_DB_SNAPSHOT_FALLBACK,
    SUBPATH_CONNECTOR_NETWORK,
    SUBPATH_COOLDOWN_REFUSED,
    SUBPATH_BUDGET_REFUSED,
    SUBPATH_NOT_SUPPORTED,
    SUBPATH_MISS,
)

#: `latest_subpath` 的初值：**还没量到**（不是 0、也不是任何一条近路）。
UNMEASURED_SUBPATH: Final[str] = UNMEASURED

#: `Outcome` 的初值：**一次都没登记**（走完了却没人认领 ⇒ 漏埋点）。
#:
#: ⚠️ 它不是一条子路径（不在 `SUBPATH_KINDS` 里）：它是一条**自鸣报警**，
#: 由一个诚实的默认值实现 —— 新加出口忘了登记时，`unexpected` 会涨，
#: 而不是被静默算到某条近路头上。
UNRECORDED: Final[str] = "unrecorded"


@dataclass
class Outcome:
    """一次 `fetch()` 的**唯一**子路径（**由调用方新建并持有**，一次一个、不共享）。

    为什么用持有者对象而不是在各处直接写计数：`_fetch_uncached` 决定不了
    「上层会不会再拿库覆盖这次结果」（网络失败/更旧时会回退库）。直接写计数
    会在这种回退上**记两次**（`miss` + `db_snapshot_fallback`），
    而比值虚高的表现是「看起来一切正常」。持有者把「最终是哪条」推迟到
    `fetch()` 的 finally 那一处写，**互斥就成了结构性的，不靠约定**。

    ## 谁新建它（2026-10-01：逐次归属接上，两种用法都要求"一次一个"）

    * **不传**（内部/老调用方）⇒ `fetch()` 自己建一个一次性的，读完就丢；
    * **传**（采集侧要"这一次走的是哪条近路"）⇒ 调用方在每次 `fetch()` 前
      **新建一个**；`fetch()` 入口会先把它清回「还没定」，所以即使同一个对象
      被复用，也不会把上一次的结论算给这一次。

    ⚠️ **不许跨并发/跨调用共享一个对象**：两个调用会互相覆盖，读出来的正是
    本模块要消灭的那种「看起来完全正常的错数」。
    """

    subpath: str = UNRECORDED

    def mark(self, subpath: str) -> None:
        """登记子路径（纯赋值，无锁、零 I/O）。

        **后写覆盖先写**：越晚的决定越接近「数据最终由谁给出」这个事实
        （联网失败 → 回退库，就是后写的赢）。
        """
        self.subpath = subpath

    def reset(self) -> None:
        """清回「还没定」（`fetch()` 在入口调用 —— 复用对象时不留上一次的结论）。"""
        self.subpath = UNRECORDED

    @property
    def resolved(self) -> bool:
        """本次是否**已经定下**子路径。

        `False` = 这次 `fetch()` 没定下来（在决定之前就抛了 / 被取消）——
        **不是** `miss`（`miss` 是"链上真的全失败"这个结论，只在链上全失败时出现）。
        """
        return self.subpath != UNRECORDED


@dataclass
class SubpathStats:
    """子路径计数的可变状态（**唯一实例**是下面的 `_STATS`）。"""

    #: 每条子路径「由它服务了这次 fetch」的次数。初值全 0（有意义）。
    counters: dict[str, int]
    #: 最近一次由哪条子路径服务（`UNMEASURED_SUBPATH` = 还没量到）。
    latest_subpath: str = UNMEASURED_SUBPATH
    #: fetch 总次数（含缺口与各类拒绝）—— 比例的分母由它给，不由调用方相加。
    total: int = 0
    #: **缺陷计数**：走完却一处都没登记的 fetch 次数（漏埋点/取值拼错）。
    unexpected: int = 0


#: 进程级状态（单一事实源）。锁保护的是**整组值**的一致性，不只是单键。
_STATS: Final[SubpathStats] = SubpathStats(
    counters={k: 0 for k in SUBPATH_KINDS})
_LOCK: Final[threading.Lock] = threading.Lock()


def record(subpath: str) -> None:
    """记一次 fetch 的最终子路径。**绝不抛、零 I/O**。

    **必须在真正决定由哪条近路服务的那一处登记**（链上各处 `Outcome.mark()`），
    并只在 `fetch()` 的 finally 里写入一次 —— 挂在只有测试才走的辅助函数上
    等于没接：本项目实测过这种形状（`describe()` 零个生产调用方，而且不报错）。

    未知取值 / `UNRECORDED`：**不抛弃、也不静默** —— 记进 `unexpected`
    （为什么这一处与 `hop_stats.bump()` 的 KeyError 不同，见模块头
    「绝不抛」一节的完整理由：finally 里抛会顶掉真正的异常）。
    """
    with _LOCK:
        if subpath not in _STATS.counters:
            _STATS.unexpected += 1
            return
        _STATS.counters[subpath] += 1
        _STATS.latest_subpath = subpath
        _STATS.total += 1


def snapshot() -> dict[str, Any]:
    """只读快照（`hop_stats.snapshot()` 与 `/health` 读它）。

    **绝不抛、绝不改状态**：读侧是前端 20 秒轮询的热路径，
    计数器出问题不该把整张健康检查打挂（同 `hop_stats.snapshot()` 的纪律）。

    返回的 `counters` 是**副本**：调用方改它不会污染真计数
    （本项目实测过「返回内部 dict，调用方清一下就把计数清了」那类自伤）。
    """
    with _LOCK:
        return {
            "counters": dict(_STATS.counters),
            "latest_subpath": _STATS.latest_subpath,
            "total": _STATS.total,
            "kinds": list(SUBPATH_KINDS),
            "unmeasured": UNMEASURED_SUBPATH,
            "unexpected": _STATS.unexpected,
        }


def reset_for_test() -> None:
    """把计数清回冷启动态（**只给测试用**；生产代码里不许出现调用）。"""
    with _LOCK:
        for key in SUBPATH_KINDS:
            _STATS.counters[key] = 0
        _STATS.latest_subpath = UNMEASURED_SUBPATH
        _STATS.total = 0
        _STATS.unexpected = 0


__all__ = [
    "SUBPATH_BUDGET_REFUSED",
    "SUBPATH_COOLDOWN_REFUSED",
    "SUBPATH_CONNECTOR_NETWORK",
    "SUBPATH_DB_SNAPSHOT",
    "SUBPATH_DB_SNAPSHOT_FALLBACK",
    "SUBPATH_KINDS",
    "SUBPATH_MISS",
    "SUBPATH_NOT_SUPPORTED",
    "SUBPATH_TTL_CACHE",
    "Outcome",
    "SubpathStats",
    "UNMEASURED_SUBPATH",
    "UNRECORDED",
    "record",
    "reset_for_test",
    "snapshot",
]
