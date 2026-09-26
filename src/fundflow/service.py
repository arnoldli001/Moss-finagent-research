"""资金流监控服务：把取数结果组装成前端要的「榜单 + 走势」。

## 两张榜怎么排（用户口径）

**板块榜**：按「最近 N 日净额均值」排序，取净流入前 20 与净流出前 20 两段
（用户要"净流入或净流出板块"，所以两段都要给）。
默认勾选的 20 个热门板块 = 当日盘中净额绝对值最大的 20 个（来自同花顺即时，
这样"热门"跟着当天的钱走，而不是写死一份名单）。

**个股榜**：按 `mean(近 N 日主力净额) / 流通市值` 排序。
用比值而非绝对额，是为了让大小盘可比（否则榜单永远被大市值占据）。

## 为什么"选择"要落库

用户手动加的板块/个股是他的判断，下次打开必须还在；系统给的默认热门
只在**该 kind 一条记录都没有**时播种（`seed`），用户删掉某只之后不会再被自动塞回来
—— "我删了它又回来了"比"没给默认值"更让人恼火。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.core.trading_session import SESSION_LABELS
from src.core.trading_session import session_state as _session_state
from src.fundflow import sector_filter
from src.fundflow.models import FlowBoard, FlowEntity, FlowPoint
from src.fundflow.provider import FundFlowProvider, _finite
from src.infrastructure.repositories.fund_flow_sqlite_repo import (
    FlowWatchEntry,
    FlowWatchRepository,
)

logger = logging.getLogger(__name__)

DEFAULT_TOP = 20
# 榜单里附带走势的实体上限（图表要能看清，20 条线已经很多了）
CHART_LIMIT = 12
# 盘中快照缓存：与做T自选池同一节奏（服务端 60 秒重算一次，前端取缓存）
SNAPSHOT_TTL = 60.0
#: ⚠️ 原「昨日涨停股保送入榜」的流通市值门槛（30 亿）。该口径已于 2026-09-22
#: 取消（见 `_rank_stocks` 的说明），这里刻意**不保留**这个常量：留着它会让
#: 后来者以为"还有个按市值筛涨停股的口径"，而实际上榜单已经是纯排序。
# 涨停池 / 盘中快照的缓存（秒）：两者都盘中变化，60 秒与快照节奏一致
QUOTE_TTL = 60.0

_SESSION_LABELS = SESSION_LABELS


def _normalize(name: str) -> str:
    """板块名归一化（去空白/标点/大小写），用于跨数据源比对。"""
    return "".join(char for char in str(name).lower()
                   if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def session_state(now: datetime | None = None) -> tuple[str, str]:
    """当前交易时段（与做T模块同口径，便于前端措辞一致）。

    口径本身来自 `core.trading_session`，此处只做转发 —— 靠注释维持口径一致
    是不可靠的：两个模块各存一份实现时，改一处漏一处就会让两个页面对同一时刻
    显示不同状态。
    """
    return _session_state(now)


@dataclass
class FundFlowService:
    """资金流监控（进程内单例）。"""

    provider: FundFlowProvider
    repo: FlowWatchRepository | None = None

    def __post_init__(self) -> None:
        self._cache: tuple[float, FlowBoard] | None = None
        self._lock = asyncio.Lock()
        self._seeded = False
        # 涨停池与盘中快照缓存（各自 60 秒）
        self._pool_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._quote_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        # 股票名录单例（不装配到 __init__ 参数里：它是可选增强，取不到不影响资金流）
        self._directory_client: Any = None
        self._directory_failed = False

    # ==================== 选择列表 ====================

    async def watchlist(self, kind: str) -> list[FlowWatchEntry]:
        if self.repo is None:
            return []
        return await self.repo.list(kind)

    async def add(self, kind: str, code: str, name: str = "") -> list[FlowWatchEntry]:
        if self.repo is None:
            raise RuntimeError("fundflow watch repository is not configured")
        entry = FlowWatchEntry(kind=kind, code=code, name=name, source="manual")
        return await self.repo.add(entry)

    async def remove(self, kind: str, code: str) -> tuple[bool, list[FlowWatchEntry]]:
        if self.repo is None:
            raise RuntimeError("fundflow watch repository is not configured")
        return await self.repo.remove(kind, code)

    async def ensure_defaults(self, *, names: list[str] | None = None,
                              top: int = DEFAULT_TOP) -> None:
        """首次使用时播种默认热门板块（**只在该 kind 为空时**）。

        `names` 由调用方按"当日盘中净额绝对值"排好序传进来（Tushare 东财截面口径）。
        """
        if self.repo is None or self._seeded:
            return
        existing = await self.repo.list("sector")
        if existing:
            self._seeded = True
            return
        entries = [FlowWatchEntry(kind="sector", code=name, name=name,
                                 source="default")
                   for name in (names or [])[:top]]
        if entries:
            await self.repo.seed(entries)
            logger.info("资金流监控：已播种默认热门板块 %d 个", len(entries))
        self._seeded = True

    # ==================== 快照 ====================

    async def snapshot(self, *, force: bool = False, window_days: int = 10,
                       top: int = DEFAULT_TOP) -> FlowBoard:
        """完整快照（榜单 + 走势），盘中 60 秒缓存；`force=True` 忽略缓存。"""
        if not force and self._cache is not None:
            from time import monotonic

            saved_at, board = self._cache
            if (monotonic() - saved_at) < SNAPSHOT_TTL:
                return board
        async with self._lock:
            if not force and self._cache is not None:
                from time import monotonic

                saved_at, board = self._cache
                if (monotonic() - saved_at) < SNAPSHOT_TTL:
                    return board
            board = await self._build(window_days=window_days, top=top)
            from time import monotonic

            self._cache = (monotonic(), board)
            return board

    async def _build(self, *, window_days: int, top: int) -> FlowBoard:
        state, label = session_state()
        board = FlowBoard(
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            window_days=window_days, session_state=state, session_label=label)
        board.refresh_hint = (
            f"盘中每 60 秒自动重算（当前 {label}）；也可手动刷新"
            if state == "trading" else
            f"当前 {label}：板块即时口径不再变化，日频序列为最近一个交易日收盘值")

        raw_realtime = await self.provider.sector_snapshot()
        # 剔除"没有明确行业/概念"的板块（地域/指数成分/持仓属性/风格规模/
        # 市场统计）。**必须在 `ensure_defaults` 之前**：默认热门是按当日净额
        # 绝对值取的，而融资融券/富时罗素/MSCI中国这类板块金额极大，
        # 不先滤掉就会被播种进用户的持久化选择列表 —— 之后再滤，
        # 界面上就只剩"它在监控列表里，却永远不进榜单"的尴尬状态。
        realtime, dropped = sector_filter.filter_payload(raw_realtime)
        config = sector_filter.current()
        note = sector_filter.disclosure(dropped, loaded=config.loaded,
                                       gap=config.gap)
        if note:
            board.source_notes.append(note)
        # 校验清单里的 (code, name) 配对是否与数据源一致。写错代码会**静默删掉
        # 另一个真实板块**（名称匹配不上、代码却命中，两条路看起来都正常），
        # 所以每次组装都对一遍，并把不一致摊在界面上而不是只记日志。
        problems = sector_filter.verify_codes(raw_realtime)
        if problems:
            board.source_notes.append(
                f"⚠️ 板块剔除清单有 {len(problems)} 条 code/名称不一致，"
                f"可能误删了别的板块：{'；'.join(problems[:3])}"
                + ("…" if len(problems) > 3 else ""))
        if not raw_realtime:
            board.gaps.append(
                "板块资金流截面不可用（Tushare `moneyflow_ind_dc` 失败或权限不足）——"
                "板块榜与走势都拿不到数据")
        else:
            board.source_notes.append(
                "板块净额/净占比/大单来自 Tushare `moneyflow_ind_dc`（东财板块口径，"
                f"最新交易日 {next(iter(realtime.values())).get('trade_date', '?')}，"
                "含超大单/大单与大单占比）")
            overlay = getattr(self.provider, "_realtime_note", "")
            if overlay:
                board.source_notes.append(overlay + "（盘中实时层，秒级缓存 60s）")
        # 默认热门 = 当日净额绝对值最大的 20 个板块（跟着当天的钱走，不写死名单）
        ranked_names = [name for name, _ in sorted(
            ((name, data) for name, data in realtime.items()
             if data.get("net") is not None),
            key=lambda item: -abs(float(item[1]["net"])))]
        await self.ensure_defaults(names=ranked_names, top=top)

        # ---- 板块榜：近 N 日净额均值排序 ----
        selected_sectors = [entry.code for entry in await self.watchlist("sector")]
        # 已选列表也要过滤：默认热门是**早先**播种的（那时还没有剔除清单），
        # 实测用户库里就躺着 MSCI中国 / 基金重仓 / 大盘成长 三个。
        # 这里不删库里的记录（那是用户的选择，删了不可逆），只是本次不参与，
        # 并如实说明有多少个被跳过 —— 否则"选定 12 个只画出 9 条线"会像 bug。
        allowed_sectors, skipped = sector_filter.filter_names(selected_sectors)
        if skipped:
            board.source_notes.append(
                f"已选板块里有 {skipped} 个属于剔除类别，本次不参与榜单与走势"
                "（可在左侧监控列表里手动移除）")
        history_names = list(dict.fromkeys(allowed_sectors))[:CHART_LIMIT]
        histories = await self._sector_histories(
            history_names, realtime=realtime, window_days=window_days)
        board.sector_rank = self._rank_sectors(histories, realtime, window_days)
        board.sectors = [histories[name] for name in history_names if name in histories]
        if selected_sectors and not board.sectors:
            board.gaps.append(
                "已选板块的走势都没取到：可能是板块名与数据源口径不一致，"
                "换一个（搜索加入）再试")

        # ---- 个股榜：净额均值 / 流通市值（自选单列，不占名额） ----
        stock_entries = await self.watchlist("stock")
        custom_codes = [entry.code for entry in stock_entries]
        stock_rank, stock_watch, stock_series, stock_gaps = await self._rank_stocks(
            window_days=window_days, top=top, extra_codes=custom_codes)
        board.stock_rank = stock_rank
        board.stock_watch = stock_watch
        board.gaps.extend(stock_gaps)
        if stock_series:
            board.source_notes.append(
                "个股日频资金流来自 Tushare moneyflow（本地仓库，单位元），"
                "流通市值来自 daily_basic.circ_mv")

        # 走势池 = 自选 + 榜单前 N（自选优先，用户明确要看的必须在，且总数受图上限约束）
        chart_codes = list(dict.fromkeys(
            [item.code for item in stock_watch] + [item.code for item in stock_rank]))
        chart_codes = chart_codes[:CHART_LIMIT]
        board.stocks = [stock_series[code] for code in chart_codes
                        if code in stock_series]
        board.trade_date = self._latest_date(board)
        relative = [item for item in board.stock_rank if item.net_to_mv is not None]
        if len(relative) < len(board.stock_rank):
            board.gaps.append(
                f"{len(board.stock_rank) - len(relative)} 只个股缺流通市值，"
                "未能计入「净额/流通市值」排序（已在榜单里单列，按净额均值排序）")
        return board

    # ==================== 排序实现 ====================

    @staticmethod
    def _summarize(entity: FlowEntity, window_days: int) -> FlowEntity:
        nets = [point.net for point in entity.series if point.net is not None]
        if nets:
            entity.net_avg = sum(nets) / len(nets)
            entity.latest_net = nets[-1]
            entity.latest_date = entity.series[-1].date if entity.series else ""
        return entity

    def _rank_sectors(
        self, histories: dict[str, FlowEntity],
        realtime: dict[str, dict[str, Any]], window_days: int,
    ) -> list[FlowEntity]:
        """板块榜：历史净额均值排序（取净流入前 20 + 净流出前 20）。

        两个口径都进榜，各自标注来源：
          - 有历史序列的 → 按「近 N 日净额均值」排序（用户口径）；
          - 只有当日截面的 → 按|当日净额|排序放后面，并如实写"仅当日"。
        这样即使某个板块的历史取不到，用户仍能看到"今天钱在往哪去"。
        """
        items: list[FlowEntity] = []
        seen: set[str] = set()
        for name, entity in histories.items():
            self._summarize(entity, window_days)
            if entity.today_net is None:
                self._attach_realtime(entity, realtime.get(name) or {})
            seen.add(name)
            items.append(entity)
        for name, info in realtime.items():
            if name in seen:
                continue
            entity = FlowEntity(kind="sector", code=name, name=name,
                                available=False,
                                gap="该板块的历史净额序列未取到（仅当日截面可用）")
            entity.data_source = "Tushare moneyflow_ind_dc（仅当日截面）"
            self._attach_realtime(entity, info)
            items.append(entity)
        with_history = [item for item in items if item.net_avg is not None]
        without_history = [item for item in items if item.net_avg is None]
        influx = sorted([item for item in with_history if (item.net_avg or 0) > 0],
                        key=lambda item: -(item.net_avg or 0))[:DEFAULT_TOP]
        outflow = sorted([item for item in with_history if (item.net_avg or 0) <= 0],
                         key=lambda item: (item.net_avg or 0))[:DEFAULT_TOP]
        without_history.sort(key=lambda item: -abs(item.today_net or 0.0))
        return influx + outflow + without_history[:DEFAULT_TOP]

    async def _rank_stocks(
        self, *, window_days: int, top: int, extra_codes: list[str],
    ) -> tuple[list[FlowEntity], list[FlowEntity], dict[str, FlowEntity], list[str]]:
        """个股榜 + 自选，**分两段返回**（2026-09-22 口径）。

        返回 `(榜单, 自选, 走势, 缺口)`。两段是**互斥**的：榜单里不含自选，
        自选里不含榜单票（同一只票只会出现在一段里）。

        ## 榜单构成（纯排序，两个名额池）

        1. **净流入前 `top`**（按「净额均值 ÷ 流通市值」排序）；
        2. **净流出前 `top`**（同口径，取最负的）。

        ## 为什么自选要单独一段（用户 2026-09-22 要求）

        原来自选票被塞进同一个 `stock_rank` 当作"入榜类别=自选"。后果实测：
        个股榜 12 行里 10 行是自选 —— 用户手动加了几只票，`top=10` 的净流入榜
        实际只显示得出 2 只，**自选把排行榜的名额吃光了**，榜就不成其为榜。

        现在自选单列：它不占 `top` 名额，排行榜稳定给出完整的净流入/净流出
        前 N；自选照样在，只是换个位置（前端单开一节）。
        「自选」这个 rank_group 也随之取消 —— 两段本身已经说明了身份，
        再挂一个类别徽标是重复信息。

        ⚠️ 保留的东西（别一起删）：
        * `_limit_up_pool()` 仍在调 —— 它是给**所有**入榜与自选个股补
          「涨停原因」列（`limitup_reason`）的数据源，与"是否入榜"无关。

        排序在 DataFrame 上做（4 个标量列），只把**入选的**几十只票转成 FlowEntity
        并带出逐日序列 —— 给 5000 只票各建一个对象纯属浪费。
        """
        gaps: list[str] = []
        frame, caps = await self.provider.stock_frame(window_days=window_days)
        if frame is None or len(frame) == 0:
            gaps.append(
                "个股日频资金流不可用：本地 Tushare 仓库没有 moneyflow / daily_basic 数据。"
                "补数：`python scripts/quant_warehouse.py sync --dataset moneyflow` "
                "再 `ingest`（流通市值同理需要 daily_basic）")
            return [], [], {}, gaps
        summary = (frame.groupby("code", as_index=False)["net"]
                   .agg(net_avg="mean", latest_net="last"))
        if caps is not None and len(caps):
            summary = summary.merge(caps, on="code", how="left")
        else:
            summary["circ_mv"] = None
            gaps.append("daily_basic 无数据 → 缺流通市值，无法按「净额/流通市值」排序，"
                        "已退化为按净额均值排序")
        summary["net_to_mv"] = summary.apply(
            lambda row: (row["net_avg"] / row["circ_mv"])
            if (row.get("circ_mv") and row["circ_mv"] > 0
                and row.get("net_avg") is not None) else None, axis=1)

        # 涨停池：**只用于**给个股补「涨停原因」列（见下方 pool_info），
        # 不参与选票 —— 选票完全由净额/市值排序决定。
        limit_pool = await self._limit_up_pool()          # 今日涨停池（带原因）

        numeric = summary.dropna(subset=["net_to_mv"]).sort_values(
            "net_to_mv", ascending=False)
        inflow = [str(code) for code in numeric["code"].head(top)]
        outflow = [str(code) for code in numeric["code"].tail(top)][::-1]
        fallback = summary[summary["net_to_mv"].isna()].sort_values(
            "net_avg", ascending=False)
        known = set(summary["code"].astype(str))
        # 自选先登记成集合：它们**不进榜单**，但仍要取数据与走势
        watch_codes = [str(code) for code in dict.fromkeys(extra_codes)]
        watch_set = {code for code in watch_codes if code in known}

        picked: list[str] = []
        group_of: dict[str, str] = {}
        for code, group in (
            *((code, "净流入前10") for code in inflow),
            *((code, "净流出前10") for code in outflow),
            *((str(code), "其他") for code in fallback["code"]),
        ):
            if code not in group_of and code not in watch_set:
                group_of[code] = group
                picked.append(code)
        # 自选里不在榜单的一并取数（顺序按用户加入的先后，保持稳定）
        picked.extend(code for code in watch_codes
                      if code in watch_set and code not in group_of)

        series = await self.provider.stock_series(picked, window_days=window_days)
        names = await self._stock_names(picked)
        realtime = await self._realtime_snapshot(picked)
        entities: dict[str, FlowEntity] = {}
        order: list[FlowEntity] = []
        watch: list[FlowEntity] = []
        by_code = {str(row["code"]): row for _, row in summary.iterrows()}
        for code in picked:
            payload = series.get(code)
            if not payload:
                continue
            row = by_code.get(code)
            pool_info = limit_pool.get(code) or {}
            entity = FlowEntity(kind="stock", code=code,
                                name=names.get(code, code),
                                data_source="Tushare moneyflow（本地仓库）")
            entity.series = [FlowPoint(**point) for point in payload["points"]]
            entity.circ_mv = payload.get("circ_mv")
            # 自选段的票不带 rank_group：身份由"在哪一段"表达，徽标是重复信息
            is_watch = code in watch_set and code not in group_of
            entity.rank_group = "" if is_watch else group_of.get(code, "其他")
            if row is not None:
                # 用 `_finite` 而不是 `value != value`：
                # pandas 的 object 列里缺值是 `None`，而 `None != None` 是 **False** ——
                # 于是 `float(None)` 直接 TypeError（实测踩过）。
                entity.net_avg = _finite(row.get("net_avg"))
                entity.latest_net = _finite(row.get("latest_net"))
                entity.net_to_mv = _finite(row.get("net_to_mv"))
            # ---- 三列新数据：流通市值 / 今日涨幅 / 涨停原因 ----
            # 三者都遵循"取不到就留空"，绝不用别的值冒充（前端会显示 —）。
            quote = realtime.get(code) or {}
            if entity.circ_mv is None and quote.get("circ_mv"):
                entity.circ_mv = quote["circ_mv"]
            if quote.get("change_pct") is not None:
                entity.change_pct = quote["change_pct"]
                entity.change_source = quote.get("source", "")
            if pool_info.get("theme"):
                entity.limitup_reason = pool_info["theme"]
                entity.limitup_date = str(pool_info.get("pool_date") or "")
                if pool_info.get("streak"):
                    entity.limitup_reason += f"（{pool_info['streak']}连板）"
            if entity.series:
                entity.latest_date = entity.series[-1].date
            entity.available = bool(entity.series)
            entities[code] = entity
            # 分两段：自选不占排行榜名额（见 `_rank_stocks` 的说明）
            (watch if is_watch else order).append(entity)
        return order, watch, entities, gaps

    async def _limit_up_pool(self) -> dict[str, dict[str, Any]]:
        """**上一交易日**涨停池（60 秒缓存）：`{code: {theme, streak, ...}}`。

        用途：给「涨停原因」列提供 `所属行业`（东财对当日涨停的题材归类）。
        历史日期由 `provider.limit_up_codes_from_warehouse()` 得到的涨停日决定 ——
        两个来源必须指向**同一天**，否则会出现"股票来自 A 日、原因来自 B 日"
        的错配（实测就这么空过一列）。
        """
        now = time.monotonic()
        if self._pool_cache is not None and (now - self._pool_cache[0]) < QUOTE_TTL:
            return self._pool_cache[1]
        trade_date = await asyncio.to_thread(self._limit_up_trade_date)
        try:
            from src.fundflow.provider import fetch_limit_up_pool

            pool = await fetch_limit_up_pool(trade_date)
        except Exception as exc:  # noqa: BLE001 涨停原因是增强项
            logger.info("涨停池取数失败（涨停原因列将留空）：%s", brief(exc, BRIEF_TIGHT))
            pool = {}
        self._pool_cache = (now, pool)
        return pool

    def _limit_up_trade_date(self) -> str:
        """与 `limit_up_codes_from_warehouse()` **同源**的涨停日（保证两边对得上）。"""
        try:
            codes = self.provider.limit_up_codes_from_warehouse()
            dates = {value for value in codes.values() if value}
            if len(dates) == 1:
                return dates.pop()
            if dates:
                return sorted(dates)[-1]
        except Exception as exc:  # noqa: BLE001
            logger.info("取涨停日失败：%s", brief(exc, BRIEF_TIGHT))
        return ""

    async def _realtime_snapshot(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """腾讯盘中快照（60 秒缓存）：补「今日涨幅 / 流通市值」的实时值。

        本地仓库只有日频收盘值，而这两列用户要盘中能刷新。
        """
        if not codes:
            return {}
        now = time.monotonic()
        if self._quote_cache is not None and (now - self._quote_cache[0]) < QUOTE_TTL:
            cached = self._quote_cache[1]
            if set(codes).issubset(cached.keys()):
                return {code: cached[code] for code in codes}
        try:
            from src.fundflow.provider import fetch_tencent_snapshot

            quotes = await fetch_tencent_snapshot(codes)
        except Exception as exc:  # noqa: BLE001
            logger.info("腾讯快照失败（涨幅/市值将用日频值）：%s", brief(exc, BRIEF_TIGHT))
            quotes = {}
        merged = dict(self._quote_cache[1]) if self._quote_cache else {}
        merged.update(quotes)
        self._quote_cache = (now, merged)
        return {code: merged[code] for code in codes if code in merged}

    async def _sector_histories(
        self, names: list[str], *, realtime: dict[str, dict[str, Any]],
        window_days: int,
    ) -> dict[str, FlowEntity]:
        """并发取多个板块的历史序列（逐条独立失败）。

        三级口径，按可靠性排序：
          1. **Tushare `moneyflow_ind_dc` 按 ts_code 查历史**（首选：一次调用 730 行，
             与截面同源，数值可对齐）；
          2. 东财 akshare 板块历史（备用：名称口径不同，且实测经常不可用）；
          3. 本地聚合（成分股 × 个股 moneyflow 求和：需要成分股接口，实测今天也不通）。
        每一级都把自己的失败原因记进 `notes`，前端能看出"这条线是哪来的、为什么不是首选口径"。
        """
        if not names:
            return {}
        tasks = [self._one_sector_history(name, realtime=realtime,
                                         window_days=window_days)
                 for name in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output: dict[str, FlowEntity] = {}
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                output[name] = FlowEntity(
                    kind="sector", code=name, name=name,
                    gap=f"取数异常：{type(result).__name__}")
            else:
                output[name] = result
        return output

    async def _one_sector_history(
        self, name: str, *, realtime: dict[str, dict[str, Any]], window_days: int,
    ) -> FlowEntity:
        info = realtime.get(name) or {}
        code = str(info.get("code") or "")
        primary = await self.provider.sector_history_tushare(
            name, code, window_days=window_days)
        if primary is not None and primary.available:
            self._attach_realtime(primary, info)
            return primary
        backup = await self.provider.sector_history(name, window_days=window_days)
        if backup.available:
            self._attach_realtime(backup, info)
            if primary is not None and primary.gap:
                backup.notes.append(f"Tushare 口径不可用：{primary.gap}")
            return backup
        local = await self.provider.sector_history_local(
            name, window_days=window_days)
        if local is not None and local.available:
            self._attach_realtime(local, info)
            local.notes.append(
                f"Tushare 与东财口径都不可用（{primary.gap if primary else '无 ts_code'}）；"
                f"{backup.gap or '东财无数据'}")
            return local
        empty = FlowEntity(kind="sector", code=name, name=name)
        empty.gap = ("该板块的历史资金流三级口径都取不到："
                     f"Tushare（{primary.gap if primary else '无 ts_code'}）；"
                     f"东财（{backup.gap or '空'}）")
        self._attach_realtime(empty, info)
        return empty

    @staticmethod
    def _attach_realtime(entity: FlowEntity,
                         info: dict[str, Any]) -> None:
        """把截面（当日）数据挂到实体上：当日净额、净占比、大单、涨跌幅。"""
        if not info:
            return
        entity.today_net = _finite(info.get("net"))
        entity.change_pct = _finite(info.get("pct_change"))
        if entity.data_source and info.get("net_rate") is not None:
            entity.notes.append(
                f"当日净占比 {float(info['net_rate']):+.2f}%"
                + (f"，当日排名 {int(info['rank'])}"
                   if info.get("rank") is not None else ""))

    async def _stock_names(self, codes: list[str]) -> dict[str, str]:
        """股票代码 → 名称（仓库名录；`auto_enrich=False` 不发网络请求）。"""
        return await asyncio.to_thread(self._stock_names_sync, codes)

    def _stock_names_sync(self, codes: list[str]) -> dict[str, str]:
        directory = self._directory()
        if directory is None:
            return {}
        mapping: dict[str, str] = {}
        for code in codes:
            try:
                name = directory.name_of(code, auto_enrich=False)
            except Exception:  # noqa: BLE001 单只查不到不影响其余
                name = ""
            if name:
                mapping[code] = name
        return mapping

    def _directory(self) -> Any:
        """股票名录（进程内单例；不可用则返回 None 并只记一次日志）。"""
        if self._directory_client is not None or self._directory_failed:
            return self._directory_client
        try:
            from src.quant.stock_directory import stock_directory

            self._directory_client = stock_directory()
        except Exception as exc:  # noqa: BLE001 名录不可用不影响资金流本身
            logger.info("资金流监控：股票名录不可用（%s）", brief(exc, BRIEF_TIGHT))
            self._directory_failed = True
        return self._directory_client

    @staticmethod
    def _latest_date(board: FlowBoard) -> str:
        dates: list[str] = []
        for group in (board.sectors, board.stocks, board.sector_rank, board.stock_rank):
            for entity in group:
                if entity.latest_date:
                    dates.append(entity.latest_date)
        if not dates:
            return ""
        # 统一成 YYYYMMDD（个股来自 Tushare 是紧凑格式，板块来自东财是带横杠）
        normalized = sorted(date.replace("-", "") for date in dates)
        return normalized[-1]
