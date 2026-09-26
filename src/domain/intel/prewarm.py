"""情报流**后台预热**：把"重建"从请求路径挪到启动期与后台周期。

## 用户口径（2026-09-25）

> "服务启动就加载已有数据。用户打开本页就直接加载已有数据，然后后台检查
>   是否是工作日且相比上次抓取间隔2小时，若满足则记录此刻时间并执行一次
>   后台抓取调用，获取新数据操作，记录时间+15分钟后自动获取后台新抓取的新
>   数据，再推送到前端，这样就不存在首屏冷启动。"

## 预热解决的到底是什么

`GET /feed` 的契约是**请求永不等待采集**（`routes/intel.py` 的端点 docstring
专门论证过）：冷启动时先下发**空壳**，后台重建完再经 WebSocket 通知前端回拉。
于是用户看到的是"先空着几秒、再刷出内容"。

实测（2026-09-25 性能剖析）：一次完整重建 **2.6~3.3 秒**，其中 zsxq 分页
~1.2s + `fetch_all` ~0.5s 全是网络，CPU 只占 ~0.17s。这 3 秒就是"首屏冷启动"
的全部代价。

预热把它挪走：**启动后**（以及此后每 15 分钟）在后台检查是否该抓，该抓就抓，
抓完落缓存 + WS 通知。于是：

    · 服务刚起 → 90 秒后预热自己跑起来，用户此时打开页面往往已经就绪；
    · 用户打开页面 → 直接读已建好的缓存（**0 网络、几十毫秒**）；
    · 后台到点了 → 抓新的 → 落缓存 → 通知前端 → 界面自己更新。

## 为什么"间隔 2 小时"而不是每次都抓

`fetch_incremental`（知识星球）的正常节奏就是 2 小时一轮，快讯源也是分钟级。
2 小时内重抓拿不到实质新内容，只会白打上游 —— 而"自己打自己的上游"在本项目
是有过代价的教训（`FEED_REFRESH_FLOOR` 的注释记过）。

## 与接口层缓存的分工

预热**不引入新的缓存**：它调的就是接口后台重建走的那条路
（`_build_feed_bg` → 同一个 `_FEED_CACHE` 键），所以预热出来的那份与
"用户请求触发的那份"是同一种东西，不存在两套口径漂移。

## 交易日判定：只用**日历**，不用行情时钟

用户口径里的"工作日"按**本机日历**判（周一~周五）。这里刻意**不**用
`intraday.auto_select.is_trading_day()` —— 它依赖行情源时钟，而情报流在
**非交易时段同样有内容**（知识星球的券商研报盘后与夜里都在发，这是它相对
快讯的价值所在）。用行情时钟判"今天休市就不抓"会让周末与节假日的研报
整段拿不到，那是**功能缺失**而不是省成本。

省成本的目标由"2 小时间隔"承担，它已经足够 —— 不必再拿休息日去卡。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.config import Settings, get_settings
from src.core.errors import BRIEF_TIGHT, brief

logger = logging.getLogger(__name__)

#: 上次**成功抓取**时刻的落盘位置。
#:
#: 为什么必须落盘而不是只放内存：用户口径要的是"相比上次抓取间隔 2 小时"，
#: 而进程重启会让内存里那份归零 —— 于是每次重启都立刻重抓一次，2 小时的
#: 间隔形同虚设（本仓的"内存态节流"已经因为这个原因踩过坑，见
#: `intraday/notify_dedup` 的模块 docstring）。
DEFAULT_STATE_PATH = Path("data/cache/intel/prewarm_state.json")

#: 上次成功重建的 **payload 快照**（落盘），供**下次启动**热加载。
#:
#: ## 为什么必须有它（否则"首屏冷启动"只解决了一半）
#
# 预热循环有启动延迟（默认 90 秒）。若用户在延迟期内打开页面，接口缓存还是
# 空的 —— 端点只能下发空壳，用户照样看到"正在采集"。光有"后台抓取"不够，
# **还要让"已抓到的数据"跨重启活下来**：
#
#     启动 → 载回这份 payload 进 `_FEED_CACHE` → 首个请求**命中真实数据**（0 IO）
#          → 90 秒后预热再检查该不该刷新
#
# 尺寸实测 **~220 KB**（60 条 items + heat），一次原子写，代价可忽略。
# 这与做T自选池的 `hot_cache`、主线热快照是同一套"落盘热加载"范式。
DEFAULT_PAYLOAD_PATH = Path("data/cache/intel/feed_payload.json")

_ISO = "%Y-%m-%dT%H:%M:%S"

#: 本进程是否已经用 `warning` 报过一次"跳过"。
#:
#: 为什么需要它：绝大多数 tick 都会跳过（2 小时才抓一次、15 分钟一跳），
#: 全用 warning 就是刷屏；全用 info 又会被 `main.py` 那个未配 handler 的
#: logger 丢掉，于是"循环到底活着吗"在日志里看不出来。取中间：**每进程
#: 第一条跳过用 warning**，之后静默 —— 既确认了循环起来了，也不刷屏。
_skip_logged = False


def _path_of(path: str | Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_STATE_PATH


def _payload_path_of(path: str | Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_PAYLOAD_PATH


def _write_json_atomic(target: Path, payload: Any) -> bool:
    """原子写 JSON（临时文件 + `os.replace`）。失败只记日志、返回 False。"""
    tmp = target.with_suffix(target.suffix + f".tmp{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)          # 原子替换：读方永远看到完整文件
        return True
    except Exception as exc:  # noqa: BLE001 落盘失败只影响下次启动，不该中断当前流程
        logger.warning("情报流缓存落盘失败（%s）：%s", target.name,
                       brief(exc, BRIEF_TIGHT))
        tmp.unlink(missing_ok=True)
        return False


def save_payload(payload: dict[str, Any], *, path: str | Path | None = None,
                 cache_key: str = "") -> bool:
    """把一份重建好的 payload 落盘（供下次启动热加载）。

    `cache_key` 一起存：载回时要写进**同一个键**，否则请求读的键与预热的键
    不一致（`routes.intel.feed_cache_key` 的注释记过这类漂移的后果）。
    """
    if not payload:
        return False
    return _write_json_atomic(_payload_path_of(path), {
        "version": 1,
        "saved_at": datetime.now().strftime(_ISO),
        "cache_key": str(cache_key or ""),
        "payload": payload,
    })


def load_payload(*, path: str | Path | None = None
                 ) -> tuple[dict[str, Any] | None, str]:
    """读上次落盘的 payload → `(payload, cache_key)`；没有/损坏返回 `(None, "")`。

    ⚠️ **fail-open**：读不到就当没有缓存（端点照常下发空壳 + 后台重建）。
    反过来 fail-closed 会让服务起不来，而"启动不了"比"首屏慢 3 秒"严重得多。
    """
    try:
        raw = json.loads(_payload_path_of(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, ""
    except Exception as exc:  # noqa: BLE001 文件损坏只是没有热缓存
        logger.debug("情报流缓存不可读（按无缓存处理）：%s", brief(exc, BRIEF_TIGHT))
        return None, ""
    if not isinstance(raw, dict):
        return None, ""
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not payload.get("items"):
        # 空 items 的 payload 没有热加载价值（前端还是要等采集完），
        # 而且它会占着"非空壳"的判断分支，让用户看到一片空白而不是"正在采集"。
        return None, ""
    return payload, str(raw.get("cache_key") or "")


def prime_cache(*, path: str | Path | None = None) -> bool:
    """把落盘的 payload 载回接口缓存（**启动时调**）。返回是否成功载入。

    这是"服务启动就加载已有数据"这句口径的落地：载入之后，用户在预热循环
    跑起来之前打开页面，读到的就是**上次抓到的真实数据**，而不是空壳。
    """
    from src.api.routes.intel import _FEED_CACHE, _FEED_LOCK, _FeedEntry, feed_cache_key

    payload, _stored_key = load_payload(path=path)
    if payload is None:
        logger.info("情报流无落盘缓存可载入（首次部署 / 文件被清理）")
        return False
    # ⚠️ **键一律重算，不用文件里存的那个**（`_stored_key` 故意丢弃）。
    #
    # 踩过的坑（2026-09-26 加多/空筛选时）：`feed_cache_key` 的格式加了
    # `direction` 段，而落盘文件里存的是**旧格式**的键。信它的话，载入会
    # 写进一个"请求永远不会读"的键 —— 用户照样吃冷启动，
    # 而日志里明明白白写着"已从落盘缓存热加载"，一个字都不报错。
    #
    # 重算所需的三个参数（limit/sort/filter）本来就在 payload 里
    # （`_feed_public_payload` 写进去的），所以重算不需要额外信息。
    # 这是一条更稳的纪律：**键的拼法只能有一处实现**，
    # 磁盘上的缓存只存"数据"，不存"键应该长什么样"。
    key = feed_cache_key(
        limit=int(payload.get("limit") or len(payload.get("items") or [])),
        watch=[], sort=str(payload.get("sort") or "credibility"),
        filter=str(payload.get("filter") or "all"),
        direction=str(payload.get("direction") or "all"))
    with _FEED_LOCK:
        _FEED_CACHE[key] = _FeedEntry(
            # ⚠️ `at` 用"现在"而不是文件的保存时刻：这是**本进程**刚载入的内存
            # 缓存，年龄从载入算起。用文件时间会让它一启动就"已过期"，
            # 于是首个请求又去触发重建 —— 热加载的意义全没了。
            at=time.monotonic(), at_iso=str(payload.get("fetched_at") or ""),
            seq=0, payload=dict(payload))
    logger.warning("情报流已从落盘缓存热加载：键 %s（%d 条）",
                   key, len(payload.get("items") or []))
    return True


def snapshot_payload(*, state_path: str | Path | None = None,
                     payload_path: str | Path | None = None) -> bool:
    """把当前内存缓存里那份 payload 落盘（供下次启动热加载）。

    由 `_build_feed_bg` 在**每次重建成功后**调用 —— 不只预热那几趟，
    用户请求触发的重建也一起落盘，这样"磁盘上那份"始终跟得上内存里那份。
    """
    from src.api.routes.intel import _FEED_CACHE, _FEED_LOCK

    with _FEED_LOCK:
        if not _FEED_CACHE:
            return False
        key, entry = max(_FEED_CACHE.items(), key=lambda kv: kv[1].at)
        # `dict(entry.payload)` 做一层浅拷贝：序列化发生在锁外，别让并发的
        # 重建把正在序列化的 dict 换掉。
        payload = dict(entry.payload)
    return save_payload(payload, path=payload_path, cache_key=key)


def load_last_fetch(*, path: str | Path | None = None) -> datetime | None:
    """读上次成功抓取时刻；没有 / 读不出来返回 `None`。

    ⚠️ **fail-open**：读不到就当"从没抓过"（于是会抓一次）。反过来
    fail-closed（读不到就当刚抓过）会让预热永远不跑 —— 那是最坏的静默失效：
    界面照常显示，只是数据永远是旧的。
    """
    try:
        raw = json.loads(_path_of(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 文件损坏不该让预热挂掉
        logger.debug("预热状态不可读（按未抓过处理）：%s", brief(exc, BRIEF_TIGHT))
        return None
    stamp = str((raw or {}).get("last_fetch_at") or "")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def save_last_fetch(moment: datetime | None = None, *,
                    path: str | Path | None = None) -> bool:
    """记下这次成功抓取的时刻（原子落盘）。返回是否写成功。"""
    target = _path_of(path)
    payload = {
        "last_fetch_at": (moment or datetime.now()).strftime(_ISO),
        "version": 1,
    }
    tmp = target.with_suffix(target.suffix + f".tmp{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)          # 原子替换：读方永远看到完整文件
        return True
    except Exception as exc:  # noqa: BLE001 记不上时间只影响节流，不该中断预热
        logger.warning("预热状态落盘失败：%s", brief(exc, BRIEF_TIGHT))
        tmp.unlink(missing_ok=True)
        return False


def is_weekday(moment: datetime | None = None) -> bool:
    """本机日历的工作日（周一~周五）。理由见模块 docstring 的"交易日判定"。"""
    return (moment or datetime.now()).weekday() < 5


# ======================================================================
# 慢聚合的落盘缓存（投资日历 / 舆情热度）
#
# ## 为什么与上面的 feed payload 分开
#
# `/feed` 有自己的进程内缓存 + 空壳契约（"请求永不等待采集"），它的瓶颈在
# 网络抖动，几秒内就会重建完。而日历/热度是**分钟级都不会变**的慢聚合，
# 且代价高得多 —— 实测 `/intel/calendar` **11.1 秒**（宏观源按天请求财经
# 日历，30 天 ≈ 30 次 HTTP 就占了 8.7 秒）。
#
# 把它做成"空壳 + 后台重建"是不合适的：日历空着没有任何意义（用户来看的
# 就是日程），而且 11 秒的空窗太长。**正确做法是让它几乎永远命中缓存** ——
# 于是有一个跨重启的磁盘层，配小时级 TTL。
#
# ## 语义（三档，路由层据此决定要不要续期）
#
#     没有文件        → payload=None          → 现拉（首次部署）
#     文件新鲜        → payload + stale=False → 直接返回，**零上游**
#     文件过期        → payload + stale=True  → **先返回旧的**，后台续期
#
# 第三档是关键：宁可给一份"几分钟前"的日程，也不要让用户等 11 秒。
# 日程类数据晚几分钟没有任何实际影响，而"打开页面等十秒"是用户直接投诉的。
# ======================================================================

#: 慢聚合的落盘位置（按名字分文件；两份互不覆盖）。
DEFAULT_SLOW_DIR = Path("data/cache/intel")

#: 这批缓存的**版本号**。改了 payload 结构就 +1 —— 否则启动时会热加载
#: 一份旧结构，前端读到缺字段后在渲染期才炸（比"没有缓存"难查得多）。
SLOW_CACHE_VERSION = 1


@dataclass
class SlowCache:
    """慢聚合缓存的一次读取结果。"""

    payload: dict[str, Any] | None
    #: 这份数据多旧（秒）；没有缓存时为 0。
    age_seconds: float = 0.0
    #: 是否已过 TTL（过期仍会返回 payload，只是同时让调用方去续期）。
    stale: bool = True

    @property
    def hit(self) -> bool:
        return self.payload is not None


def slow_cache_path(name: str, *, directory: str | Path | None = None) -> Path:
    """`name` 对应的缓存文件路径（`name` 只允许字母数字下划线）。"""
    safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "_-")
    return Path(directory or DEFAULT_SLOW_DIR) / f"slow_{safe}.json"


def save_slow(name: str, payload: dict[str, Any], *,
              directory: str | Path | None = None) -> bool:
    """原子落盘一份慢聚合结果。空 payload 不写（空壳没有缓存价值）。"""
    if not payload:
        return False
    return _write_json_atomic(slow_cache_path(name, directory=directory), {
        "version": SLOW_CACHE_VERSION,
        "saved_at": datetime.now().strftime(_ISO),
        "payload": payload,
    })


def load_slow(name: str, *, ttl_hours: float,
              directory: str | Path | None = None) -> SlowCache:
    """读慢聚合缓存，并按 `ttl_hours` 判新旧。

    ⚠️ **fail-open**：文件缺失/损坏/版本不符一律当"没有缓存"（现拉一次）。
    反过来会让页面永远空白，而"偶尔慢一次"远比"永远空"好。

    ⚠️ `ttl_hours <= 0` 视为**关闭缓存**（永远 stale），与
    `source_cache._fresh` 的约定一致 —— 写反会变成"想关缓存却永久缓存"。
    """
    try:
        raw = json.loads(slow_cache_path(name, directory=directory)
                         .read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SlowCache(payload=None, age_seconds=0.0, stale=True)
    except Exception as exc:  # noqa: BLE001 文件损坏只是这次慢一点
        logger.debug("慢聚合缓存不可读（%s，按无缓存处理）：%s",
                     name, brief(exc, BRIEF_TIGHT))
        return SlowCache(payload=None, age_seconds=0.0, stale=True)

    if not isinstance(raw, dict) or raw.get("version") != SLOW_CACHE_VERSION:
        return SlowCache(payload=None, age_seconds=0.0, stale=True)
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not payload:
        return SlowCache(payload=None, age_seconds=0.0, stale=True)

    age = 0.0
    try:
        saved = datetime.fromisoformat(str(raw.get("saved_at") or ""))
        age = max(0.0, (datetime.now() - saved).total_seconds())
    except ValueError:
        # 时间戳坏了 → 当作**很旧**（会触发续期），但数据仍可用。
        age = float("inf")

    if ttl_hours <= 0:
        return SlowCache(payload=payload, age_seconds=age, stale=True)
    return SlowCache(payload=payload, age_seconds=age,
                     stale=age >= ttl_hours * 3600.0)

def should_fetch(*, now: datetime | None = None, min_interval_hours: float,
                 path: str | Path | None = None) -> tuple[bool, str]:
    """现在该不该抓一次？→ `(是否抓, 人话原因)`。

    判据只有两条（用户口径）：**工作日** 且 **距上次抓取已达间隔**。
    原因文案用于日志与运行记录 —— 排查"为什么没抓"时它是唯一的线索。
    """
    moment = now or datetime.now()
    if not is_weekday(moment):
        return False, f"非工作日（{moment:%Y-%m-%d} 周{'一二三四五六日'[moment.weekday()]}）"
    if min_interval_hours <= 0:
        return True, "间隔限制已关闭（min_interval_hours=0）"
    last = load_last_fetch(path=path)
    if last is None:
        return True, "没有上次抓取记录 → 抓一次打底"
    elapsed = moment - last
    need = timedelta(hours=float(min_interval_hours))
    if elapsed >= need:
        return True, (f"距上次抓取 {elapsed.total_seconds() / 3600:.1f} 小时"
                      f"（≥ {min_interval_hours:g} 小时）")
    return False, (f"距上次抓取仅 {elapsed.total_seconds() / 3600:.1f} 小时"
                   f"（< {min_interval_hours:g} 小时），本次跳过")


async def prewarm_once(*, settings: Settings | None = None,
                       hub: Any = None, force: bool = False,
                       state_path: str | Path | None = None) -> dict[str, Any]:
    """检查一次并按需后台抓取。返回结果字典（供日志/运行记录/测试断言）。

    `hub` 是告警 WebSocket hub —— 传了就顺手推送"数据已就绪"。
    不传（或为 None）时只落缓存，前端下次请求/轮询自己会拿到。
    """
    settings = settings or get_settings()
    now = datetime.now()
    outcome: dict[str, Any] = {
        "checked_at": now.strftime(_ISO), "fetched": False, "reason": "",
    }
    if not force:
        need, why = should_fetch(
            now=now,
            min_interval_hours=float(settings.intel_prewarm_min_interval_hours),
            path=state_path)
        outcome["reason"] = why
        if not need:
            # ⚠️ 级别取舍（与 `retention_service` 同一套理由）：本仓
            # `main.py` 的模块 logger 没有配 handler，其 `logger.info`
            # **会被直接丢弃** —— 于是"循环到底活着吗 / 为什么没抓"在日志里
            # 看不出来。所以**第一次**跳过用 warning（每个进程一条，确认循环
            # 起来了且判定正常），之后回到 info 静默：绝大多数 tick 都会跳过
            # （2 小时才抓一次、15 分钟一跳），全用 warning 就是刷屏。
            global _skip_logged
            if not _skip_logged:
                _skip_logged = True
                logger.warning("情报流预热循环已启动，本次跳过：%s", why)
            else:
                logger.info("情报流预热跳过：%s", why)
            return outcome

    # 调**接口后台重建**那条路，而不是自己拼一遍 payload：
    # 它已经负责"build_feed → payload → heat → 落 _FEED_CACHE → WS 通知"，
    # 自己再写一遍必然漂移（`_feed_public_payload` 的注释专门记过这种漂移
    # 表现为"预热出来的那份少了 heat"）。
    from src.api.routes.intel import _build_feed_bg, feed_cache_key
    from src.domain.intel.service import DEFAULT_LIMIT, watchlist_codes

    watch = watchlist_codes()
    key = feed_cache_key(limit=DEFAULT_LIMIT, watch=[], sort="credibility",
                         filter="all")
    try:
        await _build_feed_bg(key, watch=watch, limit=DEFAULT_LIMIT,
                             sort="credibility", filter="all", hub=hub)
    except Exception as exc:  # noqa: BLE001 预热失败绝不能影响服务
        outcome["reason"] = f"抓取失败：{type(exc).__name__}"
        logger.warning("情报流预热抓取失败（忽略）：%s", brief(exc, BRIEF_TIGHT))
        return outcome

    outcome["fetched"] = True
    outcome["cache_key"] = key
    # ⚠️ 只在**成功**后记时间：失败也记的话，2 小时节流会把"失败的这次"
    # 当成"抓过了"，于是后面 2 小时都不再尝试 —— 那是静默失效。
    save_last_fetch(now, path=state_path)
    logger.warning("情报流预热完成：已重建并推送（%s）", outcome.get("reason") or "强制")
    return outcome


async def prewarm_slow(*, force: bool = False,
                       settings: Settings | None = None) -> dict[str, Any]:
    """预热**慢聚合**（投资日历 / 舆情热度）。

    与 `prewarm_once` 的分工：

    * `prewarm_once` 管 `/feed`（走接口的 `_build_feed_bg`，因为它还要 WS 通知）；
    * 本函数管日历/热度 —— 它们不属于 feed 那条链路，也没有 WS 通知，
      只是"算一次很贵、很久不变"的两份 payload。

    判据是**缓存文件是否过期**（与 `should_fetch` 的"距上次抓取间隔"不同）：
    日历/热度没有"水位线"概念，唯一有意义的问题就是"这份缓存还能用吗"。

    `force=True` 时无条件重建（排查用）。失败只记日志、**不抛** ——
    预热是后台增益，绝不能因为它让服务起不来。
    """
    settings = settings or get_settings()
    ttl = float(settings.intel_slow_cache_hours)
    outcome: dict[str, Any] = {"rebuilt": [], "skipped": [], "failed": []}

    from src.api.routes.intel import _build_heat_payload
    from src.domain.intel.calendar import DEFAULT_HORIZON_DAYS, build_calendar

    async def _calendar() -> dict[str, Any]:
        res = await build_calendar(horizon_days=DEFAULT_HORIZON_DAYS)
        payload = res.to_public()
        payload["disclaimer"] = (
            "本日历只呈现已公布的日程安排与覆盖范围统计，"
            "不含方向判断，不构成投资建议。"
            "日程可能变更，请以交易所与公司公告为准。")
        return payload

    # 键必须与路由层**逐字一致**（`calendar_{horizon}`），否则预热写一份、
    # 请求读另一份，用户照样吃 11 秒 —— 而日志里一个字都不报。
    jobs: dict[str, Any] = {
        f"calendar_{DEFAULT_HORIZON_DAYS}": _calendar,
        "heat": _build_heat_payload,
    }

    for name, build in jobs.items():
        if not force:
            cache = load_slow(name, ttl_hours=ttl)
            if cache.hit and not cache.stale:
                outcome["skipped"].append(name)
                continue
        try:
            payload = await build()
        except Exception as exc:  # noqa: BLE001 预热失败不影响服务
            outcome["failed"].append(name)
            logger.warning("慢聚合预热失败（%s）：%s", name, brief(exc, BRIEF_TIGHT))
            continue
        if save_slow(name, payload):
            outcome["rebuilt"].append(name)

    if outcome["rebuilt"]:
        logger.warning("情报慢聚合预热完成：重建 %s（TTL %.1f 小时）",
                       "、".join(outcome["rebuilt"]), ttl)
    return outcome


def slow_cache_ready(*, settings: Settings | None = None) -> dict[str, Any]:
    """慢聚合缓存当前状态（**只读**，供启动日志与观测）。

    启动时打一行"日历/热度缓存是否已就绪"，是为了让"首屏会不会慢"
    这件事在日志里直接可判 —— 而不是等用户报障再去量。
    """
    settings = settings or get_settings()
    ttl = float(settings.intel_slow_cache_hours)
    from src.domain.intel.calendar import DEFAULT_HORIZON_DAYS

    out: dict[str, Any] = {}
    for name in (f"calendar_{DEFAULT_HORIZON_DAYS}", "heat"):
        cache = load_slow(name, ttl_hours=ttl)
        out[name] = {
            "hit": cache.hit,
            "age_seconds": round(cache.age_seconds, 1) if cache.hit else None,
            "stale": cache.stale,
        }
    return out


async def run_prewarm_loop(*, hub: Any = None, settings: Settings | None = None,
                           state_path: str | Path | None = None,
                           tick_seconds: float | None = None,
                           startup_delay: float | None = None,
                           max_rounds: int | None = None) -> int:
    """预热后台循环：启动延迟 → 每 `tick` 检查一次该不该抓。

    返回值是实际"抓取"的次数（供测试断言；生产上无人消费）。

    ## 两个参数是给测试用的

    `max_rounds` 让用例不必真的等 15 分钟；`tick_seconds` / `startup_delay`
    同理。生产路径一律走 `Settings`（见 `intel_prewarm_*`）。
    """
    settings = settings or get_settings()
    tick = float(tick_seconds if tick_seconds is not None
                 else settings.intel_prewarm_tick_seconds)
    delay = float(startup_delay if startup_delay is not None
                  else settings.intel_prewarm_startup_delay_seconds)
    if delay > 0:
        logger.info("情报流预热将在 %.0f 秒后开始首次检查", delay)
        await asyncio.sleep(delay)

    fetched_rounds = 0
    rounds = 0
    while True:
        rounds += 1
        try:
            outcome = await prewarm_once(settings=settings, hub=hub,
                                        state_path=state_path)
            if outcome.get("fetched"):
                fetched_rounds += 1
            # ★ 慢聚合（日历/热度）每轮都查一次"过期了没" —— 它自己的
            #   判据是缓存文件年龄，与 feed 的 2 小时间隔**无关**，所以
            #   不受上面那个 skipped 分支影响。过了 TTL 就顺手在后台重建，
            #   用户下一次打开页面时已经是热的。
            await prewarm_slow(settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 循环必须活着（一次失败不该停掉预热）
            logger.warning("情报流预热循环异常，下一轮继续", exc_info=True)
        if max_rounds is not None and rounds >= max_rounds:
            return fetched_rounds
        await asyncio.sleep(max(1.0, tick))


def prime_slow(*, settings: Settings | None = None) -> bool:
    """启动时**立刻**热加载慢聚合缓存 —— 不重建，只是让"已有什么"可见。

    返回是否存在**新鲜**缓存。为什么只报不建：日历经实测要 11 秒，
    放在启动路径里会把首屏/行情预热挤在一起（本文件里有多处同类教训）。
    真正的重建交给 `run_prewarm_loop` 的后台轮次。

    但要**当场把状态写进日志**：这样"首屏会不会慢 10 秒"是启动日志里
    一眼可判的事实，而不是等用户报障再回来量。
    """
    state = slow_cache_ready(settings=settings)
    ready = all(v["hit"] and not v["stale"] for v in state.values())

    def _one(name: str, v: dict[str, Any]) -> str:
        if not v["hit"]:
            return f"{name}=无"
        tag = "过期" if v["stale"] else "新鲜"
        return f"{name}={tag}({v['age_seconds']}s)"

    detail = "、".join(_one(k, v) for k, v in state.items())
    if ready:
        logger.warning("情报慢聚合缓存已就绪，首屏无需现拉：%s", detail)
    else:
        logger.warning("情报慢聚合缓存未就绪（首次部署或已过期），"
                       "首屏可能需现拉一次；后台预热将补上：%s", detail)
    return ready


__all__ = [
    "DEFAULT_SLOW_DIR",
    "DEFAULT_STATE_PATH",
    "SLOW_CACHE_VERSION",
    "SlowCache",
    "is_weekday",
    "load_last_fetch",
    "load_slow",
    "prewarm_once",
    "prewarm_slow",
    "prime_cache",
    "prime_slow",
    "run_prewarm_loop",
    "save_last_fetch",
    "save_slow",
    "should_fetch",
    "slow_cache_path",
    "slow_cache_ready",
]
