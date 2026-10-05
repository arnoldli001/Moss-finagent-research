"""A01 采集侧的**数据路径计数**（单一事实源）—— 回答「这次走了几条路径、命中哪一条、护栏有没有拦」。

## 为什么必须存在（可观测性缺口，不是洁癖）

采集侧落审计/日志时，此前只有**"有没有取到"**这一个比特。于是两个问题**都答不出来**：

* "为什么慢" —— 是被**防撞钟护栏**拦下的（活太重/源太慢），还是源上就没有？
* "为什么没取到" —— 是**语义上不适用 / 专题表未收录**（本就不该有，别去补），
  还是连接器认了但这次没给数据（真缺口，该去修）？

四跳链的跳级命中埋点在 `src/core/hop_stats.py`，但它量的是 **A17 `query_data`**
那条链路；那份模块头 + `docs/PRD.md` §30.3 的诚实边界写着「不覆盖采集路径的
联网兜底（另一条链路，混进来会污染四跳口径）」。所以**采集路径必须有自己的计数**，
两套口径不许互相借：混在一起，"四跳命中率"会变成两条链路各占一半的混合平均 ——
那不是任何一个链路的命中率。

## 计数键 = A01 **边界内能证实**的结论（一个不多、一个不少）

| 键 | 含义 | 与谁相反 |
|---|---|---|
| `path1_backend` | 后端（连接器链）**给出了数据点** —— 命中 | 与下面四项互斥 |
| `path1_empty` | 后端返回**空列表**（"没量到"，不是"量到 0"） | 不是异常 |
| `path1_exempt` | 后端明确回报**语义不适用 / 未收录该主体**（豁免，不是缺口） | 不该联网硬试 |
| `path1_error` | 后端抛异常（真故障） | 该去修链 |

四者**逐次互斥**（一次取数只记一个键）—— 这正是"命中率/豁免率/故障率"能算出来的前提：
只要有人在多个分支各记一次，比例就会虚高且看不出来（`hop_stats` 的判据 ① 防的是同一形状）。

## 诚实边界：**后端内部**的子路径由后端自报（2026-10-01 起逐次接上）

`ConnectorRouter` 内部还有三层短路（TTL 缓存 → 本地库 → 连接器链），
A01 只调一个 `fetch()` —— 那几跳的归属**不在本模块的能力范围内**，
所以本模块**不猜**，只把后端自报的值原样带出去：

* **聚合分布**：唯一事实源是 `src/infrastructure/connectors/subpath_stats.py`
  （8 个互斥键，含 `db_snapshot_fallback` / `cooldown_refused` / `budget_refused` /
  `unexpected`）；读出口是 `hop_stats.snapshot()["connector_subpaths"]`
  与 `/health` 的 `query_data_hops.connector_subpaths`。
* **逐次归属**（`CHG-0157` §33.5 第 1 条，本轮接上）：A01 每次取数**自己新建**
  一个 `Outcome` 传给 `fetch()`，于是能读到"**这一次**走的是哪条近路"，
  并把它记进 `record()` 报告里的 `subpath` 字段（⇒ 产出、异常属性、台账、日志行
  自动都带上它；渲染归 `format_tokens`，格式只那一处）。

⚠️ 为什么 `subpath` **不进** `counters` / `PATH_KINDS`：它是**另一维**。
`counters` 那一维答"这一跳的**结论**是什么"（`path1_backend/empty/exempt/error`，
逐次互斥）；`subpath` 答"这一次由**哪条近路**服务"（后端自报）。两者描述的是
**同一次**取数，揉进同一个键就是同一个数被算两次 ⇒ 命中率虚高，而且读起来
完全正常。分布本身已在上面那个唯一事实源里数着，这里再数一份＝两份实现。

**所以本模块仍然不写"本地命中率 / 联网触发率"这两个数字**：比例的分母
（`subpath_stats.total`）是**全进程 fetch 次数**，而 A01 只占其中一部分
（分时面板、回测 API、定时作业、A17 的 `query_data` 都直接调 `fetch`）——
混算出来的比率不是任何一条链路的（`subpath_stats` 模块头第 1 条讲的就是这件事）。
采集侧要算自己那条链路的比率时，分母用**本模块的 `total`**（A01 取数次数），
分子从逐次 `subpath` 的记录（产出 `result["path_stats"]` / 台账 / 日志行）里数：
两个数属同一条链路，不借也不混。

## 绝不新增 I/O、绝不抛

计数全是**进程内整数自增**（一次取数 +1 次），不写盘、不发网络请求 ——
采集是主链路，任何"为了观测多走一趟"都是把观测成本压到用户身上。
`record()` 只在参数非法时抛（未知键），其余一切外部读数（候选路径数）都在
调用方 `try/except` 里降级成 `UNMEASURED`。

## 空态：「还没量到」与「量到 0」必须分得开

`counters` 里一律是整数（0 是有意义的数，比例的分母要它）；
冷启动时 `latest_path` 是 `UNMEASURED`（=「未量到」，与
`network_fallback.UNMEASURED`、`hop_stats.UNMEASURED` **同一个常量**，
不另写一份字面量）= 还没量到；某个 `path1_*` = 最近一次的结论。
同理 `paths_available` 问不出来时是 `UNMEASURED` —— **不是 0**：
0 读起来是"这个指标一条路都没有"（那是另一个结论：契约/登记缺失）。

## 线程安全：`threading.Lock`（与 `hop_stats` 同一个选择，理由不重复）

采集并发跑（`asyncio.gather` + `asyncio.to_thread`），而读侧可能来自线程池
（同 `hop_stats` docstring 的三条理由）。这里额外说明**为什么不靠单键原子性**：
`snapshot()` 要同时读出"一组计数 + latest_path + total"这一份自洽快照，
读的过程中夹进一次 `record()` 就会得到「total 涨了、某一键没涨」这种
**看起来完全正常**的矛盾快照。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Final

#: 「没量到」的唯一说法：与 `hop_stats` / `network_fallback` **共用同一个常量**。
#:
#: 为什么 import 而不是再写一遍：本仓库的硬约束是「读不到数据时显示
#: 『未量到/无法统计』，绝不用 0 糊过去」，而这条判据会出现在日志行、审计结果、
#: `/health` 三处 —— 字面量写歪一个，下游的 `in` 判据就**静默不命中**。
from src.infrastructure.catalog.network_fallback import UNMEASURED

#: 后端（连接器链）**给出了数据点** —— 这一跳命中。
PATH_BACKEND: Final[str] = "path1_backend"

#: 后端返回**空列表**：走过了、但没答出来（"没量到"，不是"量到 0"）。
PATH_EMPTY: Final[str] = "path1_empty"

#: 后端明确回报**豁免类**结论（语义不适用 / 专题表未收录该主体）。
#:
#: 与 `PATH_EMPTY` 分开，是因为两者下一步动作**相反**：豁免的不该联网硬试
#: （硬试只是烧钱），空结果才要去补链。混成一个 `empty`，这件事就又不可判定。
PATH_EXEMPT: Final[str] = "path1_exempt"

#: 后端抛异常（取数故障，真缺口）。
PATH_ERROR: Final[str] = "path1_error"

#: 计数键的**稳定顺序**（渲染顺序、文档、断言都读它，不各写一份）。
PATH_KINDS: Final[tuple[str, ...]] = (
    PATH_BACKEND, PATH_EMPTY, PATH_EXEMPT, PATH_ERROR,
)

#: `latest_path` 的初值：**还没量到**（不是 0、也不是任何一条路径）。
UNMEASURED_PATH: Final[str] = UNMEASURED

#: 本次取数**受防撞钟约束**（A01 传了 `deadline_sec` 且后端认这个参数）。
GUARDRAIL_ARMED: Final[str] = "armed"

#: 本次取数**不受**防撞钟约束（定时作业/预热路径 —— 重活正是要在那里做）。
GUARDRAIL_OFF: Final[str] = "off"


def classify(*, got_points: bool, exc: BaseException | None,
             gap_kind: str | None) -> str:
    """把一次取数的**观测结果**映射到一条路径键（纯函数，判据直接打它）。

    判据顺序即语义优先级：**取到数据 > 有豁免依据 > 空结果 > 异常**。
    为什么"豁免"排在"异常"前面：豁免本身就是通过抛异常传达的
    （`NoApplicableData`），先判异常会把它记成 `path1_error` ——
    于是"该口径对本主体不适用"在统计里变成"取数故障"（正是报障现场）。
    """
    if got_points:
        return PATH_BACKEND
    if gap_kind:
        return PATH_EXEMPT
    if exc is not None:
        return PATH_ERROR
    return PATH_EMPTY


@dataclass
class PathStats:
    """路径计数的可变状态（**唯一实例**是下面的 `_STATS`）。"""

    #: 每一条路径键"由它得出结论"的次数。初值全 0。
    counters: dict[str, int]
    #: 最近一次的结论落在哪一条（`UNMEASURED_PATH` = 还没量到）。
    latest_path: str = UNMEASURED_PATH
    #: 取数次数（含四个键的总和）—— 命中率的分母由它给，不由调用方相加。
    total: int = 0


#: 进程级状态（单一事实源）。锁保护的是**整组值**的一致性，不只是单键。
_STATS: Final[PathStats] = PathStats(counters={k: 0 for k in PATH_KINDS})
_LOCK: Final[threading.Lock] = threading.Lock()


def record(
    outcome: str, *,
    paths_queried: int = 1,
    paths_available: int | str = UNMEASURED,
    guardrail_armed: bool = False,
    guardrail_stopped: bool = False,
    subpath: str | None = None,
) -> dict[str, Any]:
    """记一次取数的路径结论，并返回**本次**的结构化报告（落审计/日志用）。

    **必须在真正得出结论的那一处调用**（`DataCollectorAgent.execute` 的三个
    出口之前一次性算完）—— 挂在只有测试才走的辅助函数上等于没接：
    本项目实测过这种形状（`describe()` 零个生产调用方，而且不报错）。

    `outcome` 不在 `PATH_KINDS` 里会 `KeyError`（**不是**静默忽略）：
    静默忽略会让一个拼错的键表现成"这条路永远 0 命中"，而它读起来正好是
    "这条路没用"。

    `paths_queried` / `paths_available` / `guardrail_*` 是**这次的观测值**，
    不参与全局计数（全局只记结论分布 —— 否则一次取数会在多个键上各加一次，
    比例就没法算了）。

    `subpath` 是**正交的另一维**，也是"这次的观测值"：`outcome`（报告里的
    `hit_path`）答"这一跳的**结论**是什么"（四个互斥键），`subpath` 答
    "这次由**哪条近路**服务"（后端自报，8 个键之一，唯一事实源是
    `src/infrastructure/connectors/subpath_stats.py`）。它**不参与全局计数**：

    1. 揉进 `counters` 就是同一个数被算两次（两个键描述的是**同一次**取数）
       ⇒ 命中率虚高，而且读起来完全正常；
    2. 分布已有唯一事实源（`subpath_stats.counters`），在这里再数一份
       ＝两份实现（本项目最贵的那类缺陷）；
    3. 本模块的分母是 **A01 取数次数**，那边的分母是**全进程 fetch 次数** ——
       两个数不许相加，也不许相除（混算出来的比率不是任何一条链路的）。

    `subpath=None`（后端不认这个出参 / 还没量到）⇒ 报告里是 `UNMEASURED`，
    **不是 0、也不是任何一条近路**（AGENTS.md 硬约束：读不到就说未量到）。
    ⚠️ 取值**不在这里校验**（不做白名单）：键集合由 `subpath_stats` 拥有，
    再判一次就是第二份实现；而它不参与计数 ⇒ 即使取值拼错，也不会让比例虚高
    （真拼错时那边会记进 `unexpected`，不静默）。
    """
    with _LOCK:
        _STATS.counters[outcome] += 1
        _STATS.latest_path = outcome
        _STATS.total += 1
    return {
        "paths_queried": int(paths_queried),
        "paths_available": paths_available,
        "hit_path": outcome,
        #: ★ 本次由哪条近路服务（后端自报；`UNMEASURED` = 还没量到）
        "subpath": subpath if subpath else UNMEASURED,
        "guardrail_armed": bool(guardrail_armed),
        "guardrail_stopped": bool(guardrail_stopped),
        "unmeasured": UNMEASURED,
    }


def snapshot() -> dict[str, Any]:
    """只读快照（运维/健康检查/审计读它）。

    **绝不抛、绝不改状态**：读侧可能是 20 秒轮询的热路径（同 `hop_stats`）。

    返回的 `counters` 是**副本**：调用方改它不会污染真计数
    （本项目实测过"返回内部 dict，调用方清一下就把计数清了"那类自伤）。

    ⚠️ 这里**没有**"子路径分布"，也不该有：那是**另一条链路**上的一维，
    唯一事实源是 `src/infrastructure/connectors/subpath_stats.py`
    （读出口 `/health` 的 `query_data_hops.connector_subpaths`）。
    本模块只带**逐次**的 `subpath`（在 `record()` 的报告里）——
    两份分布会让同一个问题有两个答案，而两个答案都"看起来正常"。
    """
    with _LOCK:
        return {
            "counters": dict(_STATS.counters),
            "latest_path": _STATS.latest_path,
            "total": _STATS.total,
            "kinds": list(PATH_KINDS),
            "unmeasured": UNMEASURED_PATH,
        }


def format_tokens(report: Any) -> str:
    """把一份路径报告渲染成**日志行尾的 `key=value` 片段**（格式只此一处）。

    为什么渲染放在本模块：字段名与键名是同一份口径；让调用方各自拼字符串，
    必然出现"日志里叫 hit、审计里叫 latest_path"这种**改了不报错**的漂移。

    `report` 不是映射/缺字段时返回空串（**观测坏了不该把日志行带下去**）。
    """
    try:
        if not isinstance(report, dict):
            return ""
        get = report.get
        tokens = [
            f"paths_queried={get('paths_queried', 1)}",
            f"paths_available={get('paths_available', UNMEASURED)}",
            f"hit_path={get('hit_path', UNMEASURED)}",
            #: 护栏三态用常量渲染（不许在这里另写一遍 'armed'/'off' 字面量）
            "guardrail=" + (GUARDRAIL_ARMED if get("guardrail_armed")
                            else GUARDRAIL_OFF),
            f"guardrail_stopped={bool(get('guardrail_stopped'))}",
            #: ★ 新字段**追加在尾部**（与 `format_gap_log` 同一条纪律：
            #: 既有契约断言日志行以固定前缀开头/含固定片段，插到前面会打断 grep）。
            #: 「未量到」也走 `UNMEASURED` 常量渲染，不另写字面量。
            f"subpath={get('subpath', UNMEASURED)}",
        ]
        return " ".join(tokens)
    except Exception:  # noqa: BLE001 渲染失败 ⇒ 少一段，不抛
        return ""


def reset_for_test() -> None:
    """把计数清回冷启动态（**只给测试用**；生产代码里不许出现调用）。"""
    with _LOCK:
        for key in PATH_KINDS:
            _STATS.counters[key] = 0
        _STATS.latest_path = UNMEASURED_PATH
        _STATS.total = 0


__all__ = [
    "GUARDRAIL_ARMED",
    "GUARDRAIL_OFF",
    "PATH_BACKEND",
    "PATH_EMPTY",
    "PATH_ERROR",
    "PATH_EXEMPT",
    "PATH_KINDS",
    "PathStats",
    "UNMEASURED",
    "UNMEASURED_PATH",
    "classify",
    "format_tokens",
    "record",
    "reset_for_test",
    "snapshot",
]
