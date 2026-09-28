"""SmartFetcher：freshness-aware 智能数据获取。

## 解决的问题

原 supervisor 流程：
  1. `_collect_one(indicator)` 总是先调网络（akshare/tushare/...）
  2. 失败 → `_storage_fallback` 读 DB 最近一条

缺点：
  - **每个指标都付网络握手 + TLS 成本**（0.85~0.9s/次，实测）
  - **完全不利用 DB 已有数据**：哪怕 CPI 是 30 天前入库的，也照样重新拉一遍
  - **20 指标并发**，每个都打外网 = 20× 网络延迟

SmartFetcher 改造：
  1. **元数据查表**（CatalogRepository.bulk_get）一次性拿全 freshness
  2. **FreshnessState.is_fresh 判定**：freshness_hours 内有数据 → DB-only（不联网）
  3. **stale/missing → 走网络**（按需，并行度可配）
  4. **网络回来的新数据 → 写库 + 更新 catalog 索引**（下次直接走 DB-only）
  5. **完全失败**（网络 + DB 都没有）→ 沿用原 `_storage_fallback` 行为，诚实上报缺口

## 性能基线

原 collect 阶段 13 指标 cold start：
  - 13× 网络延迟（max 23.4s fed:rate_prob:next）= ~25s

SmartFetcher warm cache（同 query 第二次起）：
  - bulk_get（毫秒级）+ freshness check（in-process）= ~50ms
  - **−25s 冷启 → 0.05s 热命中**

## 接口设计

```python
result = await smart_fetcher.fetch_many(
    ["CPI", "PPI", "mkt:turnover:total", ...],
    progress_token=cancel_token,
)
# result.from_db = ["CPI", "PPI"]        # 走 DB
# result.from_network = ["mkt:..."]      # 联网拉
# result.missing = ["us_fed_rate"]       # 拿不到
# result.data["CPI"] = [DataPoint, ...]
# result.elapsed_ms = 2340
```
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from src.core.cancel import CancellationToken
from src.core.schemas import DataPoint
from src.infrastructure.catalog.catalog_repo import (
    CatalogRepository,
    get_catalog_repository,
)
from src.infrastructure.catalog.frequency_infer import (
    infer_from_rows,
    parse_period_date,
)
from src.infrastructure.catalog.registry import (
    FreshnessState,
    IndicatorMeta,
    get_registry,
)
from src.infrastructure.repositories.base import DataPointRepository

logger = logging.getLogger(__name__)


#: 网络获取函数签名：`async (indicator, start_date, end_date) -> list[DataPoint]`
LiveFetcher = Callable[[str, str | None, str | None], Awaitable[list[DataPoint]]]


@dataclass
class SmartFetchResult:
    """一次批量 fetch 的完整结果（含每指标的来源标记）。"""

    # 已取到的所有数据点（合并 DB + 网络）
    data: dict[str, list[DataPoint]] = field(default_factory=dict)
    # 哪些走了 DB（freshness_hours 内）
    from_db: list[str] = field(default_factory=list)
    # 哪些走了网络（stale / missing）
    from_network: list[str] = field(default_factory=list)
    # 完全失败（DB 没有 + 网络也拿不到）
    missing: list[str] = field(default_factory=list)
    # 元数据未登记（fallback 视为 always-stale）
    unregistered: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def summary(self) -> str:
        return (
            f"SmartFetcher: {len(self.data)} 命中 / "
            f"{len(self.from_db)} 走DB / {len(self.from_network)} 联网 / "
            f"{len(self.missing)} 缺口 / {len(self.unregistered)} 未登记 / "
            f"{self.elapsed_ms}ms"
        )


def _has_measurement(entry: Any) -> bool:
    """该索引条目**是否被测量过**（row_count 或 last_period_date 有值）。

    为什么不直接看 `is_fresh()`：它把"从未回填"和"数据陈旧"混为一谈。
    两者处置相反 —— 前者该查库复核，后者才该联网。
    """
    rc = getattr(entry, "row_count", None)
    lpd = getattr(entry, "last_period_date", None)
    return bool(rc) or bool(lpd)


class SmartFetcher:
    """DB 优先 → 网络 fallback 的智能获取器。"""

    def __init__(
        self,
        *,
        catalog_repo: CatalogRepository | None = None,
        data_repo: DataPointRepository | None = None,
        registry: Any | None = None,  # IndicatorRegistry（避免循环导入）
    ) -> None:
        self._catalog = catalog_repo or get_catalog_repository()
        self._data_repo = data_repo
        self._registry = registry or get_registry()

    async def fetch_many(
        self,
        indicators: list[str],
        *,
        live_fetcher: LiveFetcher | None = None,
        cancel_token: CancellationToken | None = None,
        force_refresh: bool = False,
        limit_per_indicator: int | None = None,
        persist: bool = True,
        update_catalog: bool = True,
    ) -> SmartFetchResult:
        """批量取数：fresh → DB-only；stale/missing → 联网（并行）。

        Args:
            indicators: 待取数的指标 id 列表
            live_fetcher: 网络获取函数（如 `A01_collector.execute`）；None 时只走 DB
            cancel_token: 取消令牌（取消时 TaskCancelledError 抛出）
            force_refresh: True 时跳过 freshness 检查，全部走网络
            limit_per_indicator: 单指标返回条数上限
            persist: ★ 联网拿到的数据是否**立刻写库**。
                - True（默认）：独立使用 SmartFetcher 时（如批量预热脚本）直接落库
                - **False：在投研主链路（supervisor.collect_node）必须传 False** ——
                  数据要先经 A02 清洗 / A03 校验，由 A04 统一入库。
                  SmartFetcher 抢先落库会**绕过校验**，把脏数据写进事实表。
            update_catalog: 是否更新 indicator_catalog 索引（persist=False 时
                通常也应 False，把索引更新交给 A04 之后统一做）
        """
        began = time.perf_counter()
        result = SmartFetchResult()

        if not indicators:
            result.elapsed_ms = int((time.perf_counter() - began) * 1000)
            return result

        # 1) 元数据查表（一次性）
        #
        # ★ 2026-09-28 第十三轮：**聚合展开**
        # `mkt:cybkcb:turnover:all` 这类"聚合请求"本身不入库（连接器返回的是
        # 它的 3 条子指标）。不展开的话索引 row_count 恒 0 → 每次都联网。
        # 展开后：新鲜度按子指标判、数据从子指标取、索引按子指标回填。
        expanded: dict[str, tuple[str, ...]] = {}
        query_ids: list[str] = []
        for ind in indicators:
            meta = self._registry.get(ind)
            if meta is not None and meta.expands_to:
                expanded[ind] = tuple(meta.expands_to)
                query_ids.extend(meta.expands_to)
            else:
                query_ids.append(ind)
        query_ids = sorted(set(query_ids))

        catalog_entries = await self._catalog.bulk_get(query_ids)
        # 未登记的指标：注册元数据（默认 enabled=True、daily、freshness=24h）
        # 让"意外的新指标"也能享受智能路由（虽然效果有限，但避免 panic）
        for ind in query_ids:
            if ind not in catalog_entries:
                meta = self._registry.get(ind)
                if meta is None:
                    meta = IndicatorMeta(
                        indicator=ind, category="other", frequency="daily",
                        freshness_hours=24, primary_source="", source_url="",
                        enabled=True, ttl_days=365,
                    )
                    result.unregistered.append(ind)  # 真正完全没登记的
                await self._catalog.upsert_meta(meta)
                catalog_entries[ind] = await self._catalog.get(ind)

        # 2) 分类：fresh / stale / unregistered
        now_ms = int(time.time() * 1000)
        fresh_ids: list[str] = []
        stale_ids: list[str] = []
        for ind in query_ids:
            entry = catalog_entries.get(ind)
            if entry is None:
                # ★ 2026-09-28 用户裁定：**元数据缺失 ≠ 数据不存在**。
                #
                # 原实现无条件 `stale_ids.append(ind)`（always-stale），
                # 于是 `fact_data_points` 里明明有 120 条 CPI，只要索引没登记
                # 就被当成"没有数据"→ 每次联网。用户原话：
                #   「PPI/CPI … 这个数据在本地表里有啊 … 为什么说没有，
                #     还要去联网获取？」
                #
                # 现在先**查库确认**，并按**期间日期**现场推断新鲜度
                # （用户明确选择：按期间日期，不按抓取时间 ——
                #  期间日期才是数据的新鲜度，抓取时间只说明"我们问过"）。
                ok, why = await self._infer_unregistered(ind)
                result.unregistered.append(ind)   # 仍登记为缺失，便于后续补登记
                if ok:
                    fresh_ids.append(ind)
                    logger.info("未登记但有本地数据，按期间日期判为新鲜(%s)：%s",
                                ind, why)
                else:
                    stale_ids.append(ind)
                    logger.debug("未登记且判为不新鲜(%s)：%s", ind, why)
                continue
            state = entry.to_freshness()
            if force_refresh or not state.is_fresh(now_ms=now_ms):
                # ★★ 2026-09-28 用户报障的真正修复点。
                #
                # 诊断更正：`entry is None` 其实**几乎不可达** —— 上面
                # 167-181 行会给未登记指标**自动补登记**
                # （`frequency="daily", freshness_hours=24`）。
                # 所以"索引里没登记 → always-stale"不是真机制。
                #
                # 真机制：自动补登记的条目 **`row_count`/`last_period_date`
                # 从未回填**（回填靠 store 节点调 `refresh_stats_from_facts`，
                # 而它在索引表缺失时写不进去）→ `is_fresh()` 必然为假
                # → 每次都走网络。用户原话：
                #   「…fact_data_points表里有indicator字段，有cpi数据啊，
                #     为什么说没有，还要去联网获取？」
                #
                # 判据：条目**从未被测量过**（无 row_count 且无 last_period_date）
                # 时，"stale"可能只是"索引没回填"，不等于"数据陈旧"
                # → 查库按**期间日期**复核（用户裁定：不按抓取时间）。
                if not force_refresh and not _has_measurement(entry):
                    ok, why = await self._infer_unregistered(ind)
                    if ok:
                        fresh_ids.append(ind)
                        logger.info(
                            "索引未回填但有本地数据，按期间日期判为新鲜(%s)：%s",
                            ind, why)
                        continue
                    logger.debug("索引未回填且查库判为不新鲜(%s)：%s", ind, why)
                stale_ids.append(ind)
            else:
                fresh_ids.append(ind)

        # 3) fresh 走 DB（批量查询）
        if fresh_ids and self._data_repo is not None:
            if cancel_token is not None:
                cancel_token.check()
            try:
                db_data = await self._data_repo.query_points_batch(
                    fresh_ids, limit_per_indicator=limit_per_indicator,
                )
                for ind in fresh_ids:
                    rows = db_data.get(ind, [])
                    if rows:
                        result.data[ind] = rows
                        result.from_db.append(ind)
                    else:
                        # DB 标 fresh 但实际没数据（数据被 prune / 一致性问题）→ 走网络
                        stale_ids.append(ind)
                        fresh_ids.remove(ind)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SmartFetcher DB 批量查询失败: %s", exc)
                # 全转网络
                stale_ids.extend(fresh_ids)
                fresh_ids.clear()

        # 4) stale 走网络（如果提供了 live_fetcher）
        if stale_ids and live_fetcher is not None:
            if cancel_token is not None:
                cancel_token.check()
            await self._fetch_network_parallel(
                stale_ids, live_fetcher, result, cancel_token,
                limit_per_indicator=limit_per_indicator,
                persist=persist, update_catalog=update_catalog,
            )

        # 5) 剩下的 missing 标记（DB 没数据 + 网络也失败）
        #    ★ 聚合指标要回映：`turnover:all` 的数据以子指标形式存在，
        #    不能因为"all 这个名字不在 data 里"就判它缺口。
        for ind in indicators:
            subs = expanded.get(ind)
            if subs:
                if any(s in result.data for s in subs):
                    # 聚合请求成功：把子指标的数据挂到聚合名下（便于调用方按名取）
                    merged: list[Any] = []
                    for s in subs:
                        merged.extend(result.data.get(s, []))
                    if merged:
                        result.data.setdefault(ind, merged)
                    continue
                # 一个子指标都没拿到 → 才算缺口
                result.missing.append(ind)
                continue
            if ind not in result.data:
                result.missing.append(ind)

        result.elapsed_ms = int((time.perf_counter() - began) * 1000)
        logger.info(result.summary())
        return result

    async def _fetch_network_parallel(
        self,
        indicators: list[str],
        live_fetcher: LiveFetcher,
        result: SmartFetchResult,
        cancel_token: CancellationToken | None,
        *,
        limit_per_indicator: int | None,
        persist: bool,
        update_catalog: bool,
    ) -> None:
        """并行网络获取（每指标一次；通过 `asyncio.gather` 并发）。

        ⚠️ `persist=False` 时**只取不写** —— 主链路必须这样用，让数据经
        A02/A03 校验后由 A04 统一入库（否则脏数据绕过校验进事实表）。
        """
        # 锁（写 catalog 索引时），避免并发更新 race
        catalog_lock = asyncio.Lock()

        async def _one(ind: str) -> None:
            if cancel_token is not None:
                cancel_token.check()
            try:
                points = await live_fetcher(ind, None, None)
            except Exception as exc:  # noqa: BLE001 单条失败不阻断
                logger.debug("live fetch 失败(%s): %s", ind, exc)
                return
            if not points:
                return
            if persist and self._data_repo is not None:
                try:
                    await self._data_repo.save_points(
                        points, task_id="smart_fetcher")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SmartFetcher 入库失败(%s): %s", ind, exc)
            if update_catalog:
                async with catalog_lock:
                    try:
                        await self._catalog.update_from_points(points, ind)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("catalog 更新失败(%s): %s", ind, exc)
            # ★ 采集深度权威表（2026-09-28）：按指标类别截断为**最新 N 条**。
            #
            # 原来这里是 `points[:limit_per_indicator]` —— **前 N 条**。
            # 而连接器有的按期升序、有的降序返回，同一个 `[:N]` 在不同连接器上
            # 含义相反（升序时取到的是**最旧的 N 条**）。
            # 现在方向由 `fetch_depth.trim()` 定死：内部按 period_date 排序后取尾部，
            # 不依赖调用方顺序。默认 60 条，宏观 12/10、指数快照 20、
            # 日线 15、结构化行情财务 100（见该模块的有序规则表）。
            from src.infrastructure.catalog.fetch_depth import trim as _trim_depth

            result.data[ind] = _trim_depth(points, ind)
            result.from_network.append(ind)

        await asyncio.gather(*[_one(i) for i in indicators])

    # ============ 辅助：未登记指标的"元数据缺失≠数据不存在"兜底 ============

    #: 为推断频率而读的样本数（`frequency_infer` 需要 ≥3 个 period_date）
    _UNREGISTERED_LOOKUP_LIMIT: int = 12

    #: 新鲜度容差（几个周期）。月度指标 freshness_hours=720h(30天)，
    #: 容差 2 个周期 = 60 天 → 上月 1 号的值得以判为新鲜。
    #: 为什么要容差：数据源发布有延迟（CPI 次月 9-15 日发布），
    #: 卡死 1 个周期会把"刚刚好的上月值"误判成过期 → 又变回每次联网。
    _UNREGISTERED_TOLERANCE_PERIODS: float = 2.0

    async def _infer_unregistered(self, indicator: str) -> tuple[bool, str]:
        """未登记指标：查库确认有没有数据，并按**期间日期**推断新鲜度。

        Returns: `(是否新鲜, 人话原因)`。原因会进日志，要能读懂。

        判据（用户裁定「按期间日期」）：
          1. 库里**一条都没有** → 不新鲜（真缺口，该联网）
          2. 有数据 → 从 `period_date` 间隔推断频率与新鲜度阈值，
             再比 `今天 - 最新期间日期` 是否在 `阈值 × 容差` 内
        """
        if self._data_repo is None:
            return False, "没有数据仓库，无法判"
        try:
            rows_map = await self._data_repo.query_points_batch(
                [indicator], limit_per_indicator=self._UNREGISTERED_LOOKUP_LIMIT)
        except Exception as exc:  # noqa: BLE001 探测失败不该阻断主链路
            return False, f"查库失败({type(exc).__name__})，保守判为不新鲜"
        rows = rows_map.get(indicator) or []
        if not rows:
            return False, "库里确实没有数据（真缺口）"

        as_dicts = [r if isinstance(r, dict) else r.model_dump() for r in rows]
        try:
            freq, freshness_hours, samples = infer_from_rows(as_dicts)
        except Exception as exc:  # noqa: BLE001
            return False, f"频率推断失败({type(exc).__name__})"

        dates = [parse_period_date(str(r.get("period_date") or ""))
                 for r in as_dicts]
        dates = [d for d in dates if d is not None]
        if not dates:
            return False, f"有 {len(rows)} 条但都没有合法 period_date"
        last = max(dates)
        age_days = (datetime.now() - last).days
        limit_days = (freshness_hours / 24.0) * self._UNREGISTERED_TOLERANCE_PERIODS
        fresh = age_days <= limit_days
        why = (f"频率={freq}({samples}样本) 最新期间={last.date()} "
               f"距今{age_days}天 阈值{limit_days:.0f}天")
        return fresh, why

    # ============ 辅助：把 DB 已有数据也写进结果（不联网）============

    async def fetch_db_only(
        self,
        indicators: list[str],
        *,
        cancel_token: CancellationToken | None = None,
        limit_per_indicator: int | None = None,
    ) -> SmartFetchResult:
        """只走 DB（不联网）；missing 标记空数据。"""
        return await self.fetch_many(
            indicators,
            live_fetcher=None,
            cancel_token=cancel_token,
            limit_per_indicator=limit_per_indicator,
        )