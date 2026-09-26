"""主线挖掘：漏斗编排（三层串起来 + 告警规则 + 快照）。

## 一次 `score_date` 的完整链路

    0. 定位交易日（本地仓里最新的有板块指数的那天）
    1. 预取告警历史（异步，供"确认"与"冷却"判定）
    2. 线程内做纯计算：
       读板块目录 / 板块指数 / 板块资金流 / 成分股并集指标 / 宏观状态
       第一层 score_six_dim        → 全市场六维分 → rank → 候选池（前 20%）
       第二层 score_accumulation   → **只对候选池** → 精选名单（前 5~10）
       第三层 evaluate_resonance   → **只对精选**   → 门控加分
       total = six×w + accumulation×(100−w) + gate_bonus + etf_bonus
       （`w` = `synthesis.layer_weights.six_dim`，V2.3 起为 100）
       告警规则 → 确认 → 冷却
    3. 异步落库（评分 + 告警）

## 三个刻意的边界

**1. 计算线程里不碰协程。** 取数与打分是同步阻塞代码（SQLite + pandas），
放在 `asyncio.to_thread` 里跑；而"确认 / 冷却"要读告警表，是异步的，
因此在进线程**之前**把历史预取出来，以普通 list 传进去。
这样做而不是在同步代码里 `asyncio.run(...)`：后者在事件循环已运行时直接抛
`RuntimeError`，而失败点藏在告警逻辑里，表现是"确认永远不生效" ——
一种不会报错、只会让功能静默失效的写法。

**2. 本层不联网。** 取数全部走 `MainlineDataStore`（本地仓）。接口刷新
不该因为某个数据源超时而挂住 —— 数据同步由 `datastore.sync_all` 在 API /
调度侧显式触发。后果必须说清：本地仓为空时 `score_date` 返回一个
"0 个板块"的快照并在 `gaps` 里写明"请先同步数据"，而不是偷偷联网拉一遍。

**3. 候选池外的板块不伪造第二层分。** `accumulation.score` 在池外恒为 0，
`candidate=False` 才是"没算"的正确读法（见 `models.BoardScore` 文档）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.core.trading_session import (
    AFTERNOON_CLOSE,
    AFTERNOON_OPEN,
    CALL_AUCTION_START,
    MORNING_CLOSE,
    MORNING_OPEN,
    minutes_of,
)
from src.mainline.accumulation import AccumulationInput, score_accumulation
from src.mainline.config import MainlineConfig, load_config
from src.mainline.datastore import (
    MainlineDataStore,
    apply_pure_pool,
    filter_listed,
)
from src.mainline.etf import LEVEL_LABELS, board_etf_signals, breakout_bonus
from src.mainline.etf import load_mapping as load_etf_mapping
from src.mainline.leader import LeaderInput, evaluate_resonance, score_leader
from src.mainline.macro import (
    MacroSensitivity,
    build_macro_state,
    load_sensitivity,
)
from src.mainline.models import (
    AlertSignal,
    BoardScore,
    BoardSeries,
    MainlineSnapshot,
    SignalLevel,
)
from src.mainline.pool import resolve_pool as resolve_member_pool
from src.mainline.scoring import (
    breakout_ceiling,
    clamp,
    is_breakout,
    to_float,
    weighted_score,
)
from src.mainline.six_dim import BoardInput, candidate_cutoff, score_six_dim

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _shift(stamp: str, days: int) -> str:
    """`YYYYMMDD` 平移自然日（把"交易日窗口"换成足够宽的取数区间）。"""
    try:
        base = datetime.strptime(stamp, "%Y%m%d")
    except (TypeError, ValueError):
        return stamp
    return (base + timedelta(days=int(days))).strftime("%Y%m%d")


def session_label(moment: datetime | None = None) -> tuple[str, str]:
    """当前 A 股时段 `(state, label)`（盘前 / 早盘 / 午间 / 尾盘 / 已收盘）。

    刻意只做轻量判断而不引入完整交易日历：这个标签只用于面板提示
    "你看到的是不是当日数据"，不参与任何打分。

    ⚠️ **边界取自 `core.trading_session`，不在这里重写**。
    本模块的**分段**与做T/资金流不同（这里把上午拆成 集合竞价 / 早盘，
    它们合成一个 `trading`），所以标签确实该各写各的；
    但"几点算开盘、几点算午休"是**交易所口径**，各处各写一遍就会漂移 ——
    本项目已经有过两份 `session_state` 逐行重复的教训。
    """
    now = moment or datetime.now()
    if now.weekday() >= 5:
        return "closed", "非交易日"
    minutes = minutes_of(now)
    if minutes < CALL_AUCTION_START:
        return "pre", "盘前"
    if minutes < MORNING_OPEN:
        return "auction", "集合竞价"
    if minutes < MORNING_CLOSE:
        return "morning", "早盘"
    if minutes < AFTERNOON_OPEN:
        return "noon", "午间休市"
    if minutes < AFTERNOON_CLOSE:
        return "afternoon", "午盘"
    return "closed", "已收盘"


@dataclass
class MainlineService:
    """主线挖掘服务。

    `repo` 为 None 时仍能算分（内存态），只是不落库 —— 与
    `build_mainline_repository` 的降级语义一致。
    """

    config: MainlineConfig = field(default_factory=load_config)
    store: MainlineDataStore | None = None
    repo: Any = None
    _sensitivity: MacroSensitivity | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = MainlineDataStore(config=self.config)

    @property
    def sensitivity(self) -> MacroSensitivity:
        """行业宏观敏感度表（懒加载一次；文件缺失时返回空表 + gap）。"""
        if self._sensitivity is None:
            self._sensitivity = load_sensitivity(self.config)
        return self._sensitivity

    # ---------- 对外入口 ----------

    async def score_date(self, trade_date: str = "", *, save: bool = True,
                         limit_boards: int = 0) -> MainlineSnapshot:
        """算一个交易日的完整漏斗并返回快照（异步；重活在子线程里跑）。"""
        history = await self._alert_history(trade_date)
        snapshot = await asyncio.to_thread(
            self._compute, trade_date, limit_boards, history)
        if save and self.repo is not None and snapshot.scores:
            await self._persist(snapshot)
        return snapshot

    def score_date_sync(self, trade_date: str = "", *, limit_boards: int = 0,
                        alert_history: Sequence[dict] | None = None
                        ) -> MainlineSnapshot:
        """同步入口（**不落库、不做确认**）—— 回测与测试用。

        回测要跑上千个交易日，每个都落库既慢又没意义（回测自己的记录在
        `mainline_backtest` 表里）；确认与冷却在回测里由 `backtest.py`
        自己按下标推进，不查库。
        """
        return self._compute(trade_date, limit_boards, list(alert_history or []))

    async def _persist(self, snapshot: MainlineSnapshot) -> None:
        try:
            await self.repo.save_scores(snapshot.trade_date, snapshot.scores)
            if snapshot.alerts:
                await self.repo.save_alerts(snapshot.alerts)
        except Exception as exc:  # noqa: BLE001 落库失败不该让快照拿不到
            logger.warning("主线结果落库失败（不影响本次结果）：%s",
                           brief(exc, BRIEF_TIGHT))

    async def _alert_history(self, trade_date: str) -> list[dict]:
        """预取告警历史（确认与冷却判定用；读不到就返回空 = 不确认不冷却）。"""
        if self.repo is None:
            return []
        target = trade_date or self._latest_trade_date()
        if not target:
            return []
        span = max(int(self.config.alert.cooldown_days), 1) * 6 + 30
        try:
            return await self.repo.load_alerts(
                start=_shift(target, -span), end=target, limit=800)
        except Exception as exc:  # noqa: BLE001
            logger.warning("告警历史读取失败（本次不做确认/冷却）：%s",
                           brief(exc, BRIEF_TIGHT))
            return []

    # ---------- 计算主体（同步、纯本地） ----------

    def _compute(self, trade_date: str, limit_boards: int,
                 alert_history: list[dict]) -> MainlineSnapshot:
        started = time.monotonic()
        gaps: list[str] = []
        notes: list[str] = []
        store = self.store
        assert store is not None  # __post_init__ 保证

        target = trade_date or self._latest_trade_date()
        if not target:
            return self._empty_snapshot(
                "本地数据仓没有可用交易日：请先执行数据同步")

        boards = store.boards()
        if not boards:
            return self._empty_snapshot(
                "板块目录为空：请先执行数据同步（board / member / board_bar）")
        cap = int(limit_boards or self.config.data.max_boards)

        # ---------- 1) 取数（全部本地） ----------
        history_days = max(int(self.config.min_history_days), 60)
        start = _shift(target, -int(history_days * 2.2))   # 交易日 → 自然日宽放

        # ⚠️ 顺序：**先按"本地有没有指数数据"筛，再按 max_boards 截断**。
        # 反过来（先截断再筛）会按 `(kind, code)` 取前 N 个 —— 那个顺序与
        # "板块重不重要"毫无关系：概念代码段 700xxx/86xxxx 排在 88xxxx 之前，
        # 于是医药、半导体等概念在打分**之前**就被丢掉，
        # 极端情况下可用板块数直接是 0，而面板上只显示"全市场 0 个板块"。
        with_bars = store.board_codes_with_bars(start=start, end=target)
        available = [item for item in boards if item.code in with_bars]
        if len(available) < len(boards):
            gaps.append(f"{len(boards) - len(available)} 个板块没有本地指数数据"
                        "（未参与打分）")
        # ⚠️ `board_codes_with_bars` 只判"**窗口内**有数据"，不判"**当天**有数据"：
        # 上游指数是逐板块陆续发布当天的（2026-09-23 实测 19:07 只有 66/138），
        # 缺当天的那批会拿**上一交易日的 K 线**参与当天打分 —— 分数照出、排名照排。
        # 不改变打分口径（沿用前一交易日仍是一种"as-of"），但**必须说出来**。
        if available:
            on_target = store.board_codes_with_bar_on(target)
            stale = [item for item in available if item.code not in on_target]
            if stale:
                gaps.append(f"{len(stale)} 个板块 {target} 当日没有指数数据"
                            "（其分数沿用上一交易日的 K 线）")
        if not available:
            return self._empty_snapshot(
                "板块指数为空：请先执行数据同步（board_bar）")
        # 截断时按成分股数从大到小：至少保证丢掉的是小板块，而不是随机的
        available.sort(key=lambda item: (-int(item.members or 0), item.code))
        if cap > 0 and len(available) > cap:
            gaps.append(f"标的池超过上限 {cap}，已按成分股数截断"
                        f"（{len(available)} → {cap}）")
            available = available[:cap]
        boards = available
        codes = [item.code for item in boards]
        series_map = store.board_bars(codes, start=start, end=target)

        flow_window = max(int(self.config.six_dim.windows.get("moneyflow", 5)), 5)
        flows = store.board_flows(codes, start=_shift(target, -flow_window * 4),
                                  end=target)
        if not flows:
            notes.append("板块资金流为空（资金流维度不参与加权）")

        member_map = store.member_map()

        # ---------- 上市日期闸门（回测防前视偏差） ----------
        #
        # ⚠️ **这一步必须放在所有成分股过滤之前**（提纯、老口径裁剪），
        # 否则后面那些步骤会拿着"未来才上市的票"去算相关性/市值，
        # 再被这里筛掉 —— 那等于用未来信息参与了筛选，闸门就白设了。
        #
        # 为什么需要它：`ml_member` 是**当前快照**（全部 401914 行的 `out_date`
        # 都是空，没有任何历史移出记录），而回测某一天时用的却是这份今天的名单。
        # 一只 2026 年才上市的票会被算进 2023 年的板块资金流里。
        #
        # 闸门只修住"当时还没上市"这一个方向，但那是**最严重**的方向：
        # 新股上市初期的成交/资金特征与老股完全不同（连板、换手极高），
        # 混进去会系统性地抬高板块分。实测池内 5510 只成分股里
        # **99.86% 能查到上市日期**（`scripts/_diag_list_date.py`），
        # 起点 2025-10 剔除 1.38%、2023-10 剔除 4.46%。
        #
        # 查不到上市日期的票：**保守剔除**。理由——`quant_stock_basic` 按
        # `list_status='L'` 同步，"查不到"基本等于"已退市/暂停上市"，
        # 而一份**当前**成分股名单里不该出现已退市的票；宁可少算，
        # 也不要让一批状态未知的代码参与打分（数量与影响都记进 notes）。
        list_dates = store.stock_list_dates()
        if list_dates and member_map:
            member_map, dropped_unlisted, dropped_unknown = filter_listed(
                member_map, list_dates, target)
            if dropped_unlisted or dropped_unknown:
                notes.append(
                    f"上市日期闸门（{target}）：剔除尚未上市 "
                    f"{dropped_unlisted} 个 (板块,股票) 对、"
                    f"上市日期缺失 {dropped_unknown} 个")
        elif member_map:
            notes.append("上市日期闸门未生效：拿不到个股上市日期"
                         "（行情仓不可用且 ml_stock_meta 为空）——"
                         "回测会有前视偏差")

        # ---------- 股池提纯（去掉"蹭概念"的噪声股） ----------
        #
        # **优先用 `ml_member_pure`**（用户配置的股池口径：走势相关性 +
        # 主营业务相关度），它缺失或过期时才回落到 `relevance` 口径。
        #
        # 为什么换口径：`relevance.clean_member_map` 的 `rel_ok` 判据是
        # "在 `ml_member_clean` 里有 relevant=1 的行"，而那张表只评过
        # **34%** 的 (板块,股票) 对 —— 于是 65.9% 从未被评估的对被
        # 当成"不相关"**静默剔除**（实测 885564 无人机 467→6、
        # 885611 阿里巴巴概念 347→1）。`ml_member_pure` 在池内覆盖 81%，
        # 且 `apply_pure_pool` 明确区分「判定为 0」与「没有行」。
        #
        # ⚠️ 过滤只影响**打分用的池子**，`ml_member` 里的原始成分股一条都不动。
        # "这次用哪些票算分"与"这个板块有哪些票"是两件事，后者改错了不可逆。
        pure_map = store.pure_member_relevance()
        pure_used = False
        if pure_map:
            before = sum(len(v) for v in member_map.values())
            member_map, dropped, unrated = apply_pure_pool(member_map, pure_map)
            after = sum(len(v) for v in member_map.values())
            pure_used = True
            notes.append(
                f"股池提纯 @{target}（ml_member_pure）：{before} → {after} 只"
                f"（剔除 {dropped} 个判定不相关、{unrated} 个未评估已保留）")
            # 提纯结果比成分股名单旧 → 说明名单刷新过，新成员全是"未评估"。
            # 这不是错误（未评估会被保留），但必须能被看见。
            if store.first_sync_epoch() and store.member_epoch():
                hours = (store.member_epoch() - store.first_sync_epoch()) / 3600
                if hours > 6:
                    notes.append(
                        f"⚠️ ml_member_pure 比 ml_member 旧 {hours:.1f} 小时 —— "
                        "期间新增的成分股按「未评估」保留，"
                        "如需正式判定请重跑 scripts/purify_members.py"
                        "（走缓存，不重复计费）")
        else:
            notes.append("ml_member_pure 不可用，回落到 relevance 口径")

        rel_cfg = self.config.relevance
        if not pure_used and rel_cfg.enabled and member_map:
            from src.mainline.relevance import (
                RelevanceStore,
                clean_member_map,
            )

            warehouse = getattr(store, "warehouse", None)
            wh_path = getattr(warehouse, "path", "") if warehouse is not None else ""
            if not wh_path:
                notes.append("成分股提纯未生效：本地行情仓不可用（市值/ST 无法判定）")
            else:
                try:
                    outcome = clean_member_map(
                        member_map, store=RelevanceStore(self.config.cache_file),
                        warehouse_path=wh_path, trade_date=target,
                        kinds=rel_cfg.kinds,
                        min_total_mv=rel_cfg.min_total_mv,
                        exclude_st=rel_cfg.exclude_st,
                        top=rel_cfg.top_themes)
                    member_map = outcome.members
                    notes.append(outcome.summary())
                    notes.extend(item for item in outcome.gaps if item)
                except Exception as exc:  # noqa: BLE001 提纯失败不该让整页 500
                    notes.append(
                        f"成分股提纯失败（改用全部成分股）：{brief(exc, BRIEF_TIGHT)}")
        elif self.config.pool.enabled and member_map:
            # 老口径：成交额前 30%。需要当日及之前的成交额 —— 用未来数据算
            # "相关度"等于让板块自动选中未来的强势股，回测会好得离谱。
            all_codes = sorted({code for codes in member_map.values()
                                for code in codes})
            market_stats = store.bulk_member_market_stats(
                {"__all__": all_codes}, trade_date=target).get("__all__", {})
            names = warehouse_names(store, all_codes)
            for code, name in names.items():
                market_stats.setdefault(code, {})["name"] = name
            caps = store.member_stats(all_codes, trade_date=target)
            for code, item in caps.items():
                market_stats.setdefault(code, {})["circ_mv"] = item.get("circ_mv")
            pool_outcome = resolve_member_pool(
                member_map,
                board_names={b.code: b.name for b in boards},
                market_stats=market_stats, config=self.config)
            member_map = pool_outcome.members
            notes.append(pool_outcome.summary())

        all_members = sorted({code for item in boards
                              for code in member_map.get(item.code, [])})
        stats = store.member_stats(all_members, trade_date=target)
        if not stats:
            notes.append("成分股指标为空（景气度 / 筹码维度不参与加权）")
        state = build_macro_state(store, self.config,
                                  sensitivity=self.sensitivity)
        if not state.available:
            notes.append("宏观状态不可用（宏观维度不参与加权）")

        # ---------- 2) 第一层 ----------
        inputs: list[BoardInput] = []
        close_map: dict[str, float] = {}
        for item in boards:
            members = member_map.get(item.code, [])
            member_stats = {code: stats[code] for code in members
                            if code in stats}
            circ = sum(to_float(row.get("circ_mv")) or 0.0
                       for row in member_stats.values())
            flow = flows.get(item.code)
            if not circ and flow is not None:
                circ = _flow_circ_mv(store, item.code, target)
            series = series_map[item.code]
            inputs.append(BoardInput(
                info=item, series=series, flow=flow, members=members,
                member_stats=member_stats, circ_mv=circ or None))
            if series.bars:
                close_map[item.code] = series.bars[-1].close

        # 景气度「增速变化率」口径需要 `W` 个交易日**之前**的基本面原始值，
        # 而 `member_stats` 只有当日 —— 从历史评分行的 payload 里取
        # （与突破触发读 `total` 历史同一套路，读的都是结果库 `self.repo`）。
        # 取不到就返回空字典，`score_six_dim` 会自动回退到水平值口径。
        history_raws: dict[str, dict[str, float]] = {}
        delta_below = float(getattr(self.config.six_dim, "prosperity_delta_below",
                                    0.0) or 0.0)
        if delta_below > 0 and self.repo is not None:
            try:
                history_raws = self.repo.prosperity_raw_history_sync(
                    target, lookback_days=int(getattr(
                        self.config.six_dim, "prosperity_delta_window", 60)))
            except Exception as exc:  # noqa: BLE001 读不到历史不该打断打分
                logger.info("景气度变化率：读历史失败 %s", brief(exc, BRIEF_TIGHT))
                history_raws = {}

        six = score_six_dim(inputs, config=self.config, state=state,
                            sensitivity=self.sensitivity,
                            history_raws=history_raws or None)

        layer_weights = self.config.synthesis.normalized_layers()
        scores: dict[str, BoardScore] = {}
        for item in inputs:
            layer, _ = six[item.code]
            board = BoardScore(code=item.code, name=item.name,
                               trade_date=target, kind=item.info.kind,
                               six_dim=layer)
            profile = self.config.board_profiles.profile_for(item.name)
            board.profile_key = profile.key
            board.decay_days = profile.decay_days
            board.weights = dict(layer_weights)
            bars = item.series.bars
            board.change_pct = bars[-1].pct_change if bars else None
            board.gaps = list(layer.notes)
            scores[item.code] = board

        ranked = sorted(scores.values(),
                        key=lambda row: (-row.six_dim.score, row.code))
        for index, board in enumerate(ranked, start=1):
            board.rank = index
        candidate_set = set(candidate_cutoff(
            {code: (row.six_dim, {}) for code, row in scores.items()},
            config=self.config))
        for board in scores.values():
            board.candidate = board.code in candidate_set

        # ---------- 3) 第二层（只对候选池） ----------
        #
        # ETF 资金异动信号先对**全体板块**算一次：它的数据源（ETF 日线+份额）
        # 与板块指数完全独立，一次批量读取就能覆盖所有板块，成本与板块数无关。
        # 它有两个用途，且都**不占第二层的权重**（见 `AccumulationConfig`）：
        #   1. 对全体板块给最终分加分（`etf_bonus`，0~20）；
        #   2. 对候选池外的板块做**提名触发**（见 `_nominate`）。
        # 这两个用途正是"ETF 放量"能在板块指数还没动时提前发现主线的路径。
        etf_signals: dict[str, Any] = {}
        etf_gap = ""
        if self.config.etf.enabled:
            etf_signals, etf_gap = self._load_etf_signals(boards, target, store)
            if etf_gap:
                gaps.append(etf_gap)
            # 加分对**全体板块**算（含候选池外）：加分是绝对量、不参与归一化，
            # 只依赖该板块自己的 ETF 信号，所以不受"有没有进候选池"影响。
            for board in scores.values():
                signal = etf_signals.get(board.code)
                board.etf_bonus = breakout_bonus(signal, config=self.config)
                board.etf_level = int(signal.level_of()) if signal else 0
                if board.etf_bonus > 0 and signal is not None:
                    board.reasons.append(
                        f"ETF {LEVEL_LABELS[board.etf_level]}，"
                        f"最终分 +{board.etf_bonus:.0f}")
                    if signal.breakout_etfs:
                        code, name, share, ratio = signal.breakout_etfs[0]
                        board.reasons.append(
                            f"  · {name or code} 份额 {share * 100:+.2f}%、"
                            f"成交额 {ratio:.2f} 倍")

        margin_start = _shift(target, -int(max(
            self.config.accumulation.leverage.low_position_window, 60) * 2.2))
        nb_start = _shift(target, -420)
        acc_inputs: list[AccumulationInput] = []
        for item in inputs:
            if item.code not in candidate_set:
                continue
            members = member_map.get(item.code, [])
            acc_inputs.append(AccumulationInput(
                code=item.code, name=item.name, series=item.series,
                margin=store.board_margin_series(members, start=margin_start,
                                                 end=target),
                northbound=store.board_northbound_series(members, start=nb_start,
                                                         end=target),
                etf=etf_signals.get(item.code),
                circ_mv=item.circ_mv))
        acc = score_accumulation(acc_inputs, config=self.config)
        for code, (layer, _raw) in acc.items():
            board = scores.get(code)
            if board is None:
                continue
            board.accumulation = layer
            board.gaps.extend(layer.notes)

        # ---------- 4) 按 `layer_weights` 合成（V2.3 起 100/0） ----------
        #
        # ⚠️ 用 `weighted_score` 而不是手写 `six*sw + acc*aw`：后者**不看层的
        # `available`**，而层没数据时它的 `score` 就是 0（`weighted_score` 的
        # 契约是"全缺 → (0.0, 0.0)"）。于是"没测到"被算成"测得极差"，
        # 第二层没数据的板块 `base_total` 被腰斩 ——
        # 这与 `weighted_score` 内部"缺维度剔出分母"是同一条原则，
        # 只是高了一层，却漏了。
        #
        # 实测（`scripts/coverage_fill_experiment.py`，V2.2 备份）：
        # 旧窗口超过一成候选板块的第二层覆盖率为 0，而它们随后**跑赢** ——
        # `accumulation_coverage` 的 H20/H60 IC 是 −0.0455/−0.0805
        # （覆盖率本不该有预测能力）。修好后旧窗口 50/50 的池内 IC
        # 由 +0.0539/+0.0569 升到 +0.0796/+0.1053；
        # 中/新窗口覆盖率本就 >0.93，**一格不变**。
        six_w = layer_weights.get("six_dim", 50.0)
        acc_w = layer_weights.get("accumulation", 50.0)
        for board in scores.values():
            if not board.candidate:
                # 池外：第二层没算过，用第一层分维持一个展示值。
                # 它**不能**与候选池内的分直接比较 —— 面板靠 `candidate` 区分。
                board.base_total = board.six_dim.score
                continue
            board.base_total, _ = weighted_score([
                (board.six_dim.score, six_w, board.six_dim.available),
                (board.accumulation.score, acc_w,
                 board.accumulation.available),
            ])

        # ---------- 5) 第三层：对**全体板块**做龙头共振评估 ----------
        #
        # ⚠️ 评估范围曾经只有"精选前 10"，这是 2024-06 医药漏报的直接原因：
        # 医疗服务板块的龙头资金集中度已到 110%~162%（药明康德逆势吸筹），
        # 但它第一层排 61/93、没进候选池 → 第三层从未执行 → 20 分门控被丢弃。
        # 现在改成：**先对全池算共振，再据此决定精选名单**。
        # 批量取数后这一步的成本是常数级（见 `bulk_member_market_stats`）。
        self._apply_leader(list(scores.values()), member_map, target=target,
                           store=store, series_map=series_map)

        # 精选名单按**含加分的总分**排 —— 这样共振/ETF 异动才能真正把板块
        # "提进"精选，而不是只给已经选中的板块锦上添花。
        pool = sorted((row for row in scores.values() if row.candidate),
                      key=lambda row: (-(row.base_total + row.gate_bonus
                                         + row.etf_bonus), row.code))
        selected = pool[:self.config.funnel.selected(len(pool))]
        for board in selected:
            board.selected = True

        # ⚠️ 加分**只在入选后兑现**：`gate_bonus` 在排序阶段只是"潜力"，
        # 排完序要把没入选的候选板块的分加回去。
        #
        # 为什么：共振评估范围从"精选 10 个"扩大到"候选池 60~80 个"之后，
        # 如果每个候选板块都实发 +15~20 分，强信号会从日均个位数涨到十几条
        # （实测 2024-06-12 的告警从 2 条涨到 18 条，占全市场 19%），
        # 告警彻底失去区分度 —— 而"告警太多"和"没有告警"对使用者是一样的。
        # 用"潜力分"排序、只给入选者兑现，既保住了"共振能把板块提进精选"，
        # 又让最终信号数量维持在可读的规模。
        selected_codes = {board.code for board in selected}
        for board in scores.values():
            # 先把"潜力分"留档再做清零：`total` 里用的是**兑现后**的
            # `gate_bonus`，而真正的排序键是"兑现前"的那个值。不存下来的话，
            # IC 报告只能拿"平滑分数 + 稀疏跳变"算秩相关，**系统性低估**
            # 排序质量（实测 `base_total` IC +0.075/+0.085 对 `total` +0.052/+0.040）。
            # 详见 `BoardScore.bonus_potential` 与 §16.35。
            board.bonus_potential = board.gate_bonus
            if board.candidate and board.code not in selected_codes:
                if board.gate_bonus:
                    board.gaps.append(
                        f"龙头共振（集中度 {(board.resonance_ratio or 0) * 100:.0f}%）"
                        "参与了排序但未入选精选，本次不计入加分")
                board.gate_bonus = 0.0

        # 提名通道：没进候选池、但龙头共振足够强（或 ETF 异动确认）的板块也送进告警
        for board in self._nominate(scores, pool, etf_signals=etf_signals):
            board.promoted = True

        # ---------- 6) 合成最终分 + 告警 ----------
        #
        # `etf_bonus` 无条件兑现（不像 `gate_bonus` 那样要先入选精选）：
        # ETF 历史级放量是稀有事件，而且它恰恰要能把"还没进精选"的板块
        # 顶上来 —— 等入选才给就失去了这个作用。理由详见 `BoardScore.etf_bonus`。
        for board in scores.values():
            board.total = clamp(board.base_total + board.etf_bonus
                                + board.gate_bonus)

        # ---------- 5.5) 突破触发：越过**它自己**的长期震荡上沿 ----------
        #
        # 为什么不能只看绝对阈值：阈值是全市场统一的，所以"能不能报"
        # 取决于分数够不够极端，而不是"这个板块是不是刚突破自己的区间"。
        # 实测农业种植 885812 在 20260623 的六维排名是**全市场第 8**
        # （板块确实启动了），但 `total` 只有 62.3、低于中信号线 → 不报；
        # 真正报出来靠的是三天后的门控加分。全市场普涨时真领涨的板块
        # 就会被绝对阈值漏掉，等它"足够极端"时行情已到中后段。
        self._apply_breakout(scores, target)

        alerts = self._build_alerts(scores, target, close_map,
                                    history=alert_history)
        tracked = self._track(selected, inputs)

        ordered = sorted(scores.values(),
                         key=lambda row: (-row.total, row.code))
        label_state, label_text = session_label()
        return MainlineSnapshot(
            generated_at=_now(), trade_date=target,
            session_state=label_state, session_label=label_text,
            scores=ordered, alerts=alerts,
            weight_mode="static", weights=dict(layer_weights),
            ic_summary=self._pool_summary(acc),
            board_count_total=len(inputs),
            candidate_count=len(pool), selected_count=len(selected),
            tracked=tracked,
            source_notes=notes + [f"用时 {time.monotonic() - started:.1f}s"],
            gaps=gaps, refresh_hint=self._refresh_hint(target),
            disclaimer=self.config.disclaimer)

    # ---------- 第三层 ----------

    def _apply_leader(self, boards: Sequence[BoardScore],
                      member_map: dict[str, list[str]], *, target: str,
                      store: MainlineDataStore,
                      series_map: dict[str, BoardSeries]) -> None:
        """对**给定的一批板块**做龙头识别与共振门控（含龙虎榜席位）。

        ## 为什么是"一批"而不是"精选名单"

        调用方传进来的是**全体板块**（或候选池）。评估范围放在这里而不是
        写死成"精选"，是因为 2024-06 的教训：共振信号出现在一个第一层排名
        第 61 的板块上，而旧实现只对前 10 名评估 —— 信号在被看到之前就被丢了。

        ## 为什么全部取数都批量

        逐个板块调 `member_market_stats` / `board_flows` 是 0.17 秒/板块，
        全市场 300 个板块就是 50 秒/轮。这里改成一次性取：
        成分股量价一条 SQL（`bulk_member_market_stats`）、板块资金流一条、
        龙虎榜一条、股票名一条。板块数从 10 涨到 300 时，耗时基本不变。
        """
        cfg = self.config.leader
        window = max(int(cfg.capital_window), 5)
        seat_start = _shift(target, -int(max(cfg.seat.lookback_days, 1) * 2.5))
        seats_by_code: dict[str, list[Any]] = {}
        for row in store.seats(start=seat_start, end=target):
            seats_by_code.setdefault(row.code, []).append(row)

        codes = [board.code for board in boards]
        subset = {code: member_map.get(code, []) for code in codes}
        market_map = store.bulk_member_market_stats(subset, trade_date=target)
        flow_start = _shift(target, -window * 4)
        flows = store.board_flows(codes, start=flow_start, end=target)

        warehouse = getattr(store, "warehouse", None)
        all_members = sorted({code for members in subset.values()
                              for code in members})
        names = (warehouse.stock_names(all_members)
                 if warehouse is not None else {})
        # 业务相关性：**只用于展示**（让"资金龙头"与"业务龙头"分得开）。
        # 取不到就退化成空 dict，不影响任何判定。
        business = {}
        reader = getattr(store, "pure_member_business", None)
        if callable(reader):
            try:
                business = reader()
            except Exception as exc:  # noqa: BLE001 展示信息拿不到不该让打分失败
                logger.info("主打分：读取成分股业务信息失败（仅影响展示）：%s",
                            brief(exc, BRIEF_TIGHT))

        for board in boards:
            members = subset.get(board.code, [])
            if not members:
                continue
            flow = flows.get(board.code)
            board_net = None
            if flow is not None and flow.points:
                tail = [net for _, net in flow.points[-window:]
                        if net is not None]
                board_net = float(sum(tail)) if tail else None
            item = LeaderInput(
                code=board.code, name=board.name,
                series=series_map.get(board.code) or BoardSeries(code=board.code),
                board_net=board_net, members=market_map.get(board.code) or {},
                seats=[row for code in members
                       for row in seats_by_code.get(code, [])],
                names={code: names.get(code, "") for code in members},
                business=business.get(board.code) or {})
            outcome = evaluate_resonance(item, config=self.config)
            board.leader = score_leader(item, outcome, config=self.config)
            board.resonance = outcome.triggered
            board.resonance_ratio = outcome.concentration
            board.leaders = outcome.leaders
            board.seat_note = outcome.seat_note
            board.gate_bonus = outcome.bonus
            board.gaps.extend(outcome.notes)

    def _load_etf_signals(self, boards: Sequence[Any], target: str,
                          store: MainlineDataStore
                          ) -> tuple[dict[str, Any], str]:
        """算一次全市场的板块级 ETF 资金异动信号。

        返回 `({板块代码: EtfSignal}, gap说明)`。ETF 行情与份额都读本地仓
        （`sync_etf` 落库），因此这一步不联网。

        ## 为什么先查 `ml_etf` 里有没有数据再决定要不要报缺口

        没同步过 ETF 时表格是空的：此时**不该**把每个板块都标成"数据缺口"
        （89 个板块各一条缺口，会把真正的告警淹没），只需要一条全局说明。
        """
        mapping = load_etf_mapping(self.config)
        if not mapping.loaded:
            return {}, mapping.gap or "ETF 映射表未加载"
        codes = store.etf_codes()
        if not codes:
            return {}, "本地还没有 ETF 数据（执行一次数据同步即可启用该维度）"
        start = _shift(target, -int(self.config.etf.history_days * 1.6))
        bars = store.etf_bars(codes, start=start, end=target)
        if not bars:
            return {}, f"本地 ETF 行情为空（{len(codes)} 只已登记）"
        board_by_name = {item.name: item.code for item in boards}
        signals = board_etf_signals(
            bars, mapping, board_by_name=board_by_name,
            window=int(self.config.etf.window),
            breakout_amount_ratio=float(self.config.etf.breakout_amount_ratio),
            breakout_percentile=float(self.config.etf.breakout_percentile))
        if not signals:
            return {}, "ETF 映射没有命中任何本地板块（检查 mainline_etf_mapping.yaml）"
        return signals, ""

    def _nominate(self, scores: dict[str, BoardScore],
                  pool: Sequence[BoardScore],
                  etf_signals: dict[str, Any] | None = None
                  ) -> list[BoardScore]:
        """提名通道：把"没进候选池但有强异动"的板块送进告警。

        这是本模块**仅有的两条绕过第一层初筛**的路径，都必须窄：

        **① 龙头共振提名**
            concentration ≥ `nominate_min_concentration`（默认 0.45）
            six_dim       ≥ `nominate_min_score`        （默认 35）

        **② ETF 资金异动提名**（`etf.nominate_on_breakout`）
            一级：成交额达到自身近 60 日的 2 倍且 90 分位以上
            二级：一级 + 份额净申购
            six_dim       ≥ `nominate_min_score`

        两条都要求第一层分不低于下限。只留集中度/异动会捞进一堆"跌了很久、
        刚好有人抢反弹"的小板块；只留分数则退化成"候选池的补充"，
        起不到"第一层看不到拐点"时的兜底作用。

        ⚠️ ETF 提名**一级就放行**（不要求净申购）：理由是"份额没净申购 ≠
        没有资金异动"（折价套利承接、份额未更新都会如此），二值耦合不该存在。
        召回优先，精度由加分区分（一级 +6 / 二级 +12）。

        注意：**这一条不是农业 2026-06/07 那一案的解药** —— 实测 562900 在
        06-26 / 06-29 的成交额只有中位的 1.54 / 1.98 倍，没到 2.0 门槛，
        所以那两天即便放行一级也不会触发。详见 `src/mainline/etf.py` 模块头。

        ## 两条通道各自有独立预算（`nominate_etf_limit`）

        实测（2026-07-06）：两条通道共用 `nominate_limit = 8` 时，当天满足共振
        条件的板块就有 8 个以上，于是**ETF 通道实际拿到 0 个名额** ——
        农业种植凭 ETF 二级异动加了 15 分、总分 67.99，却**一条告警都没有**。

        为什么这会让整条 ETF 链路失效：`_decide_level` 规定**候选池外的板块
        只能走提名通道**（池外没算第二层，分数与池内不可比）。所以提名名额被
        占满 = 池外板块无论 ETF 异动多强都不会告警。第一层看不到的拐点，
        正是这个通道存在的理由；被另一条通道饿死就本末倒置了。
        """
        alert_cfg = self.config.alert
        if not alert_cfg.nominate_enabled:
            return []
        floor = float(alert_cfg.nominate_min_concentration)
        min_score = float(alert_cfg.nominate_min_score)
        candidates = [row for row in scores.values()
                      if not row.candidate and row.six_dim.score >= min_score]
        resonance: list[tuple[float, float, BoardScore, str]] = []
        etf: list[tuple[float, float, BoardScore, str]] = []
        for row in candidates:
            if row.resonance and (row.resonance_ratio or 0.0) >= floor:
                resonance.append((
                    row.resonance_ratio or 0.0, 0.0, row,
                    f"龙头共振提名：集中度 {(row.resonance_ratio or 0) * 100:.0f}%"
                    f"（第一层排 {row.rank}，未进候选池，未经第二层）"))
                continue
            if not self.config.etf.nominate_on_breakout or not etf_signals:
                continue
            signal = etf_signals.get(row.code)
            # 聚合口径与单只口径、一级与二级，任一成立即可触发
            # （理由见 `EtfSignal.any_breakout` 与 `_nominate` 的说明）
            if signal is None or signal.level_of() <= 0:
                continue
            scope = {2: "二级：放量+净申购", 1: "一级：历史级放量"}.get(
                signal.level_of(), "异动")
            detail = ""
            if signal.breakout_etfs:
                code, name, share, ratio = signal.breakout_etfs[0]
                detail = (f"（{name or code} 份额 {share * 100:+.2f}%、"
                          f"成交额 {ratio:.2f} 倍）")
            # ETF 通道的排序键**不能**用第一层分。
            #
            # 用六维分降序是错的：那样排在前面的是"第一层本来就强"的板块，
            # 而它们根本不需要这条通道 —— 通道存在的理由恰恰是捞第一层看不见的
            # 拐点（农业就是第一层排 22、进不了候选池）。按第一层分排会把这个
            # 目的反过来。所以按**异动强度**排（`EtfSignal.strength()`：
            # 放量倍数 × 自身分位，聚合与单只两个口径取较大者）。
            strength = signal.strength()
            etf.append((
                float(signal.level_of()), strength, row,
                f"ETF 资金异动提名（{scope}）：{signal.etf_count} 只 ETF "
                f"成交额放大 {(signal.amount_ratio or 0):.2f} 倍"
                f"、份额 {(signal.share_change or 0) * 100:+.2f}%"
                f"{detail}"
                f"（第一层排 {row.rank}，未进候选池，未经第二层）"))

        # 共振按集中度降序（资金在个股上更直接的证据）
        resonance.sort(key=lambda item: -item[0])
        # ETF 按"级别 → 异动强度"降序（理由见上面的注释）
        etf.sort(key=lambda item: (-item[0], -item[1]))
        limit = max(0, int(alert_cfg.nominate_limit))
        etf_limit = max(0, int(alert_cfg.nominate_etf_limit))
        out: list[BoardScore] = []
        for _ratio, _strength, board, reason in (resonance[:limit]
                                                 + etf[:etf_limit]):
            board.reasons.append(reason)
            out.append(board)
        return out

    # ---------- 告警 ----------

    def _build_alerts(self, scores: dict[str, BoardScore], target: str,
                      close_map: dict[str, float],
                      *, history: list[dict]) -> list[AlertSignal]:
        """按需求 3.6 的规则生成告警（含确认与冷却）。"""
        alerts: list[AlertSignal] = []
        for board in sorted(scores.values(), key=lambda row: -row.total):
            level, reasons, dims = self._decide_level(board, target)
            board.level = level
            board.reasons = reasons
            if level is SignalLevel.NONE:
                continue
            signal = AlertSignal(
                board_code=board.code, board_name=board.name,
                trade_date=target, level=level, score=board.total,
                kind=board.kind, six_dim_score=board.six_dim.score,
                accumulation_score=board.accumulation.score,
                leader_score=board.leader.score, gate_bonus=board.gate_bonus,
                promoted=board.promoted,
                triggered_dims=dims, resonance=board.resonance,
                reasons=list(reasons), change_pct=board.change_pct,
                entry_close=close_map.get(board.code))
            self._mark_confirmation(signal, target, history)
            alerts.append(signal)
        return self._apply_cooldown(alerts, target, history) if alerts else []

    def _apply_breakout(self, scores: dict[str, BoardScore],
                        target: str) -> None:
        """给每个板块算"是否越过自己的长期震荡上沿"（严格 PIT）。

        取该板块在 `target` **之前** `breakout_lookback_days` 个交易日的
        `total`，求 `breakout_quantile` 分位当上沿；当日 `total` 高于它、
        且不低于 `breakout_min_score` 时置 `breakout=True`。

        历史不足（`breakout_min_samples`）时不判 —— 刚上市/新入库的板块
        分位数毫无意义，宁可漏报也不要乱报。
        """
        cfg = self.config.alert
        if not cfg.breakout_enabled:
            return
        # ⚠️ 读**结果库**（`self.repo`），不是 `self.store`（那是 cache 库，
        # 没有 `mainline_score` 表）。第一版放错了对象，异常被吞成"历史为空"，
        # 突破触发静默失效 —— 所以这里把异常也明确记进 `gaps`。
        if self.repo is None:
            return
        try:
            history = self.repo.score_total_history_sync(
                target, lookback_days=int(cfg.breakout_lookback_days))
        except Exception as exc:  # noqa: BLE001 读不到历史不该打断打分
            logger.info("突破触发：读历史失败 %s", brief(exc, BRIEF_TIGHT))
            return
        if not history:
            return
        for code, board in scores.items():
            if not board.candidate:
                # 池外板块的 `total` 只由第一层构成，量纲不可比，
                # 不参与突破判定（与 `_decide_level` 的闸门一致）
                continue
            ceiling = breakout_ceiling(
                history.get(code, []), quantile=float(cfg.breakout_quantile),
                min_samples=int(cfg.breakout_min_samples))
            board.breakout_ceiling = ceiling
            # `recent` 用**当日之前**的尾部若干天：用来判断"是刚越过去
            # 还是一直在上面"（趋势 vs 突破）
            board.breakout = is_breakout(
                board.total, ceiling,
                min_score=float(cfg.breakout_min_score),
                recent=history.get(code, []),
                fresh_days=int(cfg.breakout_fresh_days))

    def _decide_level(self, board: BoardScore, target: str = ""
                      ) -> tuple[SignalLevel, list[str], list[str]]:
        """在基础判定之上套一层**告警范围限制**（`mainline_alert_exclusions.yaml`）。

        为什么用"包装"而不是把限制塞进 `_decide_level_base` 的分支里：
        基础判定有 4 个出口（强/中/弱/无），把范围检查插进每个出口既啰嗦、
        又容易漏掉一个 —— 而"漏掉一个出口"的症状是**某个档位绕过了限制**，
        很难发现。包一层则只有一个地方要维护。

        限制的语义与分数无关（"这个板块现在不该被监控"），所以：
        - `block`：直接 NONE，理由进 `reasons`；
        - `strong_only` / `medium_up`：基础判定算出档位后，由
          `scope.permits()` 决定放不放行。

        ⚠️ 这里**不自己比对模式字符串** —— 判定收敛在
        `AlertScopeConfig.permits()` 一处，否则每加一个模式就要在
        5 个调用点各改一遍（`service` / `apply_alert_scope` /
        `alert_scope_effect` / `board_false_positive_report`），
        漏一处就是**静默不一致**：重打分与后处理给出不同的表且不报错。
        """
        level, reasons, dims = self._decide_level_base(board)
        scope = getattr(self.config, "alert_scope", None)
        if scope is None or not target:
            return level, reasons, dims
        mode, why = scope.restriction(board.code, target)
        if mode == "block":
            return SignalLevel.NONE, [*reasons, why], dims
        if not scope.permits(board.code, target, level.value,
                             getattr(board, "total", None)):
            return SignalLevel.NONE, [*reasons, f"{why}（本次 {level.value} / "
                                                 f"{getattr(board, 'total', 0):.1f} 分，"
                                                 "未达该模式要求的档位或分数）"], dims
        return level, reasons, dims

    def _decide_level_base(self, board: BoardScore
                           ) -> tuple[SignalLevel, list[str], list[str]]:
        """判定告警等级（需求 3.6 的三档）。

        - 🔴 强信号：最终评分 ≥ `strong_score`，**且至少 2 个维度**得分超过
          各自阈值的 70%；
        - 🟡 中信号：最终评分 ≥ `medium_score`，且龙头共振触发；
        - 🟢 弱信号：单一维度极端异常（进观察列表）。

        维度达标用 `BoardScore.dims_above`（按**各自阈值**而不是统一 60 分）：
        否则资金流维度几乎全达标、宏观几乎都不达标，"至少 2 个维度"会退化成
        "资金流达标即可"。

        告警**范围**限制不在这里，见 `_decide_level`。
        """
        cfg = self.config.alert
        reasons: list[str] = list(board.reasons)
        dims = board.dims_above(cfg.strong_dim_ratio, cfg.dim_thresholds,
                                cfg.default_dim_threshold)

        # 提名板块（未经第二层）单独判定，且**只比中信号线**，不参与强信号判定。
        if board.promoted:
            if board.total >= cfg.medium_score:
                reasons.append(
                    f"最终评分 {board.total:.1f} ≥ {cfg.medium_score:g}"
                    "（含龙头共振门控；未经第二层，分数不与精选板块直接可比）")
                return SignalLevel.MEDIUM, reasons, dims
            reasons.append(f"最终评分 {board.total:.1f}，进入观察列表")
            return SignalLevel.WEAK, reasons, dims

        # ⚠️ 候选池外的板块**只能**走提名通道，不能走普通档位判定。
        #
        # **为什么这条限制与量纲无关**（V2.3 起）：`layer_weights` 改成 100/0
        # 之后，池内与池外的 `base_total` 都等于六维层分，两边的量纲其实已经
        # 一致了。这条闸门保留下来是**漏斗策略**而不是量纲要求：候选池是漏斗
        # 第一层选出的前 N 个，"池外还能出告警"等于绕开漏斗直接广播。
        # 收窄之后，池外板块要进告警只有一条路：龙头共振足够强（`_nominate`），
        # 且会被明确标注 `promoted=True`。
        if not board.candidate:
            reasons.append(f"第一层排 {board.rank}，未进候选池")
            return SignalLevel.NONE, reasons, dims

        if board.total >= cfg.strong_score and len(dims) >= cfg.strong_min_dims:
            reasons.append(f"最终评分 {board.total:.1f} ≥ {cfg.strong_score:g}，"
                           f"且 {len(dims)} 个维度达标")
            if board.gate_bonus:
                reasons.append(f"龙头共振加分 +{board.gate_bonus:.1f}")
            return SignalLevel.STRONG, reasons, dims
        if board.total >= cfg.medium_score:
            if board.resonance or not cfg.medium_requires_resonance:
                reasons.append(f"最终评分 {board.total:.1f} ≥ "
                               f"{cfg.medium_score:g}，且龙头共振触发")
                return SignalLevel.MEDIUM, reasons, dims
            reasons.append("评分达中信号线但龙头共振未触发")
        # 🆕 突破触发：**没到绝对线**，但越过了它自己的长期震荡上沿。
        #
        # 为什么需要它：绝对阈值是全市场统一的，"能不能报"于是取决于
        # 分数够不够极端。实测农业种植 885812 在 20260623 六维排名是
        # **全市场第 8**，`total` 62.3 却低于中信号线 → 不报；
        # 等它靠加分越线时行情已到中后段（用户报障的"顶部才报"）。
        #
        # 这条路径**不需要龙头共振**：共振是"龙头资金集中"的确认，
        # 与"板块刚脱离自己的震荡区间"是两件事，后者本身就是启动信号。
        elif board.breakout:
            reasons.append(
                f"最终评分 {board.total:.1f} 越过自身 "
                f"{cfg.breakout_lookback_days} 日震荡上沿 "
                f"{board.breakout_ceiling:.1f}（分位 "
                f"{cfg.breakout_quantile:.0%}），且 ≥ 下限 "
                f"{cfg.breakout_min_score:g}")
            return SignalLevel.MEDIUM, reasons, dims
        extreme = self._extreme_dim(board, cfg.weak_extreme_zscore)
        if extreme is not None:
            reasons.append(f"{extreme[0]}维度极端异常（等效 z={extreme[1]:.2f}）")
            return SignalLevel.WEAK, reasons, dims
        return SignalLevel.NONE, reasons, dims

    @staticmethod
    def _extreme_dim(board: BoardScore, threshold: float
                     ) -> tuple[str, float] | None:
        """找一个"极端异常"的维度（弱信号判定）。

        原始因子值是元 / 比率，量纲不可比，因此看它的**横截面分位**离 50 分
        有多远，再把分位差折成等效 z（25 分 ≈ 1σ）。直接对原始值判 z 需要把
        全市场取值再传进来，代价大于收益。
        """
        best: tuple[str, float] | None = None
        for layer in (board.six_dim, board.accumulation):
            for dim in layer.dimensions:
                if not dim.available:
                    continue
                equivalent = abs(dim.score - 50.0) / 25.0
                if equivalent >= threshold and (best is None
                                                or equivalent > best[1]):
                    best = (dim.label, equivalent)
        return best

    def _mark_confirmation(self, signal: AlertSignal, target: str,
                           history: Sequence[dict]) -> None:
        """「连续 N 个信号周期内触发」确认（需求 8.6：区分试盘与真启动）。

        只看 `target` **之前**的记录（严格小于），当天自己的告警不算确认。
        """
        periods = max(1, int(self.config.alert.confirm_periods))
        previous = [row for row in history
                    if str(row.get("board_code") or "") == signal.board_code
                    and str(row.get("trade_date") or "") < target]
        if not previous:
            return
        previous.sort(key=lambda row: str(row.get("trade_date") or ""),
                      reverse=True)
        signal.confirmed = True
        signal.confirmed_date = str(previous[0].get("trade_date") or "")
        signal.reasons = list(signal.reasons) + [
            f"{signal.confirmed_date} 已触发过一次，本次为确认"
            f"（{periods} 个信号周期内）"]

    def _apply_cooldown(self, alerts: list[AlertSignal], target: str,
                        history: Sequence[dict]) -> list[AlertSignal]:
        """同一板块同等级告警冷却（**按交易日**，见模块文档）。"""
        cooldown = max(0, int(self.config.alert.cooldown_days))
        if cooldown <= 0 or not history:
            return alerts
        store = self.store
        days = store.calendar("", target)[-cooldown - 1:] if store else []
        since = days[0] if days else _shift(target, -cooldown * 2)
        seen = {(str(row.get("board_code") or ""), str(row.get("level") or ""))
                for row in history
                if since <= str(row.get("trade_date") or "") < target}
        kept: list[AlertSignal] = []
        for signal in alerts:
            if (signal.board_code, signal.level.value) in seen:
                signal.reasons = list(signal.reasons) + [
                    f"{cooldown} 个交易日内已告警过，本次不重复推送"]
                signal.push_note = "冷却期内"
                continue
            kept.append(signal)
        return kept

    # ---------- 跟踪与快照辅助 ----------

    def _track(self, selected: Sequence[BoardScore],
               inputs: Sequence[BoardInput]) -> list[dict[str, Any]]:
        """重点板块跟踪：主力资金累计净流入 / 龙头股 / 共振比例。"""
        by_code = {item.code: item for item in inputs}
        out: list[dict[str, Any]] = []
        for board in selected[:10]:
            item = by_code.get(board.code)
            if item is None:
                continue
            row: dict[str, Any] = {
                "code": board.code, "name": board.name,
                "kind": board.kind.value,
                "total": round(board.total, 2),
                "six_dim": round(board.six_dim.score, 2),
                "accumulation": round(board.accumulation.score, 2),
                "gate_bonus": round(board.gate_bonus, 2),
                "etf_bonus": round(board.etf_bonus, 2),
                "etf_level": board.etf_level,
                "level": board.level.value, "profile": board.profile_key,
                "decay_days": board.decay_days,
                "resonance": board.resonance,
                "resonance_ratio": board.resonance_ratio,
                "leaders": [leader.to_dict() for leader in board.leaders],
                "seat_note": board.seat_note,
            }
            nets = ([net for _, net in item.flow.points if net is not None]
                    if item.flow is not None and item.flow.points else [])
            row["flow_5d"] = round(sum(nets[-5:]), 2) if nets else None
            row["flow_20d"] = round(sum(nets[-20:]), 2) if nets else None
            out.append(row)
        return out

    def _pool_summary(self, acc: dict[str, Any]) -> dict[str, Any]:
        """第二层各维度在候选池内的分布（回测产出的 IC/ICIR 在报告里）。"""
        out: dict[str, Any] = {}
        for _code, (layer, _raw) in acc.items():
            for dim in layer.dimensions:
                slot = out.setdefault(dim.key, {"label": dim.label, "scores": [],
                                                "available": 0})
                if dim.available:
                    slot["scores"].append(dim.score)
                    slot["available"] += 1
        for slot in out.values():
            values = slot.pop("scores", [])
            slot["mean"] = round(sum(values) / len(values), 2) if values else None
            slot["min"] = round(min(values), 2) if values else None
            slot["max"] = round(max(values), 2) if values else None
        return out

    def _latest_trade_date(self) -> str:
        """本地仓里最新的有板块指数的交易日。"""
        store = self.store
        if store is None:
            return ""
        rows = store._read(  # noqa: SLF001 单值只读，不值得为它加公开方法
            "SELECT MAX(trade_date) AS d FROM ml_board_bar")
        value = str(rows[0]["d"] or "") if rows else ""
        if value:
            return value
        days = store.calendar("", "")
        return days[-1] if days else ""

    def data_watermark(self) -> str:
        """**数据水位线**：底层板块指数日线的最新交易日（`MAX(trade_date)`，走索引，0 ms）。

        用途是当缓存键。评分是 `(本地数据, 目标交易日)` 的纯函数，而这个值变化
        恰好等价于"本地数据变了" —— 所以：

            水位线没变 → 算多少遍结果都一样 → 缓存永远该命中
            水位线变了 → 立刻失效，不需要等 TTL

        与 `_latest_trade_date()` 是同一个查询，单独起一个公开名是为了让
        "缓存为什么失效"这件事在调用处读得出来（见 `src/mainline/warm.py`）。
        """
        return self._latest_trade_date()

    def _refresh_hint(self, target: str) -> str:
        store = self.store
        if store is None:
            return ""
        if not store.sync_status():
            return "本地仓还没有同步记录，请先执行一次数据同步"
        latest = self._latest_trade_date()
        if latest and latest < target:
            return f"本地板块指数最新到 {latest}，落后于所算日期 {target}"
        return ""

    def _empty_snapshot(self, message: str) -> MainlineSnapshot:
        state, label = session_label()
        return MainlineSnapshot(
            generated_at=_now(), session_state=state, session_label=label,
            gaps=[message], disclaimer=self.config.disclaimer)


def warehouse_names(store: MainlineDataStore,
                    codes: Sequence[str]) -> dict[str, str]:
    """批量取股票名（股池裁剪要用它判 ST；取不到就返回空 dict）。"""
    warehouse = getattr(store, "warehouse", None)
    if warehouse is None or not codes:
        return {}
    try:
        return warehouse.stock_names(list(codes))
    except Exception as exc:  # noqa: BLE001 取不到名字只影响 ST 过滤，不该中断打分
        logger.info("股票名读取失败（跳过 ST 过滤）：%s", brief(exc, BRIEF_TIGHT))
        return {}


def _flow_circ_mv(store: MainlineDataStore, board_code: str,
                  trade_date: str) -> float:
    """从 `ml_board_flow` 取板块流通市值（成分股市值缺失时的兜底）。"""
    rows = store._read(  # noqa: SLF001 单值只读
        "SELECT circ_mv FROM ml_board_flow WHERE board_code = ?"
        " AND trade_date <= ? AND circ_mv IS NOT NULL"
        " ORDER BY trade_date DESC LIMIT 1", (board_code, trade_date))
    return to_float(rows[0]["circ_mv"]) if rows and rows[0]["circ_mv"] else 0.0


def build_mainline_service(config: MainlineConfig | None = None,
                           store: MainlineDataStore | None = None,
                           repo: Any = None) -> MainlineService:
    """组装服务（API 运行时用；测试可注入假的 store / repo）。"""
    return MainlineService(config=config or load_config(), store=store, repo=repo)


__all__ = [
    "MainlineService",
    "build_mainline_service",
    "session_label",
]
