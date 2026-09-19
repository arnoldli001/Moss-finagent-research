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
- start_date/end_date 非空时**不用 DB 短路**（区间拉取要的是当天的形成中bar），
  但**仍要查一次 DB 并把它的最新日期当作「新鲜度下限」**：
  链上返回的数据若早于该下限，就继续尝试下一个源，而不是"第一个成功就收工"。
  全部源都比 DB 旧、或网络链整体失败时，回退用 DB。
  这条规则解决了实测的一个真问题：QMT 未启动时，"本地行情CSV"（QMT 的导出文件，
  可能停在三周前）会把 AkShare/腾讯挡在门外，而本地 SQL 库里其实躺着更新的数据。
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


def _newest_period(points: list[DataPoint]) -> str | None:
    """这批数据里最新的 period_date（无有效值返回 None）。

    period_date 一律是 `YYYY-MM-DD` 定长格式，字符串比较与日期比较等价 ——
    不做 strptime 是为了在每次路由决策（可能在循环里被调用很多次）上省掉解析开销。
    """
    values = [p.period_date for p in points if p.period_date]
    return max(values) if values else None


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

        start/end 指定时**不用 DB 短路**（区间拉取要的是当天的形成中bar，
        本地 DB 里的整段缓存会把这个需求遮掉），但**仍要查一次 DB** ——
        它在这里的角色从"缓存"变成「**新鲜度下限**」：

          实测踩过的坑（QMT 未启动时）：带区间查询只走网络链，而链上第一个成功的是
          「本地行情CSV」——那是 QMT 的导出文件，可能停在两周前（拿到 2026-08-31）。
          可用的 AkShare / 腾讯明明能取到 2026-09-16，却因为"第一个源成功了"根本
          没被尝试；同时本地 SQL 库里躺着 2026-09-16 的完整数据，却被 `skip_db` 跳过。
          结果日K面板一直显示两周前的行情，只给一句"日线最新为 8-31"。

        现在的规则：
          1. 先查 DB 拿到 `db_newest`，不短路，只当作"不该比这更旧"的下限；
          2. 链上某个源返回的数据**早于**这个下限 → 视为该源不可用（记日志、不下冷却），
             继续尝试下一个源 → AkShare/腾讯就有机会了；
          3. 链上全部源都比 DB 旧、或网络链整体失败 → **回退用 DB**（比用更旧的数据强）。
        """
        ttl = self._cache_ttl(indicator)
        now = time.monotonic()
        ranged = bool(start_date or end_date)

        # ====== [1] 进程内 TTL 缓存 ======
        skip_ttl = self._disable_cache or ttl is None or ranged
        if not skip_ttl:
            cached = self._cache.get(indicator)
            if cached and cached[0] > now:
                logger.debug("路由TTL命中 %s (剩%.0fs, %dpts)",
                             indicator, cached[0] - now, len(cached[1]))
                return list(cached[1])

        # ====== [2] 本地持久化 DB ======
        db_points: list[DataPoint] = []
        db_newest: str | None = None
        skip_db = self._disable_db or not _should_query_db(indicator)
        if not skip_db:
            try:
                db_points = await self._repo.query_points(
                    indicator, start_date, end_date)
                db_newest = _newest_period(db_points)
            except Exception as exc:  # noqa: BLE001 — DB 故障 fail-open
                logger.warning("路由DB查询异常，穿透网络: %s -> %s",
                               indicator, exc)
                db_points, db_newest = [], None
        if db_points and not ranged:
            if _is_db_fresh(db_points, indicator):
                logger.info(
                    "路由本地DB命中 %s (%dpts, 最新=%s, 跳过网络请求)",
                    indicator, len(db_points), db_newest or "?")
                # DB 命中也回填 TTL（减少同进程后续查询的 sqlite 开销）
                if not self._disable_cache and ttl is not None:
                    self._cache[indicator] = (now + ttl, list(db_points))
                return db_points
            logger.info("路由DB有但过期 %s (最新=%s), 穿透到网络刷新",
                        indicator, db_newest or "?")
        elif db_points and ranged:
            logger.info(
                "路由区间查询 %s：本地DB 最新=%s（%dpts）→ 作为新鲜度下限，"
                "仍走网络取当日形成中bar",
                indicator, db_newest or "?", len(db_points))

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
            # 真正发网络请求（把 DB 的最新日期作为"不该比这更旧"的下限传下去）
            try:
                points = await self._fetch_uncached(
                    indicator, start_date, end_date,
                    min_expected_date=db_newest if ranged else None)
            except DataFetchError:
                if ranged and db_points:
                    logger.warning(
                        "网络链全部失败，回退本地DB %s（%dpts, 最新=%s）",
                        indicator, len(db_points), db_newest or "?")
                    return db_points
                raise
            # 网络链给回来的东西比本地还旧：用本地（数据更完整、更新）
            if ranged and db_points and db_newest:
                net_newest = _newest_period(points)
                if net_newest is None or net_newest < db_newest:
                    logger.warning(
                        "网络链最新(%s) 不新于本地DB(%s) → 改用本地DB %s（%dpts）",
                        net_newest or "无", db_newest, indicator, len(db_points))
                    return db_points
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
        *,
        min_expected_date: str | None = None,
    ) -> list[DataPoint]:
        """按顺序尝试命中的连接器。

        `min_expected_date`：**数据新鲜度下限**（通常是本地 DB 的最新日期）。
        某个源成功返回、但最新日期早于这个下限时，不立刻采用，而是继续尝试下一源 ——
        这样"QMT 未启动 → 本地 CSV 停在三周前"就不会把后面的 AkShare/腾讯挡掉。
        全部源都比下限旧时，返回其中**最新的那一份**（有总比没有强，上层还会拿它
        和 DB 比一次）。
        """
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
        # 成功但比下限旧的候选：(最新日期, 数据, 源名)，等所有源试完再挑最新的一份
        stale_hold: list[tuple[str, list[DataPoint], str]] = []
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
            if min_expected_date is not None:
                newest = _newest_period(points)
                if newest is not None and newest < min_expected_date:
                    # 关键：**不**记失败冷却 —— 这个源本身是好的，
                    # 只是这份数据旧了；记冷却会让它连"最新可得的源"都当不成。
                    logger.info(
                        "源 %s 返回的数据(最新=%s)早于本地DB(最新=%s)，"
                        "继续尝试下一源 %s",
                        connector.source_name, newest, min_expected_date, indicator)
                    errors.append(
                        f"[{connector.source_name}] 数据过旧"
                        f"（最新 {newest} < 本地 {min_expected_date}）")
                    stale_hold.append((newest, points, connector.source_name))
                    if not simulated:
                        real_attempted = True
                    continue
                if newest is None and not points:
                    # 空结果也算"没拿到"：某个源返回空（实测 AkShare 东财被阻断、
                    # 新浪回退断连时会返回 []），不该让它把后面的源挡掉。
                    # 只在设了新鲜度下限（区间日线查询）时这样处理，
                    # 不改变其他指标的既有语义。
                    logger.info("源 %s 对 %s 返回空，继续尝试下一源",
                                connector.source_name, indicator)
                    errors.append(f"[{connector.source_name}] 返回空")
                    if not simulated:
                        real_attempted = True
                    continue
            if errors:
                logger.info("指标 %s 经故障转移后由 %s 取到 %d 点",
                            indicator, connector.source_name, len(points))
            return points

        # 所有源都比下限旧：返回其中最新的一份（上层还会与本地 DB 比较）
        if stale_hold:
            stale_hold.sort(key=lambda item: item[0], reverse=True)
            newest, points, name = stale_hold[0]
            logger.warning(
                "指标 %s 所有源都早于本地DB(最新=%s)；采用其中最新的 %s(最新=%s)",
                indicator, min_expected_date, name, newest)
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
