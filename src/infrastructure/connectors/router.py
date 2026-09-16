"""多数据源连接器路由：按指标把fetch分发到命中的连接器，失败顺序故障转移。

数据获取分层策略（三级短路，从快到慢）：
  1. 进程内 TTL 缓存（内存 dict，毫秒级）——同进程并发/重复请求秒回
  2. 本地持久化 DB（SQLite / PostgreSQL，通过 DataPointRepository）——进程重启后仍可命中
     - 仅慢变量（CPI/PPI/M2/社融/PE/PB/估值分位等日/月频）查 DB
     - DB 数据仍做「过期判定」：若最新 period_date 距 today 已超过窗口上限，
       则视为过期，穿透到网络刷新（避免用 DB 里的旧月度宏观数据）
  3. 真实连接器链（QMT → CSV → AkShare）——DB 无可信新鲜数据时才发网络请求
     - connector 成功后同时回填 repo（供下次进程重启后命中）和 TTL 缓存

故障转移规则（QMT→本地CSV→AkShare…）：
- 某连接器抛 DataFetchError 时自动尝试下一个命中的连接器，错误链汇总抛出；
- 非 DataFetchError 的异常视为程序缺陷，立即上抛不掩盖；
- 已有真实源尝试失败后，不再回退 simulated 连接器，防止模拟数据冒充真实。

设计原则：
- 快变量（实时成交额/换手率/盘中行情/两融北向日内）**跳过 DB 查询**——网络拿的就是最新值
- start_date/end_date 非空时跳过 DB 命中（回测按区间拉取不应命中本地整段缓存，
  但 connector 成功仍可回填 repo 供下次）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint
from src.infrastructure.connectors.base import BaseConnector

if TYPE_CHECKING:
    from src.infrastructure.repositories.base import DataPointRepository

logger = logging.getLogger(__name__)

# 指标前缀→TTL 秒（不匹配任何前缀的指标不缓存）：
_TTL_BY_PREFIX: list[tuple[tuple[str, ...], int]] = [
    # 月频宏观：24h
    (("CPI", "PPI", "M2", "社融"), 24 * 3600),
    # 估值/行业PE：日频盘后拉取，12h
    (("PE(TTM):", "PB:", "资产负债率:", "流动比率:",
      "idx_val:", "sw_ind:", "ind:"), 12 * 3600),
    # 大盘流动性（两融/北向）：T+1，4h
    (("mkt:margin_balance", "mkt:north_flow"), 4 * 3600),
    # 实时流动性（成交额/换手率）：5min
    (("mkt:turnover", "mkt:turnover:hist",
      "mkt:all_a_turnover", "mkt:chg_board", "mkt:kcb_board",
      "mkt:market_breadth"), 5 * 60),
    # FedWatch：1h（FOMC 前后更新）
    (("fed:",), 3600),
    # 行情日线：4h（收盘后稳定）
    (("stock_close:", "index_close:", "etf_close:"), 4 * 3600),
]
# 明确不缓存的前缀（盘中实时行情）
_NO_CACHE_PREFIXES: tuple[str, ...] = ()

# ========== DB 查询策略 ==========

# 哪些指标查询**跳过**本地持久化 DB（实时/盘中/快照型，网络拿的就是最新值）
_DB_SKIP_PREFIXES: tuple[str, ...] = (
    # 实时成交额/换手率：盘中每分钟变
    "mkt:turnover", "mkt:all_a_turnover", "mkt:chg_board", "mkt:kcb_board",
    "mkt:market_breadth",
    # FedWatch CME 实时赔率：FOMC 前高频刷新
    "fed:",
)

# 哪些指标**应该**查 DB（慢变量：月频/日频估值/历史序列）
# 这些指标查 DB + 过期判定，避免重复拉网络
_DB_QUERY_PREFIXES: tuple[str, ...] = (
    "CPI", "PPI", "M2", "社融", "US_CPI", "US_CORE", "US_NONFARM",
    "PE(TTM):", "PB:", "资产负债率:", "流动比率:",
    "idx_val:", "sw_ind:", "ind:",
    "mkt:margin_balance", "mkt:north_flow",  # T+1 日频，查 DB 有意义
    "mkt:turnover:hist", "mkt:turnover_rate:hist",  # 历史序列，DB 有就不重拉
    "stock_close:", "index_close:", "etf_close:",  # 日线历史，DB 有就不重拉
)

# DB 过期判定现在统一用 DataFreshnessEvaluator.db_expired_months(indicator)
# ——与 _build_context 的指数衰减口径完全一致


def _should_query_db(indicator: str) -> bool:
    """判断某指标是否应该查本地 DB（跳过快变量）。"""
    ind = indicator.lower()
    for prefix in _DB_SKIP_PREFIXES:
        if ind.startswith(prefix.lower()):
            return False
    for prefix in _DB_QUERY_PREFIXES:
        if ind.startswith(prefix.lower()):
            return True
    # 默认不查（保守：未知指标不浪费一次 sqlite 查询）
    return False


def _is_db_fresh(points: list[DataPoint], indicator: str,
                 today: date | None = None) -> bool:
    """判断 DB 查询结果是否可用（用 DataFreshnessEvaluator 统一口径）。

    规则：最新数据点的 confidence >= 0.4（非 stale/expired）才算可用。
    与 _build_context 的五级分级完全一致。
    """
    if not points:
        return False
    from src.core.data_freshness import DataFreshnessEvaluator

    evaluator = DataFreshnessEvaluator()
    newest_period = max(
        (p.period_date for p in points if p.period_date),
        default=None,
    )
    if newest_period is None:
        return False
    fe = evaluator.evaluate(indicator, newest_period, today=today)
    return fe.confidence >= 0.4  # lagging 以上（含）都可用


class ConnectorRouter(BaseConnector):
    """有序路由：supports命中者按顺序尝试，DataFetchError触发故障转移。

    数据获取三级短路（从快到慢）：
      [1] 进程内 TTL 缓存       → 毫秒级命中（同进程重复请求）
      [2] 本地持久化 DB（repo）  → 查 SQLite/PostgreSQL，进程重启后可命中
                                   仅慢变量查 DB + 过期判定
      [3] 真实 connector 链      → QMT→CSV→AkShare，DB miss/过期才网络请求
                                   connector 成功后回填 repo 和 TTL

    可选参数：
      repo: DataPointRepository | None —— 注入后启用「本地 DB 优先」策略
      disable_cache=True 关闭进程内 TTL 缓存（回测 API 会显式创建禁用版）
      disable_db=True     关闭 repo 查询（测试/性能对比用）
    """

    source_name = "router"
    source_url = ""

    def __init__(
        self,
        routes: list[tuple[BaseConnector, Callable[[str], bool]]],
        *,
        repo: DataPointRepository | None = None,
        disable_cache: bool = False,
        disable_db: bool = False,
        failure_cooldown: int = 300,
    ) -> None:
        self._routes = routes
        self._disable_cache = disable_cache
        self._disable_db = disable_db or repo is None
        self._repo = repo
        self._cache: dict[str, tuple[float, list[DataPoint]]] = {}
        self._lock = asyncio.Lock()
        self._per_key_locks: dict[str, asyncio.Lock] = {}
        # 失败冷却：(connector_source_name, indicator) → (expiry_monotonic, error)
        # 冷却期内跳过该连接器，避免重复调用已知失败的外部接口
        self._failure_cache: dict[str, tuple[float, str]] = {}
        self._failure_cooldown = failure_cooldown
        logger.info(
            "ConnectorRouter 初始化: %d routes, "
            "TTL缓存=%s, 本地DB=%s (repo=%s), "
            "失败冷却=%ds",
            len(routes), not disable_cache,
            not self._disable_db,
            type(repo).__name__ if repo else "None",
            failure_cooldown if not disable_cache else 0,
        )

    def _matched(self, indicator: str) -> list[BaseConnector]:
        return [connector for connector, supports in self._routes
                if supports(indicator)]

    def _resolve(self, indicator: str) -> BaseConnector:
        """首个命中连接器（测试/诊断用；取数请走fetch以获得故障转移）。"""
        matched = self._matched(indicator)
        if matched:
            return matched[0]
        known = [
            ind
            for connector, _ in self._routes
            for ind in connector.get_capabilities().get("indicators", [])
        ]
        raise DataFetchError(
            f"无连接器支持指标 {indicator}；已注册: {', '.join(map(str, known))}"
        )

    def _cache_ttl(self, indicator: str) -> int | None:
        """该指标应该缓存多少秒；返回 None 表示不缓存。"""
        for prefixes, ttl in _TTL_BY_PREFIX:
            if indicator.startswith(prefixes):
                return ttl
        return None

    async def _cached_fetch(
        self,
        indicator: str,
        start_date: str | None,
        end_date: str | None,
    ) -> list[DataPoint]:
        """三级短路：TTL 缓存 → 本地 DB → connector 链。

        start/end 指定时跳过 TTL（回测区间拉取），但仍可查 DB；
        connector 成功后同时回填 repo + TTL。
        """
        ttl = self._cache_ttl(indicator)
        now = time.monotonic()

        # ====== [1] 进程内 TTL 缓存 ======
        skip_ttl = self._disable_cache or ttl is None or bool(start_date or end_date)
        if not skip_ttl:
            cached = self._cache.get(indicator)
            if cached and cached[0] > now:
                logger.debug("路由TTL命中 %s (剩%.0fs, %dpts)",
                             indicator, cached[0] - now, len(cached[1]))
                return list(cached[1])

        # ====== [2] 本地持久化 DB ======
        skip_db = self._disable_db or not _should_query_db(indicator)
        # start/end 指定时跳过 DB 命中（区间查询可能不匹配，但 connector 成功会回填）
        skip_db = skip_db or bool(start_date or end_date)
        if not skip_db:
            try:
                db_points = await self._repo.query_points(
                    indicator, start_date, end_date)
                if db_points and _is_db_fresh(db_points, indicator):
                    logger.info(
                        "路由本地DB命中 %s (%dpts, 最新=%s, 跳过网络请求)",
                        indicator, len(db_points),
                        max(p.period_date or "?" for p in db_points),
                    )
                    # DB 命中也回填 TTL（减少同进程后续查询的 sqlite 开销）
                    if not self._disable_cache and ttl is not None and db_points:
                        self._cache[indicator] = (now + ttl, list(db_points))
                    return db_points
                if db_points:
                    logger.info(
                        "路由DB有但过期 %s (最新=%s), 穿透到网络刷新",
                        indicator,
                        max(p.period_date or "?" for p in db_points),
                    )
            except Exception as exc:  # noqa: BLE001 — DB 故障 fail-open
                logger.warning("路由DB查询异常，穿透网络: %s -> %s",
                               indicator, exc)

        # ====== [3] 真实 connector 链 ======
        # per-key Lock 防击穿：同一指标并发请求只有一个发网络
        lock = self._per_key_locks.setdefault(indicator, asyncio.Lock())
        async with lock:
            # 二次检查（持锁期间其他协程可能已写入 TTL）
            if not skip_ttl:
                cached = self._cache.get(indicator)
                now = time.monotonic()
                if cached and cached[0] > now:
                    return list(cached[1])
            # 真正发网络请求
            points = await self._fetch_uncached(indicator, start_date, end_date)
            # 回填两级缓存
            now = time.monotonic()
            if not self._disable_cache and ttl is not None and points:
                self._cache[indicator] = (now + ttl, list(points))
            # 回填 DB（进程重启后可命中；fail-open：DB 存不进不影响本次返回）
            if not self._disable_db and points:
                try:
                    await self._repo.save_points(points, task_id="connector_router")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("路由回填DB失败（不影响本次结果）: %s -> %s",
                                   indicator, exc)
            return points

    def invalidate(self, indicator: str | None = None) -> None:
        """清空指定指标或全部 TTL 缓存（回测API用）。"""
        if indicator is None:
            self._cache.clear()
        else:
            self._cache.pop(indicator, None)

    def clear_failure_cache(self, indicator: str | None = None) -> None:
        """清空失败冷却记录。

        indicator 为 None 清全部；指定 indicator 只清该指标的。
        用于：手动重试已知失败源、或自修复后重置冷却。
        """
        if indicator:
            keys_to_remove = [
                k for k in self._failure_cache
                if k.endswith(f":{indicator}")
            ]
            for k in keys_to_remove:
                self._failure_cache.pop(k, None)
        else:
            self._failure_cache.clear()
            logger.info("失败冷却缓存已全部清空")

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        return await self._cached_fetch(indicator, start_date, end_date)

    async def _fetch_uncached(
        self,
        indicator: str,
        start_date: str | None,
        end_date: str | None,
    ) -> list[DataPoint]:
        matched = self._matched(indicator)
        if not matched:
            known = [
                ind
                for connector, _ in self._routes
                for ind in connector.get_capabilities().get("indicators", [])
            ]
            raise DataFetchError(
                f"无连接器支持指标 {indicator}；已注册: {', '.join(map(str, known))}"
            )

        now_mono = time.monotonic()
        errors: list[str] = []
        real_attempted = False
        all_in_cooldown = True  # 是否所有匹配连接器都在冷却期
        for connector in matched:
            simulated = bool(connector.get_capabilities().get("simulated"))
            if simulated and real_attempted:
                # 真实源已失败：宁报数据缺口，也不用模拟数据冒充
                logger.warning("真实源失败后跳过模拟连接器: %s (%s)",
                               connector.source_name, indicator)
                continue

            # 失败冷却检查：如果该连接器对该指标最近失败过，跳过
            fkey = f"{connector.source_name}:{indicator}"
            fc = self._failure_cache.get(fkey)
            if fc and fc[0] > now_mono:
                logger.debug(
                    "连接器 %s 对 %s 在失败冷却中（剩%.0fs），跳过",
                    connector.source_name, indicator, fc[0] - now_mono)
                errors.append(
                    f"[{connector.source_name}] 冷却中"
                    f"（{fc[1][:60] if fc[1] else '未知错误'}）")
                continue
            elif fc:
                # 冷却过期，清除
                self._failure_cache.pop(fkey, None)

            all_in_cooldown = False
            try:
                points = await connector.fetch(
                    indicator, start_date, end_date)
            except DataFetchError as exc:
                errors.append(f"[{connector.source_name}] {exc}")
                # 记录失败冷却
                self._failure_cache[fkey] = (
                    now_mono + self._failure_cooldown, str(exc))
                logger.warning(
                    "数据源失败，记录冷却(%ds)，尝试下一源: %s -> %s",
                    self._failure_cooldown, indicator, exc)
                if not simulated:
                    real_attempted = True
                continue
            # 成功：清除该连接器对该指标的失败冷却记录（如果有）
            self._failure_cache.pop(fkey, None)
            if errors:
                logger.info("指标 %s 经故障转移后由 %s 取到 %d 点",
                            indicator, connector.source_name, len(points))
            return points

        # 所有源都失败或在冷却中
        if all_in_cooldown and not real_attempted:
            # 全部在冷却期 → 返回空列表而非 raise（让 _storage_fallback 接管）
            logger.info(
                "指标 %s 所有匹配源均在失败冷却中，跳过网络请求",
                indicator)
            return []

        raise DataFetchError(
            f"所有数据源获取 {indicator} 均失败: " + " | ".join(errors)
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": "ConnectorRouter",
            "routes": [
                connector.get_capabilities() for connector, _ in self._routes
            ],
        }
