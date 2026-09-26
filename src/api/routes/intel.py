"""舆情情报：REST 接口（前缀 `/api/v1/intel`）。

## 三道门各管一件事

| 层 | 管什么 | 在哪 |
|---|---|---|
| `LoginGateMiddleware` | 登录了没有 | 自动 —— 新路由默认需登录，不必在此重复 |
| `require_feature` | **这个等级买了没有** | 本文件每个端点显式调用 |
| `core.policy` | 角色能不能做这个动作 | 写操作（事件处置） |

## 数据源保密的实现位置（重要）

**接口响应里根本不存在**以下字段（不是"脱敏"，是不构造）：
`source_url` / `report_url` / `group_id` / `author_id` / `topic_id` / token。

三层保证，缺一不可：
  1. 连接器 `IntelItem.to_public()` 白名单构造（新字段默认不出）
  2. 本路由只回 `feed.to_public()`，**不直接序列化内部对象**
  3. `tests/unit/test_intel_source_privacy.py` 断言键集合与值形态

## 为什么接口不做 LLM 分析

采集要快（秒级），模型慢（本地 8B ~30s/批）。分析放调度任务，
结果存库给接口读 —— 在线延迟稳定，模型升级/重跑不影响用户。

## ★ 更进一步：`/feed` **也不做（同步）采集**（2026-10-01 第七轮）

用户口径：

> "前端刷新触发后端任务，但是不应该等待后端输出才显示，后端采集信息和
>   输出需要时间的，容易触发等待时间，可以做监听回调，有信息了再触发
>   自动刷新前端。"

实测 `build_feed` 冷 2.18s / 热 1.01s（并发拉六个源 + 知识星球增量），
而原实现是 `await build_feed(...)` —— 于是**每一次打开页面**都要先等 1~2 秒。
只加 TTL 缓存不够：第一次与每一次过期仍然要等，那正是用户不接受的部分。

所以本文件里 `/feed` 的形状是"**立即返回 + 后台重建 + 就绪通知**"：

    GET /feed ──► 立刻返回进程内缓存那一份（没有就给形状正确的空壳）
                  └─► 缺失/过期/显式 refresh → 起后台任务（单飞）
                      后台建完 → 落缓存 → 经**既有**告警 WebSocket 推信令
                      前端收到 → 自己再拉一次（用户什么都不用点）

⚠️ 这条链路**没有**引入第二次采集：后台任务调用的仍然是同一个 `build_feed`。
"不阻塞"是靠**把等待搬出请求**做到的，不是靠降低数据质量。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.routes.my_features import require_feature
from src.core.errors import brief
from src.domain.intel.service import DEFAULT_LIMIT, KIND_LABELS, build_feed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/intel", tags=["intel"])

#: 功能 key。**必须与 `platform/config.py` 的 `FEATURES` 完全对得上** ——
#: 对不上的表现是"矩阵里能勾，但接口一律 403"（包括管理员），
#: 而日志里只有一句 `feature_disabled`，看不出是 key 写错了。
#:
#: ⚠️ 2026-09-25 改名：`intel.radar` → **`intel.hot`**。
#: 原来三项（雷达 / 盘前简报 / 事件告警）按用户口径并为两项：
#: 删掉「情报雷达」「盘前简报」，新增「热点&研报小作文」。
#: 当年 5 个端点用的是 `FEATURE_RADAR`，如果只改 `FEATURES` 不改这里，
#: 情报流 / 投资日历 / 热榜 / 来源健康 会**全部 403**（管理员也一样）——
#: 这是本次改动最容易漏的一处。
FEATURE_HOT = "intel.hot"
FEATURE_ALERTS = "intel.alerts"

#: 原文倾向筛选器的取值（用户口径 2026-09-26）。
#:
#: `all` = 不筛；`bull` = 【多】；`bear` = 【空】。
#:
#: ⚠️ **与可信度档位 `FILTERS` 是两个独立的轴**，不要合并成一个参数：
#: 合并后用户在「高可信」档下切到「偏多」就会丢掉可信度档位，
#: 想表达"高可信 + 偏多"就没有办法了。
#:
#: 判据见 `domain.intel.service.direction_of` —— 前端那个【多】/【空】
#: 标记用的是同一份数据，所以"筛出来的"与"看到的标记"必然一致。
_DIRECTIONS = ("all", "bull", "bear")

#: ⚠️ `FEATURE_BRIEF` 已删除：`/intel/brief` 端点已随「盘前简报」下线。
#: 前端从来没有调用过它（`IntelBriefTab` 未挂载），保留一个永远 403 的
#: 端点只会让人以为"这个功能还在，只是没权限"。

#: 热议扫描的池子大小（只影响扫描，不影响返回条数）
HOT_SCAN_POOL = 300

#: 热榜取数的超时墙（秒）。
#:
#: 热榜是**附加信息**，而情报流是主内容 —— 不能让它把主内容拖成超时。
#: 实测千股千评约 1~3s；给 6s 足够，超了就记缺口。
HOT_RANK_TIMEOUT = 6.0

#: 落盘热榜的**保鲜期**（秒）。超过它才允许现抓一次。
#:
#: ## 为什么不是"每次都现抓"（那正是这次的性能事故）
#:
#: 原来的实现是**每次 `/feed` 都实时抓热榜**（实测 3.0~3.5 秒），
#: 与模块注释声称的"热榜走定时任务落库，这里只读结果"**完全相反** ——
#: 于是情报流每打开一次就要等 5~6 秒。
#:
#: ## 为什么也不是"只读落盘、绝不现抓"
#:
#: 落盘结果可能缺失或过期（作业没跑成 / 还没到班次），只读落盘会让
#: 客户那边的人气榜**永远是空的**，"优化"直接变成功能故障。
#:
#: 所以取折中：**有新鲜落盘就读（0 ms），过期了才现抓一次并落盘**。
#: 有调度器的实例上这一段几乎不触发；落盘确实缺失时退化成
#: "每 10 分钟有一个访问者等 3 秒"，而不是"每一次都等"。
#:
#: ⚠️ 这里原来写的是"因为对外试点实例 `MOSS_SCHEDULER_ENABLED=0`
#: （根本没有定时任务）"。**那句话已被实测推翻**（2026-10-01）：
#: `MOSS_SCHEDULER_ENABLED` **全仓库没有一处读它** —— `manage.py` 写它、
#: 启动横幅照着它印"定时任务：已关闭"，而 `main.py` 的 lifespan
#: **无条件** `CronScheduler(...).start()`。证据：`data/pilot/scheduler/runs.jsonl`
#: 有 497 条记录、24 个作业（含每 30 分钟一班的 `quant_data_sync`）。
#: 兜底的理由要换成真的那一个（**作业可能没跑成**），否则后来的人会以为
#: "把开关关掉"就能让这段兜底失效。
HOT_RANK_MAX_AGE = 600.0

#: `/feed` 结果缓存的**保鲜期**（秒）。
#:
#: ## 为什么情报流可以缓存，而资金流不行
#:
#: 情报是**小时级**内容：平台快讯由定时任务采集（知识星球 2 小时一轮），
#: 「热议事件」也是 2 小时一次的聚合。而 `/feed` 每次请求都要**现场并发拉六个源**
#: （实测冷 2.0s / 热 1.0s）—— 用户切一次页签就付一次，纯浪费。
#:
#: ## ★ 这个 TTL **不决定"用户要不要等"**（2026-10-01 第七轮改口径）
#:
#: 请求**永远立即返回**（见本文件"后台重建"一段），所以 TTL 不是"用户能忍
#: 多久的陈旧"，而是"多久允许触发一次后台重建"。判据因此变成
#: **上游多久可能真的多出一条内容**：
#:
#:   · 前端**没有轮询**（`IntelPanel` 只在挂载 / 切档位 / 手动刷新时取数，
#:     实测全仓库 `fetchIntelFeed` 只有那一处调用），所以 60 秒吸收的正是
#:     "来回切页签、切筛选档、切排序"这一串重复请求；
#:   · 上游采集是分钟级到小时级（快讯几秒一条、知识星球 2 小时一轮），
#:     60 秒的陈旧上限不会让用户"看不到新内容"；
#:   · 手动刷新走 `refresh=true` **绕开**它（另受 `FEED_REFRESH_FLOOR` 下限保护）。
#:
#: ⚠️ 原来写的是 300 秒。那是在"请求会阻塞在采集上"的前提下定的
#: （用更长的 TTL 换更少的等待）—— **请求不再阻塞之后那个理由就消失了**，
#: TTL 必须按"内容新鲜度"重定，否则就是留着一个没有依据的数字，
#: 而后来的人会以为"用户能忍 5 分钟"，继续往上加。
FEED_CACHE_TTL = 60.0

#: `refresh=true` 的**最小重建间隔**（秒）：比它更年轻的缓存直接复用。
#:
#: 为什么需要它：`refresh=true` 是"用户点了刷新"，而用户会**连点**，
#: 面板重挂载 / 切档位也可能带上它。没有下限时每一次点击都在后台重新
#: 并发拉六个源（~1 秒 CPU + 六次外部请求）—— 那是我们自己打自己的上游，
#: 而"刷新变慢"的表现会先落在上游封禁上，不是落在这里。
#:
#: 10 秒的依据：这个窗口内上游**不可能**产出新内容（快讯源的最小节奏是
#: 分钟级，知识星球游标 2 小时一轮），所以"不重建"不会让用户错过任何东西。
FEED_REFRESH_FLOOR = 10.0

#: 缓存条目上限（键含 limit/codes/sort/filter）。超了就整体清空 ——
#: 与 `mainline._SNAPSHOT_CACHE` 同一策略：防参数枚举把内存撑满。
FEED_CACHE_MAX = 24


@dataclass
class _FeedEntry:
    """缓存里的一份情报流（**一次成功重建的全部产物**）。"""

    #: 写入时刻（`time.monotonic()`）：只用于算年龄，不受系统时钟调整影响。
    at: float
    #: 写入时刻（可读 ISO）：下发给前端做"数据截至"，也是排障时唯一的线索。
    at_iso: str
    #: 全局递增序号。前端只比较它 —— 比"时间戳字符串"可靠得多
    #: （时间戳有格式、时区、精度三个坑，而序号只有一个单调方向）。
    seq: int
    payload: dict[str, Any]


#: 缓存的**三道结构**：条目表 / 正在重建的键 / 后台任务的强引用。
#:
#: ## ⚠️ 为什么用 `threading.Lock` 而不是 `asyncio.Lock`
#:
#: 本文件里**所有**触碰这三样的代码都跑在事件循环上（路由是 `async def`，
#: 后台重建是 `asyncio.Task`），所以 `asyncio.Lock` 也能用。选 `threading.Lock`
#: 的理由只有一条，但是硬的：`asyncio.Lock` 会**绑定第一次使用它的那个事件循环**，
#: 之后从另一个循环再用就抛 `RuntimeError`。而单测里 `asyncio.run(...)` 一次
#: 就是一个新循环、`TestClient` 自带 portal 循环 —— 一个跨循环的锁会让
#: "换个写法跑测试就报一个与业务无关的错"，那种红最难查。
#:
#: ⚠️ **绝不许在持锁期间 `await`**：`threading.Lock` 挡住的是整个事件循环
#: （不是只挡住这一个协程）。所以三处临界区都只有字典/集合操作，
#: 没有一次 I/O —— 加任何一行 `await` 进来都会把并发能力整体废掉。
_FEED_CACHE: dict[str, _FeedEntry] = {}
_FEED_BUILDING: set[str] = set()
_FEED_TASKS: set[asyncio.Task] = set()
_FEED_LOCK = threading.Lock()
#: 全局序号（每次成功重建 +1）。故意**不做成每键一份**：前端兜底轮询是
#: 无参数的一条廉价接口，用全局序号它才不需要复刻缓存键的拼法
#: （拼法一旦漂移，兜底轮询就永远看不到新数据，而且不会有任何报错）。
_FEED_SEQ = 0


def feed_cache_key(*, limit: int, watch: list[str], sort: str,
                   filter: str, direction: str = "all") -> str:
    """情报流结果缓存的键。**必须含全部影响结果的参数**。

    抽成函数而不是在端点里内联拼字符串，是因为它现在有**两个**调用点
    （端点 / 后台预热 `domain.intel.prewarm`）—— 两处各拼一遍必然漂移，
    而漂移的表现最阴：**预热写进的键与请求读的键差一点，于是预热永远
    命中不了**，用户照样吃冷启动，而日志里一个字都不报。

    漏掉任何一个参数（比如 `filter`）的后果同样隐蔽：切到"高可信"档会拿到
    上一档的结果，看起来完全正常。

    ⚠️ **`direction` 有默认值 `"all"` 是刻意的**：老的调用方（预热、
    以及任何还没升级的路径）按 `"all"` 拼键 —— 与端点在不传该参数时的键
    **逐字一致**，所以预热的缓存仍然命中。给它默认空串或 None 都会让
    两边键不同、静默失去预热效果。
    """
    return f"{limit}|{','.join(watch)}|{sort}|{filter}|{direction}"


def _feed_cached(cache_key: str) -> tuple[_FeedEntry | None, float | None]:
    """读缓存：返回 `(条目, 年龄秒)`。没有条目时年龄是 `None`（**不是 0** ——
    "从没建过"与"刚建好"在判断里必须分得开）。"""
    with _FEED_LOCK:
        entry = _FEED_CACHE.get(cache_key)
    if entry is None:
        return None, None
    return entry, time.monotonic() - entry.at


def _feed_building(cache_key: str) -> bool:
    with _FEED_LOCK:
        return cache_key in _FEED_BUILDING


# ======================================================================
# 慢聚合的"返回旧数据 + 后台续期"发射器（投资日历 / 舆情热度）
#
# 与 `/feed` 的"空壳 + 后台重建"是**不同**的策略，别互相套用：
#
#                /feed                      /calendar、/heat
#   冷启动       给空壳（前端画骨架）        现拉一次（空壳对人无意义）
#   过期         TTL 短(60s)，重建快         返回**旧数据**，后台续期
#   理由         内容是分钟级、空壳能表达"在采集"  日程/热度是小时级，
#                                            给一份几分钟前的远比等 11 秒好
#
# 实测依据（2026-09-26，本机 8110 真实会话）：日历 **11,133 ms**、热度
# **3,472 ms**，而 `/feed` 只要 36 ms —— 首屏卡 5~10 秒的真凶是前者。
# ======================================================================

#: 正在后台续期的慢聚合名（防同一份被并发续期多次）。
_SLOW_BUILDING: set[str] = set()
#: 后台续期任务（持有强引用，防被 GC 提前回收 —— 与 `_FEED_TASKS` 同理）。
_SLOW_TASKS: set[asyncio.Task] = set()


def _slow_ttl_hours() -> float:
    try:
        from src.core.config import get_settings

        return float(get_settings().intel_slow_cache_hours)
    except Exception:  # noqa: BLE001 配置读不到就用保守默认，不阻断请求
        return 6.0


def _spawn_slow_renew(name: str, build: Any) -> None:
    """后台续期一份慢聚合。**请求不等待它**。

    单飞：同一份已经在续期就不重复起 —— 用户连点刷新时那几十次请求
    只该产生一次上游拉取（"自己打自己的上游"在本项目是有代价的教训）。
    """
    with _FEED_LOCK:
        if name in _SLOW_BUILDING:
            return
        _SLOW_BUILDING.add(name)

    async def _run() -> None:
        from src.domain.intel import prewarm

        try:
            payload = await build()
            if payload:
                prewarm.save_slow(name, payload)
        except Exception as exc:  # noqa: BLE001 续期失败不该影响本次响应
            logger.warning("慢聚合后台续期失败（%s）：%s", name, brief(exc))
        finally:
            with _FEED_LOCK:
                _SLOW_BUILDING.discard(name)

    task = asyncio.create_task(_run())
    _SLOW_TASKS.add(task)
    task.add_done_callback(_SLOW_TASKS.discard)


async def _slow_payload(name: str, build: Any) -> dict[str, Any]:
    """慢聚合的统一出口：`build()` 是"从上游现算一份"的协程。

    三档（见 `prewarm.load_slow` 的说明）：
      命中且新鲜 → 直接回，**零上游**；
      命中但过期 → 回旧的 + 后台续期；
      没命中     → 现拉一次（并落盘给下次用）。
    """
    from src.domain.intel import prewarm

    ttl = _slow_ttl_hours()
    cache = prewarm.load_slow(name, ttl_hours=ttl)
    if cache.hit:
        payload = dict(cache.payload or {})
        payload["cache_age_seconds"] = round(cache.age_seconds, 1)
        if cache.stale:
            _spawn_slow_renew(name, build)
        return payload

    payload = await build()
    prewarm.save_slow(name, payload)
    payload = dict(payload)
    payload["cache_age_seconds"] = 0.0
    return payload


def _feed_status() -> dict[str, Any]:
    """后台重建状态（**零 I/O、零网络**，见 `/feed/status`）。"""
    now = time.monotonic()
    with _FEED_LOCK:
        newest = max((e.at for e in _FEED_CACHE.values()), default=0.0)
        newest_iso = max((e.at_iso for e in _FEED_CACHE.values()), default="")
        return {
            "building": bool(_FEED_BUILDING),
            "seq": _FEED_SEQ,
            "built_at": newest_iso,
            "age_seconds": round(now - newest, 1) if newest else None,
            "ttl": FEED_CACHE_TTL,
        }


def _empty_heat() -> dict[str, Any]:
    """空的热议区块。键必须与 `_build_heat` 的返回值**逐键一致**。

    ⚠️ 少一个键不会有任何报错，只会让前端某块渲染不出来（`IntelHeatBlock`
    读的是 `heat.stocks` / `heat.rank`…），而冷启动恰好是每个新进程
    第一次打开页面时必然走到的路径 —— 也就是"一定会踩到、但只在重启后踩到"。
    """
    return {"at": "", "stocks": [], "topics": [], "rank": [], "rank_at": "",
            "stocks_scanned": 0, "gaps": [],
            # 冷启动时它**不是**例行免责声明，而是实话：
            # 界面上如果走到了这里，应当显示"正在采集"而不是"今天没人讨论"。
            "note": "正在采集公开信息…"}


def _feed_placeholder(*, limit: int, filter: str, sort: str) -> dict[str, Any]:
    """冷启动（缓存还没建好）时下发的**空壳 payload**。

    键与真实 payload 逐键相同（见 `_empty_heat` 的同一条纪律）：
    前端不必为"这一次是空壳"写第二套渲染分支。

    ⚠️ `limit` 也要给：真实 payload 里带它（启动热加载要靠它重算缓存键，
    见 `_feed_public_payload` 的注释），空壳少了它就会让"第一次请求"与
    "第二次请求"的键集合不一致 —— 而那正是这条纪律要防的事。
    """
    from src.domain.intel.credibility import FILTERS

    return {
        "items": [], "gaps": [], "counts": {}, "fetched_at": "",
        "degraded": False, "credibility_dist": {}, "cluster_stats": {},
        "tone_dist": {}, "filter_stats": {},
        # 多/空筛选器的角标。空壳也要给（哪怕全是 0）—— 少一个键会让
        # 前端在读 `direction_dist.bull` 时炸在渲染期，而"这次是空壳"
        # 本来就不该是特例。
        "direction_dist": {"bull": 0, "bear": 0}, "direction_hidden": 0,
        "admin_hints": [], "limit": limit, "filter": filter, "sort": sort,
        "filters": FILTERS, "heat": _empty_heat(),
    }


def _feed_hub(request: Request) -> Any:
    """拿到告警 WebSocket hub（**复用既有推送通道**，不新建第二条）。

    `runtime` / `alert_hub` 都可能是 `None`（子系统没装配 / 单测里没有 lifespan），
    这时返回 `None`，前端自动退回 `/feed/status` 轮询（见 `IntelPanel`）。
    """
    runtime = getattr(getattr(request, "app", None), "state", None)
    return getattr(getattr(runtime, "runtime", None), "alert_hub", None)


def _kick_feed_build(request: Request, cache_key: str, *, watch: list[str],
                     limit: int, sort: str, filter: str,
                     direction: str = "all") -> bool:
    """在**后台**重建这份缓存；**不 await、不阻塞请求**。返回是否新起了任务。

    ## 单飞（single-flight）：并发的请求不许各起一个采集

    判据是"这个键是否已在 `_FEED_BUILDING` 里"，检查与插入在**同一个临界区**里
    完成 —— 分成两步就是经典的 TOCTOU：两个请求同时读到"没在重建"，
    于是并发拉两遍六源。这类 bug 在本地单用户下测不出来，只有在试点
    （多个用户同时打开页面）才会表现为"上游偶尔超时"。

    ⚠️ 这是**进程内**的守卫，仅此而已。多 worker / 多副本部署时，
    每个进程各有一份缓存与一个守卫，会各自采集一遍 —— 那不是本层的职责
    （本层只保证"一个进程内不重复劳动"），也不该在这里做分布式租约
    （硬约束：调度器的租约锁由另一个任务负责）。

    ## 后台任务必须有强引用

    ⚠️ `asyncio.create_task` 只保留**弱**引用：任务对象被 GC 之后会被**静默取消**，
    表现是"点了刷新、界面永远等不到新数据"，而日志里一个字都没有。
    所以挂进 `_FEED_TASKS`，结束回调里再摘掉（顺带防这个集合无界增长）。
    """
    with _FEED_LOCK:
        if cache_key in _FEED_BUILDING:
            return False
        _FEED_BUILDING.add(cache_key)
    try:
        task = asyncio.create_task(
            _build_feed_bg(cache_key, watch=watch, limit=limit, sort=sort,
                           filter=filter, direction=direction,
                           hub=_feed_hub(request)),
            name=f"intel-feed-build:{cache_key}")
    except RuntimeError:
        # 没有运行中的事件循环（正常路由路径不可能走到这里；单测直接调函数才可能）。
        # **必须把标记摘掉**：否则这个键会被永久认为"正在重建"，
        # 之后所有请求都不会再触发重建，页面上则永远停在旧数据上。
        logger.warning("情报流后台重建无法启动（没有事件循环），已撤销标记")
        with _FEED_LOCK:
            _FEED_BUILDING.discard(cache_key)
        return False
    _FEED_TASKS.add(task)
    task.add_done_callback(_FEED_TASKS.discard)
    return True


async def _build_feed_bg(cache_key: str, *, watch: list[str], limit: int,
                         sort: str, filter: str, hub: Any,
                         direction: str = "all") -> None:
    """后台重建一份情报流：采集 → 组装 → 落缓存 → **推送"可以来取了"**。

    ⚠️ 落缓存与推送的**顺序不能反**：先推送再落缓存，会在客户端收到通知后
    立刻回拉时命中**旧**数据（`seq` 没变），于是它认为"通知是假的"并停下 ——
    用户要再点一次刷新才看得到，正好是这次要消灭的动作。
    """
    global _FEED_SEQ
    try:
        feed = await build_feed(watch_codes=watch, limit=limit, sort=sort,
                                filter=filter, direction=direction)
        payload = _feed_public_payload(feed, limit=limit, filter=filter,
                                       sort=sort)
        payload["heat"] = await _build_heat(feed, limit=limit)
        at_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        with _FEED_LOCK:
            _FEED_SEQ += 1
            seq = _FEED_SEQ
            if len(_FEED_CACHE) >= FEED_CACHE_MAX:
                _FEED_CACHE.clear()      # 与 mainline 同策略：整体清空防撑满
            _FEED_CACHE[cache_key] = _FeedEntry(
                at=time.monotonic(), at_iso=at_iso, seq=seq,
                payload=dict(payload))
    except Exception:  # noqa: BLE001 后台失败只记日志：请求侧已经在返回数据了
        logger.exception("情报流后台重建失败 cache_key=%s", cache_key)
        return
    finally:
        with _FEED_LOCK:
            _FEED_BUILDING.discard(cache_key)

    # ── 通知客户端"数据已就绪" ──
    #
    # 复用**既有的告警 WebSocket 通道**（`src/api/alert_hub.py`，路由
    # `/api/v1/ws/alerts`）：App 层本来就有一条常驻连接（`useAlertsWs`），
    # 再开一条通道等于两套重连/心跳/鉴权要各自维护，迟早漂移。
    #
    # ⚠️ 载荷**只有序号与时间戳**，没有任何情报内容 —— 这是它能跨租户广播的
    # 前提（见 `AlertHub.notify` 的说明）。hub 拿不到时就不推：
    # 前端会自动退回 `/feed/status` 轮询，功能不降级。
    #
    # ⚠️ 这里**不能 `return`**：落盘还在后面（见下）。原来写的是
    # `if hub is None: return`，而**预热正是 hub 可能为 None 的场景** ——
    # 于是"预热抓到的数据永远不落盘"（实测：`prewarm_state.json` 写了、
    # `feed_payload.json` 没写），跨重启热加载静默失效。
    if hub is not None:
        try:
            await hub.notify({"type": "intel_feed",
                              "data": {"seq": seq, "built_at": at_iso}})
        except Exception as exc:  # noqa: BLE001 推送失败不影响缓存已经就绪这件事
            logger.info("情报流就绪通知推送失败（前端将退回轮询）：%s",
                        type(exc).__name__)

    # ── 落盘一份 payload：供**下次启动**热加载（消除首屏冷启动）──
    #
    # 顺序要求（2026-09-25 实测踩到两次，两条都要满足）：
    #
    # ① 必须在**推送之后**：`finally` 里已经清掉了 `_FEED_BUILDING`，而
    #    `_wait_idle()`（以及任何"等重建结束"的调用方）就是看这个标记的。
    #    落盘放在推送之前，会让等待方在**通知发出前**就认为完成了 ——
    #    实测 `test_build_completion_pushes_on_the_existing_alert_hub` 直接失败
    #    （`sock.sent == []`），线上表现是"前端偶发收不到就绪通知"。
    # ② 不能被 `hub is None` 短路（见上）。
    #
    # 放在这里而不是只放在预热里：用户请求触发的重建也会走到这，于是
    # "磁盘上那份"始终跟得上内存里那份（预热被关掉时也能热加载）。
    try:
        from src.domain.intel import prewarm as _prewarm

        await asyncio.to_thread(_prewarm.snapshot_payload)
    except Exception as exc:  # noqa: BLE001 落盘失败只是下次启动没热缓存
        logger.debug("情报流缓存落盘失败（忽略）：%s", type(exc).__name__)


def _feed_public_payload(feed: Any, *, limit: int, filter: str,
                         sort: str) -> dict[str, Any]:
    """`IntelFeed` → 下发 payload。**逐键与旧实现一致**（含新增的展示字段）。

    抽成函数是因为它现在有**两个**调用点（后台重建 / 将来的预热任务）——
    两份实现必然漂移，而漂移的表现是"预热出来的那份少了 `heat`"这种
    只在某一条路径上出现的缺块。

    ## `limit` 必须写进 payload（2026-09-26）

    落盘缓存（`prewarm.save_payload`）存的就是这份 payload，而**启动热加载
    要靠它重算缓存键**。原来 payload 里只有 `sort`/`filter`，没有 `limit` ——
    于是重算只能拿"items 的条数"顶替，而那个数**不等于请求的 limit**
    （实测 60 的请求落盘后 items 只有 59 条：收容组把几十条并成了一行）。

    结果就是载入写进 `59|...`、请求读 `60|...` —— 预热**静默失效**，
    而日志里明明白白写着"已从落盘缓存热加载"。所以这个字段是**功能必需**，
    不是可选的元信息。
    """
    from src.domain.intel.credibility import FILTERS

    payload = feed.to_public()
    # 用户侧只看到"某类来源暂无更新"；管理员提示单独放 admin_hints，
    # 由前端按 `applied_tier == 'admin'` 决定是否展示。
    admin_hints = [g["admin_hint"] for g in feed.gaps if g.get("admin_hint")]
    payload["gaps"] = [{k: v for k, v in g.items() if k != "admin_hint"}
                       for g in feed.gaps]
    payload["admin_hints"] = admin_hints
    payload["limit"] = limit
    payload["filter"] = filter
    payload["sort"] = sort
    payload["filters"] = FILTERS
    # 分层计数（筛选 tab 的角标）。
    #
    # ⚠️ **不随当前 `filter` 变化**：拿 `payload["items"]` 去数会让
    # "切一次 tab 所有角标都变"，用户会以为数据在动。
    # 这个分布由 `build_feed` 在**筛选之前**、于池子上统计好后带回，
    # 所以一次请求就够，且与当前档位无关。
    payload["credibility_dist"] = feed.credibility_dist
    return payload


# ======================================================================
# 情报流（情报雷达）
# ======================================================================

@router.get("/feed")
async def intel_feed(
    request: Request,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200,
                       description="返回条数上限"),
    codes: str = Query(default="",
                       description="关注标的（逗号分隔，用于拉取对应研报）"),
    sort: str = Query(default="credibility",
                      pattern="^(credibility|time)$",
                      description="取样顺序：credibility 优先纳入高可信条目｜"
                                  "time 纯时间"),
    filter: str = Query(default="all",
                        description="可信度筛选档：all/high/mid_up/low/"
                                    "official/broker（只作用于**返回结果**，"
                                    "计数 counts 始终是全量口径）"),
    direction: str = Query(default="all",
                           description="原文倾向筛选：all/bull/bear —— 即列表"
                                       "标题前的【多】/【空】标记（判据是抽取"
                                       "任务落库的 tone.tone，与标记同源）"),
    refresh: bool = Query(default=False,
                          description="true=立刻在后台重建一次（请求本身不等待）"),
) -> dict[str, Any]:
    """聚合六源情报流。**请求永远不等待采集**（用户口径 2026-10-01 第七轮）。

    **单源失败不影响整体** —— 失败进 `gaps`，并置 `degraded=true`，
    前端据此显示"数据不完整"（**不显示具体是哪个源坏了**）。

    ## ★ 为什么请求不阻塞（这一段是本端点的核心契约）

    用户原话：

    > "前端刷新触发后端任务，但是不应该等待后端输出才显示，后端采集信息和
    >   输出需要时间的，容易触发等待时间，可以做监听回调，有信息了再触发
    >   自动刷新前端。"

    实测：`build_feed` 冷 **2.18s** / 热 **1.01s**（并发拉六个源 + 知识星球
    增量，全在这条函数里同步做完）。原来的写法是 `await build_feed(...)`，
    所以每打开一次页面就要盯着空屏看 1~2 秒 —— 而"给缓存加个 TTL"只把
    重复请求变快，**第一次和每一次过期仍然要等**，那正是用户不接受的部分。

    现在的时序：

        GET /feed  ──► 立刻返回进程内缓存里那一份（可能是空的）
                        │
                        └─► 若缓存缺失/过期/显式 refresh → 起一个后台任务
                            后台：build_feed → 落缓存 → WS 推 "intel_feed"
                            前端收到 → 自己再拉一次（用户什么都没点）

    ## 下发字段（在原有键之上**追加**，老消费方不受影响）

        cached        这一份是不是缓存里来的（永远 true，除非冷启动）
        refreshing    后台是否正在重建（前端据此显示"正在采集…"并启动等待）
        built_at      这一份的生成时刻（ISO，前端可显示"数据截至"）
        age_seconds   这一份多旧（秒；冷启动为 null）
        seq           全局递增序号（前端只比较它 —— 见 `_FEED_SEQ`）

    ## 筛选为什么在服务端做（而且要在**大池子**上做）

    前端过滤只能过滤"已经取回来的这一页"（默认 60 条）。而实测那 60 条里
    最高分只有 78 —— 于是「高可信 ≥80」会**永远空**，而全量里其实有
    30 条 80+ 的。用户看到的是"没有高可信信息"，那是**假的**。

    所以筛选在 `build_feed` 内部做：它在 `FILTER_POOL`（500 条）的池子上
    筛选，再截到 `limit`。`limit` 是**返回条数**，不是池子大小。
    """
    await require_feature(request, FEATURE_HOT)

    from src.domain.intel.credibility import FILTERS

    if filter not in FILTERS:
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_filter",
                    "message": f"filter 必须是 {'/'.join(FILTERS)} 之一"})

    watch = [c.strip() for c in codes.split(",") if c.strip()]

    # 非法的 direction **报错**而不是静默按 all 处理：静默会让用户以为
    # 筛过了，实际看到的是全部 —— 那是"筛选没生效"里最难查的一种。
    if direction not in _DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_direction",
                    "message": f"direction 必须是 {'/'.join(_DIRECTIONS)} 之一"})

    cache_key = feed_cache_key(limit=limit, watch=watch, sort=sort,
                               filter=filter, direction=direction)

    entry, age = _feed_cached(cache_key)
    if entry is None or age is None:
        stale = True                    # 冷启动 / 该参数组合第一次被访问
    elif refresh:
        # 显式刷新**绕开 TTL**，但不绕开下限：刚建好的那份直接复用。
        # （用户连点刷新时，每一次都重跑采集等于自己打自己的上游 —— 见
        #   `FEED_REFRESH_FLOOR`。请求本来就不等待，所以这里省的是上游，
        #   不是用户的时间。）
        stale = age >= FEED_REFRESH_FLOOR
    else:
        stale = age >= FEED_CACHE_TTL

    if stale:
        _kick_feed_build(request, cache_key, watch=watch, limit=limit,
                         sort=sort, filter=filter, direction=direction)

    if entry is None:
        # ★ 冷启动：**不等待**，先给一个形状正确的空壳。
        # `refreshing=True` 是前端唯一的判据 —— 它据此显示"正在采集…"
        # 并开始等待通知；`seq=0` 让"还没有任何一份数据"可被识别
        # （前端拿 0 与 status 的 seq 比较）。
        payload = _feed_placeholder(limit=limit, filter=filter, sort=sort)
        payload.update(cached=False, refreshing=True, built_at="",
                       age_seconds=None, seq=0, direction=direction)
        return payload

    payload = dict(entry.payload)
    payload.update(
        cached=True,
        # 回显**本次请求**的方向档。
        #
        # ⚠️ 在这里回填而不是烘进缓存的 payload（`_feed_public_payload`）：
        # 缓存条目是按 key 存的，同一份缓存只会被它自己那个 key 读到 ——
        # 但 `filter` 是烘进去的（历史写法），方向档跟它对齐更一致；
        # 而"回显请求参数"这件事本质上属于**响应**而非缓存内容。
        direction=direction,
        # `stale or 正在重建`：只写 `stale` 会漏掉"刚好在这一刻建完"那一瞬
        # （客户端就再也等不到提示了）；只写"正在重建"会漏掉"已经建完但
        # 客户端还没拉到"那一档。两者取或，前端再靠 `seq` 收敛。
        refreshing=bool(stale or _feed_building(cache_key)),
        built_at=entry.at_iso,
        age_seconds=round(age, 1),
        seq=entry.seq,
    )
    return payload


@router.get("/feed/status")
async def intel_feed_status(request: Request) -> dict[str, Any]:
    """情报流后台重建状态：`{building, seq, built_at, age_seconds, ttl}`。

    ## 为什么它必须极廉价

    它是 WebSocket 推送的**兜底**：隧道抖动 / WS 断线时前端会在这里短轮询
    （见 `IntelPanel` 的 `READY_POLL_MS`），所以这条接口**只读进程内计数器** ——
    零 I/O、零网络、零采集。任何一次"顺手在这里查一下缓存文件"都会让它
    从微秒级变成毫秒级，而它是被高频调用的那一条。

    ## 为什么不需要任何参数（前端也不必复刻缓存键）

    返回的是**全局**序号：任何一次成功重建都会 +1（见 `_FEED_SEQ`）。
    前端只需要比较"这个序号是不是比我手里那份大"。若改成按 key 查，
    前端就得把 `limit|codes|sort|filter` 的拼法抄一遍 —— 那种抄写
    一旦漂移（比如服务端多了一个参数），兜底轮询会**永远看不到新数据**，
    而且不报任何错，只是"偶尔要手动刷新"。

    ⚠️ 不含任何情报内容，也不含条数 —— 它是信令，不是数据。
    """
    await require_feature(request, FEATURE_HOT)
    return _feed_status()


@router.get("/item/{content_hash}")
async def intel_item(content_hash: str, request: Request) -> dict[str, Any]:
    """单条**全文**（用户口径 2026-10-01："点击可以看全文"）。

    ## 为什么单独一个端点，而不是把全文塞进 `/feed`

    `summary` 在契约层被截到 260 字（移动端一条 3400 字占满十屏），
    而这条接口回答的是"**我点了这一条**，把原文给我"。两者的取数时机
    与体积都不同：情报流一次几十条，全文一条就上千字。

    ⚠️ **全文绝不出现在 `/feed` 的 payload 里**（`IntelFeed.to_public()`
    剥掉 `extract_text`），这条端点不改变那个决定 —— 否则"点击才能看"
    就退化成"一屏几十篇全文"，正是被截断要避免的事。

    ## 零模型调用（硬约束）

    这里是**纯读**：`body_store` 是**采集侧**预先写好的 JSONL，按
    `content_hash` O(1) 命中。本地模型单条 ~770ms，放进请求路径就是
    "+几十秒"（见 `intel.py` 模块 docstring）。

    ⚠️ 写入侧**有两个**（2026-10-01 第六轮补的第二个，见 `body_store`
    模块 docstring）：`_intel_tone_extract`（每 2 小时）与
    `service.build_feed`（每次请求）。后者是这次报障的修复 ——
    原来只有定时任务写，于是"刚采进来、还没排到抽取那一班"的条目
    点开就是 404，而 404 的文案写着"未留存（或已超出 3 天留存窗口）"，
    用户读到的是"这个功能坏了"。

    ## 响应里**不存在**来源标识（这是构造方式决定的，不是脱敏）

    返回的只有六个键：`content_hash` / `title` / `text` / `published_at` /
    `kind_label` / `tone`。**没有** `source_alias` / `platform` /
    `source_name` / URL —— 白名单式构造（与 `IntelItem.to_public()` 同一条
    纪律）：将来存储行里多一个字段，它默认**不出**。
    所以这条接口不需要"记得脱敏"就能保持渠道匿名。

    ⚠️ `kind_label` 由 `KIND_LABELS` **现算**，不从存储里读：
    标签是展示层的事，存下来就会在改名时留下历史值（用户刚把
    `research_note` 的展示名改成「券商作文」）。

    ## 404 的**两种成因必须分开说**（用户口径 2026-10-01，第六轮）

    用户报障看到的是前端那句「全文读取失败，请稍后重试」——
    而实际发生的是 404。两个问题叠在一起：

      ① 后端把"没留存/过期"与"长垃圾串"都当成**同一个** `item_not_found`，
         前端只拿得到这个码，**分不出**"这条太老了"与"编号是垃圾"；
      ② 前端的兜底文案把 404 归到了"稍后重试"那一类 —— 而"这条超出留存窗口"
         **重试一万次也不会好**，让用户对着一个永远不会成功的按钮点下去，
         是最坏的一种提示。

    所以拆成**两个错误码**（与 `errors.ts` 的分层报错码同一套约定：
    具体成因用稳定的 `code` 表达，人话文案在前端表里）：

        item_bad_hash      指纹是空串 / 超长垃圾串（压根不是一条记录）
        item_not_retained  存储里没有这条（没采到 / 落库失败 / 已从窗口清掉）

    ⚠️ 这是一次**对外契约的收紧**：原来两个分支共用一个码。
    改动是显式的（前端 `errors.ts` 的 `COPY` 表里登记了两条新文案），
    不是"顺手改个字符串"—— 旧码在新前端上会走进兜底文案，仍然读得通。

    ## ⚠️ `not_retained` 里**不细分**"从没采到"与"已过期"

    那两者在存储里长得**一模一样**（都只是"没有这一行"）。
    编一个具体说法（"已过期"）就是假装我们知道 —— 而用户据此做的判断
    （"再等等就有了" vs "这条永远读不到"）会直接错。
    """
    await require_feature(request, FEATURE_HOT)

    from src.domain.intel import body_store, tone_store

    h = (content_hash or "").strip()
    # 长度闸门只是**防超长垃圾串**（日志/内存），不是安全性判据：
    # 真正的判据是"存储里有没有这条"。
    if not h or len(h) > 128:
        raise HTTPException(
            status_code=404,
            detail={"code": "item_bad_hash",
                    "message": "这条记录的编号无效，读不到全文"})

    hit = body_store.get(h)
    if hit is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "item_not_retained",
                    "message": f"该条全文未留存（或已超出 "
                               f"{body_store.RETAIN_DAYS} 天留存窗口）"})

    body = body_store.view(hit)
    # 倾向**按同一个 `content_hash` join `tone_store`**（不随正文一起存）：
    # 抽取与全文是两条独立落库的链路，存两份必然漂移（界面上"全文"里是
    # 昨天的倾向）。没抽过就给"未定"（`view` 的默认值），**不编**。
    tone = tone_store.view(tone_store.get(h))["tone"]
    kind = body["kind"]
    return {
        "content_hash": body["content_hash"],
        "title": body["title"],
        "text": body["text"],
        "published_at": body["published_at"],
        "kind_label": KIND_LABELS.get(kind, kind),
        "tone": tone,
    }


async def _build_heat(feed: Any, *, limit: int) -> dict[str, Any]:
    """给情报流附上「平台热议」区块：具体个股 + 具体事件，不是统计量。

    三块内容各有分工：

      stocks   **确定性**扫出来的"哪些 A 股正在被多个平台提到"
               （名字/代码/提及条数/平台/原文证据都可核对）
      topics   模型聚合出的"讨论最集中的事件"
      rank     各平台人气/热搜排名（含中文名与关注指数）

    ⚠️ 全部失败也要**如实给 gaps**，不能让前端显示成"今天没人讨论"。
    """
    from src.domain.intel import hot_job, hot_rank, hot_scan

    gaps: list[dict[str, str]] = []

    # 扫的池子用 `feed.scan_pool` —— **收容与截断之前**的那份平铺池。
    #
    # ⚠️ 为什么不是"和 HOT_SCAN_POOL 比大小"：早先这里拿 `feed.items` 当池子，
    # 而它是**展示口径**（`limit` 截断 + 收容组把几十条并成一行），实测只剩 2 条，
    # 于是 `len(pool) < 300` 恒成立 → **每个请求都整跑一遍六源聚合**（+1.4 秒）。
    #
    # 换成 `scan_pool` 之后也不行：它受 `FILTER_POOL=500` 采样与"中性条目已丢"
    # 两道影响，实测只有 126~148 条，**仍然小于 300** —— 条件照样恒成立。
    # 根因是拿"期望的池子大小"去衡量"上游实际给了多少"，而后者我们无从控制。
    #
    # 所以判据改成**看有没有**，不看大小：有 `scan_pool` 就用它（它就是
    # `build_feed` 这一步能拿到的全部条目），只有拿不到时才兜底再聚合一次。
    pool = [dict(x) for x in (getattr(feed, "scan_pool", None) or [])]
    if not pool:
        # 例外路径：`feed` 不是 `build_feed` 造的（拿不到 scan_pool）。
        # 正常路径下这份池子有一百多条，走不到这里。
        pool = [dict(x) for x in feed.items]
    if not pool:
        try:
            bigger = await build_feed(limit=HOT_SCAN_POOL, sort="time",
                                      group_undetermined=False)
            pool = [dict(x) for x in bigger.items]
        except Exception as exc:  # noqa: BLE001 扫不全不影响主流程
            logger.warning("热议扫描取全量失败：%s", type(exc).__name__)

    try:
        stocks = [h.to_public() for h in hot_scan.scan_items(pool)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("热议扫描失败：%s", type(exc).__name__)
        stocks = []
        gaps.append({"kind": "heat_scan", "message": "热议个股扫描失败"})

    agg = hot_job.load()
    topics = agg.get("topics") or []
    if not topics:
        gaps.append({"kind": "heat_topics",
                     "message": "热议事件尚未聚合（定时任务每 2 小时一次）"})

    # ── 平台人气榜：**优先读定时任务落盘的结果**（0 ms），过期才现抓 ──
    #
    # 这一段原来是 `await asyncio.wait_for(fetch_hot_rank(), 6.0)` ——
    # 每次请求 3.0~3.5 秒，是情报流"打开要等 5 秒"的主因（见
    # `HOT_RANK_MAX_AGE` 的说明）。现在有新鲜落盘就直接用。
    #
    # ⚠️ 重试节奏看 **`tried_at`**（含失败的尝试），展示看 **`at`**（最近一次成功）：
    # 只看 `at` 会让主源宕机期间每个请求都重试一次（退回 3 秒／次）。
    rank_data = hot_rank.load()
    retry_age = hot_rank.age_seconds(rank_data, key="tried_at")
    if not rank_data.get("rows") or retry_age is None or retry_age > HOT_RANK_MAX_AGE:
        try:
            rank_data = await asyncio.wait_for(hot_rank.refresh(),
                                               timeout=HOT_RANK_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 热榜失败不能拖垮情报流
            logger.info("热榜获取失败：%s", type(exc).__name__)
    rank_rows = list(rank_data.get("rows") or [])
    data_age = hot_rank.age_seconds(rank_data)
    if not rank_rows:
        gaps.append({"kind": "hot_rank", "message": "平台人气榜当前不可用"})
    elif data_age is not None and data_age > HOT_RANK_MAX_AGE:
        # 手里是旧榜：继续用，但把"多旧"一起给出去，前端可以显示"数据截至"，
        # 而不是假装它是刚抓的。
        gaps.append({"kind": "hot_rank_age",
                     "message": f"平台人气榜为 {int(data_age // 60)} 分钟前的结果"})

    return {
        "at": agg.get("at") or "",
        "stocks": stocks,
        "topics": topics,
        "rank": rank_rows,
        "rank_at": str(rank_data.get("at") or ""),
        "stocks_scanned": len(pool),
        "gaps": gaps,
        "note": (
            "「热议个股」为**名称精确匹配**结果（本地个股名录），"
            "每条都给出来源平台与原文标题，可自行核对；"
            "「利好/利空」仅为对第三方原文语气的归类，不构成投资建议。"),
    }


@router.get("/calendar")
async def intel_calendar(
    request: Request,
    horizon_days: int = Query(default=30, ge=1, le=180,
                              description="展望天数"),
) -> dict[str, Any]:
    """投资日历：预约披露 / 限售解禁 / 宏观发布 / 交易日。

    每类**主备互用**（见 `src/domain/intel/calendar.py`）：
    主源失败自动走备源，全失败则进 `gaps` 并置 `degraded=true`。

    ⚠️ **交易日历刻意无备源** —— 交易日是交易所规则，不存在"第二个可信来源"，
    用不可信的日历会让整个调度在错误的日子跑，后果比"日历不可用"严重。

    合规：只呈现**已公布的日程**与**覆盖范围统计**，
    不含方向判断、不给目标价、不给买卖时点。
    """
    await require_feature(request, FEATURE_HOT)

    from src.domain.intel.calendar import build_calendar

    async def _build() -> dict[str, Any]:
        res = await build_calendar(horizon_days=horizon_days)
        payload = res.to_public()
        payload["disclaimer"] = (
            "本日历只呈现已公布的日程安排与覆盖范围统计，"
            "不含方向判断，不构成投资建议。"
            "日程可能变更，请以交易所与公司公告为准。")
        return payload

    # ★ 走慢聚合缓存（命中则**零上游**）。实测这一条原来是 11.1 秒：
    #   宏观源按天逐个请求财经日历，光它就占 8.7 秒（见 `calendar.py`
    #   的 `FEED_FETCH_MAX_DAYS`）。而日程类内容一天之内几乎不变。
    try:
        return await _slow_payload(f"calendar_{horizon_days}", _build)
    except Exception as exc:  # noqa: BLE001
        logger.exception("投资日历聚合失败")
        raise HTTPException(
            status_code=503,
            detail={"code": "calendar_unavailable",
                    "message": brief(exc) or "投资日历暂时不可用"}) from exc


@router.get("/sources/health")
async def intel_sources_health(request: Request) -> dict[str, Any]:
    """各源最近一次采集健康度 —— **只给聚合状态，不按源名细分**。

    为什么聚合而不细分：`{"zsxq_48848484411448": {...}}` 这种形状
    等于把群组 ID 直接印在响应里。前端只需要知道"采集是否正常"。
    """
    await require_feature(request, FEATURE_HOT)

    from src.infrastructure.connectors.intel_sources import health

    h = health()
    total = len(h)
    ok = sum(1 for v in h.values() if v.get("ok"))
    return {
        # 聚合口径：不暴露有几个源、分别叫什么
        "state": "healthy" if total == 0 or ok == total else "degraded",
        "sources_total": total,
        "sources_ok": ok,
        # 最近一次的耗时区间（性能用，不含来源标识）
        "last_ok_ms": max((v.get("ms", 0) for v in h.values()
                           if v.get("ok")), default=0),
    }


# ======================================================================
# 盘前简报 —— **已下线**（2026-09-25）
# ======================================================================
#
# 用户口径：权限矩阵里"删除 舆情情报 · 盘前简报 这一项"。
# 它本来就没有前端入口（`IntelBriefTab` 组件存在但从未被任何页面挂载，
# 全仓库搜不到引用），所以删掉的是**一个没有任何调用方的端点**。
#
# 为什么不留着"反正没人调"：
#   · 它的功能 key `intel.brief` 已从 `FEATURES` 删除 → 保留的话
#     任何调用都会 403，而 403 的文案是"请联系管理员开通"，
#     会让人以为"这个功能还在、只是没给我开"（实际是已下线）；
#   · 端点本身就是可探测的接口面，下线功能留接口是白留一处面。
#
# 要恢复：从 git 历史里取回 `/brief` 处理器，并在 `FEATURES` 里把
# `intel.brief` 加回去（两处必须同时做，否则又是"能勾但 403"）。


# ======================================================================
# 事件处置（告警中心）
# ======================================================================

class AlertStateBody(BaseModel):
    state: str = Field(description="ack（已确认）| ignore（已忽略）| open（重新打开）")
    note: str = Field(default="", max_length=200, description="处置备注")


@router.post("/alerts/{alert_id}/state")
async def set_alert_state(alert_id: str, body: AlertStateBody,
                          request: Request) -> dict[str, Any]:
    """处置一个事件（确认 / 忽略 / 重新打开）。

    只改**状态与备注**，不删数据 —— 审计链要能回溯"谁在什么时候处置了什么"。
    """
    user_id, _ = await require_feature(request, FEATURE_ALERTS)

    allowed = {"ack", "ignore", "open"}
    if body.state not in allowed:
        raise HTTPException(
            status_code=422,
            detail={"code": "bad_state",
                    "message": f"state 必须是 {'/'.join(sorted(allowed))} 之一"})

    # 角色维度：处置动作走 policy（合规/审计只读）
    from src.core.policy import WRITE_ALERT_STATE, authorize

    decision = authorize(action=WRITE_ALERT_STATE, user_id=user_id)
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden",
                    "message": decision.reason or "当前角色无权处置事件"})

    # 事件存储由 alerts 模块负责；此处只做状态变更与留痕。
    from src.domain.alerts import service as alerts_service

    try:
        await asyncio.to_thread(
            alerts_service.set_state, alert_id, body.state,
            operator=user_id, note=body.note)
    except AttributeError:
        # alerts 模块尚未提供该入口 → 明确报"未实现"，不要假装成功。
        # （假装成功会让前端显示"已处置"而库里没变，是最坏的一类静默故障）
        raise HTTPException(
            status_code=501,
            detail={"code": "not_implemented",
                    "message": "事件处置存储尚未接入，请联系开发者"}) from None
    except Exception as exc:  # noqa: BLE001
        logger.exception("事件处置失败 alert_id=%s", alert_id)
        raise HTTPException(
            status_code=400,
            detail={"code": "alert_state_failed",
                    "message": brief(exc) or "处置失败"}) from exc

    return {"ok": True, "alert_id": alert_id, "state": body.state,
            "operator": user_id}

# ======================================================================
# 舆情热度（热议榜 + 热门个股 + 热议事件）
# ======================================================================

@router.get("/heat")
async def intel_heat(request: Request) -> dict[str, Any]:
    """舆情热度：**具体内容**，不是统计量。

    > 用户口径（2026-09-25）："舆情热度关注的是各大金融平台 贴吧里的热议
    > 事件、新闻热搜事件，对应的行业、股票，其利好利多分析……
    > 需求是具体的内容，而不是统计数量。"

    ## 与上一版的区别（用户明确说上一版"毫无使用价值"）

    **删掉**：来源类型结构、按日分布、被提及标的（6 位代码）、纯数字倾向分布。
    它们的共同问题是只回答"有多少"，不回答"发生了什么"。

    **改为**：

      · hot_rank 各平台人气/热搜排名（**升幅**排名靠前 —— 变化才含信息）
      · stocks   被讨论最多的股票 + 行业 + 事件摘要 + **利好/利空**
      · 	opics   讨论最集中的事件/主题 + 关联个股

    ## 数据从哪来

      · 热榜   免费公开接口（hot_rank.py，多源主备 + 重试）
      · 个股/事件  本地模型把研报/笔记整批聚合（hot_topics.py），
                 并**校验名字必须在原文出现**（编一个股票名用户看不出来）

    ⚠️ 原项目用过雪球/股吧/同花顺/韭研公社，但那是经 Tavily 搜索 API
    （需 key）取的。本端点只收**免费可直取**的源；取不到的如实进 gaps，
    不假装"今天没人讨论"。
    """
    await require_feature(request, FEATURE_HOT)

    # ★ 走慢聚合缓存。实测这一条原来 3.5 秒，全部来自 `fetch_hot_rank`
    #   （外部热榜多源主备 + 重试）。而热榜的**排名**是缓变的，
    #   聚合结果本身还是"每 2 小时一次"的定时任务产物 —— 分钟级重拉
    #   拿不到实质新内容，只会白打上游。
    return await _slow_payload("heat", _build_heat_payload)


async def _build_heat_payload() -> dict[str, Any]:
    """从上游现算一份热度载荷（`/heat` 的实体，也被预热循环复用）。"""
    from src.domain.intel import hot_job, hot_rank

    gaps: list[dict[str, str]] = []

    # 热榜与聚合结果**互不依赖**，并发取
    rank_task = asyncio.create_task(hot_rank.fetch_hot_rank())
    try:
        agg = hot_job.load()
    except Exception as exc:  # noqa: BLE001 聚合结果读不到不影响热榜
        logger.warning("热度聚合结果读取失败：%s", type(exc).__name__)
        agg = {}

    try:
        rank = await rank_task
    except Exception as exc:  # noqa: BLE001
        logger.warning("热榜获取失败：%s", type(exc).__name__)
        rank = None

    rank_rows: list[dict[str, Any]] = []
    if rank is not None and rank.ok:
        rank_rows = [r.to_public() for r in rank.rows]
    else:
        # 全失败要**如实记缺口**，不能静默给空列表 ——
        # 那会让用户以为"今天没有热门股"
        gaps.append({"kind": "hot_rank",
                     "message": "热议榜当前不可用（数据源暂无响应）"})

    stocks = agg.get("stocks") or []
    topics = agg.get("topics") or []
    if not stocks and not topics:
        gaps.append({"kind": "hot_topics",
                     "message": "热门个股与事件尚未聚合（定时任务每 2 小时一次）"})

    return {
        "at": agg.get("at") or "",
        "notes_used": agg.get("notes_used") or 0,
        "hot_rank": rank_rows,
        "stocks": stocks,
        "topics": topics,
        "gaps": gaps,
        "degraded": bool(gaps),
        "disclaimer": (
            "本页为公开信息的聚合与统计：热议榜来自公开人气/热搜排名；"
            "全部内容仅供参考，不构成投资建议。"),
    }
