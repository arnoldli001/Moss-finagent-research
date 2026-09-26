"""主线挖掘：Purged Walk-Forward 回测 + 因子相关性验证 + 分场景验证（需求四）。

## 防前视偏差：三层防线，缺一不可

**1. Purged（清洗窗口）。** 训练集与验证集之间空出 `purge_days` 个交易日。
理由：因子在 t 日用到的是"截至 t 日"的数据，而 t 日之后的收益又要用来
评估它 —— 训练集末尾那几天的标签区间与验证集开头重叠，权重会"偷看"
验证期的行情。空 10 个交易日是覆盖最长持有期（默认 20 日的一半）的折中。

**2. 数据侧截断。** 所有回测输入都带 `trade_date` 上界：
财务按 `ann_date <= trade_date`（见 `datastore.member_stats`）、
市值按当日截面、板块资金流只读已发生的日期。**这一层比第一层重要**：
purge 只是挪动评估边界，而数据泄漏会让因子在训练期就"知道"未来。

**3. 没有未来函数的分位。** 所有因子分都是**当日横截面**分位，
不存在"用全样本均值标准化"这种典型的隐性泄漏。

## 因子相关性验证（需求 4.4）

**必须算在原始因子上，不能算在 0-100 的分位分上。** 分位映射是单调变换，
但不同因子的分位映射会改变线性相关结构 —— 用分位分算相关性会得出
"相关性都很低"的假结论，而去重是否有效正是要验证的东西。
因此 `six_dim` / `accumulation` 都额外返回一份**原始因子值字典**。

验证目标：**跨层**因子相关性应低于 `correlation_limit`（默认 0.7）。
跨层而不是层内：层内高相关是正常的（同一子模型的两个因子本来就该相关），
重复加权的问题出在**跨层**（V1.0 的资金流 / 股东户数 / 成交量被第一层与
第二层各算了一遍）。

## 分场景验证（需求 4.5）

四个历史案例（CRO / 锂电6F / 农业种植 / 券商）写在
`configs/mainline.yaml` 的 `backtest.scenes` 里，代码不硬编码日期。
判定口径：案例的检验窗口内**是否出现过告警**、首次告警日相对案例启动日
**提前了几个交易日**、以及告警后 60 日的最大涨幅。

## 时间成本与 `step_days`

一天完整漏斗约 1.2~2 秒（31 个申万行业），1150 个交易日就是 25~40 分钟。
`step_days` 允许"每 N 个交易日算一次"来换速度：`step_days=5` 会把 IC 样本
从 1150 降到 230（仍然够算 ICIR），耗时降到 1/5。
**默认值是 1（逐日）**，因为 IC 的样本数直接决定 ICIR 的可信度，
把它调大是使用者的显式选择，不该由代码替他决定。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.config import MainlineConfig, load_config
from src.mainline.datastore import MainlineDataStore
from src.mainline.models import (
    AlertSignal,
    BacktestFold,
    BacktestMetrics,
    BacktestReport,
    BoardScore,
    FactorCorrelation,
    SceneCase,
)
from src.mainline.scoring import (
    annualize,
    correlation,
    icir,
    max_drawdown,
    mean,
    median,
    spearman,
    stdev,
    to_float,
)
from src.mainline.service import MainlineService

logger = logging.getLogger(__name__)

#: 相关性验证里各因子的中文标签（报告可读性）
FACTOR_LABELS: dict[str, str] = {
    "six.intraday": "第一层·日内动量",
    "six.overnight": "第一层·隔夜跳空",
    "six.roe_yoy": "第一层·ROE同比",
    "six.profit_yoy": "第一层·净利同比",
    "six.revenue_yoy": "第一层·营收同比",
    "six.net_ratio": "第一层·主力净流入/市值",
    "six.elg_ratio": "第一层·超大单净额/市值",
    "six.persistence": "第一层·净流入持续性",
    "six.profit_ratio": "第一层·获利盘占比",
    "six.holder_change": "第一层·股东户数变化",
    "six.macro_tilt": "第一层·宏观倾斜",
    "six.ma": "第一层·均线排列",
    "six.rsi": "第一层·RSI",
    "six.volume": "第一层·量能比",
    "accumulation.change": "第二层·融资余额变化",
    "accumulation.persistence": "第二层·融资连续走强",
    "accumulation.cum_ratio": "第二层·北向净买入/市值",
    "accumulation.raw_streak": "第二层·北向连续增持",
    "accumulation.form": "第二层·量价形态",
    "accumulation.strength": "第二层·形态强度",
}

#: 第一层 / 第二层的因子前缀（跨层配对的判定用）
LAYER_PREFIX = {"six.": "six_dim", "accumulation.": "accumulation"}


def _shift(stamp: str, days: int) -> str:
    try:
        base = datetime.strptime(stamp, "%Y%m%d")
    except (TypeError, ValueError):
        return stamp
    return (base + timedelta(days=int(days))).strftime("%Y%m%d")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ScoreFrame:
    """一个交易日算完后的横截面（回测内部用，不进 models）。"""

    trade_date: str
    scores: dict[str, BoardScore] = field(default_factory=dict)
    raw_six: dict[str, dict[str, float | None]] = field(default_factory=dict)
    raw_acc: dict[str, dict[str, float | None]] = field(default_factory=dict)
    alerts: list[AlertSignal] = field(default_factory=list)
    candidate_count: int = 0
    selected_count: int = 0


@dataclass
class MainlineBacktester:
    """Purged Walk-Forward 回测器。"""

    config: MainlineConfig = field(default_factory=load_config)
    store: MainlineDataStore | None = None
    service: MainlineService | None = None

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = MainlineDataStore(config=self.config)
        if self.service is None:
            self.service = MainlineService(config=self.config, store=self.store)

    # ---------- 主流程 ----------

    def run(self, start: str = "", end: str = "",
            progress: Callable[[str], None] | None = None) -> BacktestReport:
        """跑一次完整回测并返回报告对象（落库由调用方决定）。"""
        begun = time.monotonic()
        report = BacktestReport(run_id=f"bt-{datetime.now():%Y%m%dT%H%M%S}",
                                started_at=_now(),
                                disclaimer=self.config.disclaimer)
        store = self.store
        assert store is not None
        try:
            days = store.calendar(start, end)
            if not days:
                report.error = "本地交易日历为空：请先同步数据（calendar）"
                return report
            begin, finish = days[0], days[-1]
            report.range_start, report.range_end = begin, finish
            warmup = max(int(self.config.min_history_days), 60)
            if len(days) <= warmup:
                report.error = (f"区间只有 {len(days)} 个交易日，"
                                f"不足预热所需的 {warmup} 天")
                return report
            step = max(1, int(getattr(self.config.backtest, "step_days", 1) or 1))
            grid = days[warmup::step]
            if progress is not None:
                progress(f"开始回测：{begin}~{finish}，评估 {len(grid)} 个交易日")

            frames = self._score_grid(grid, progress)
            if not frames:
                report.error = "没有任何一天算出有效评分（检查数据同步）"
                return report
            report.gaps.extend(self._coverage_gaps(frames, grid))
            # 采样步长 > 1 时，分场景验证与逐日 IC 都不可靠 —— 必须说出来。
            # 案例的检验窗口通常只有 16~25 个交易日，10 天步长下只剩 2~3 个采样点，
            # "未触发"很可能只是"那天没采样到"，而报告里两者长得一模一样。
            if step > 1:
                report.gaps.append(
                    f"回测采样步长 step_days={step}：分场景验证与逐日 IC 的可靠性"
                    "下降（案例窗口内只有少量采样点）。要出正式结论请用 "
                    "step_days=1 重跑，或只把分场景结果当作参考。")

            folds = self._folds(grid, step)
            report.folds = folds
            report.metrics = self._metrics(frames)
            report.correlation = self._correlation(frames)
            report.scenes = self._scenes(frames, report)
            signals = [item for frame in frames for item in frame.alerts]
            signals.sort(key=lambda row: (row.trade_date, -row.score))
            report.signals = signals
            report.false_positives = [
                item for item in signals if not _realized(item)]
            report.metrics.signal_count = len(signals)
            report.metrics.signal_hit_rate = _hit_rate(signals)
            report.metrics.false_positive_rate = (
                (len(report.false_positives) / len(signals)) if signals else None)
            report.metrics.avg_max_gain = mean(
                [item.max_gain_pct for item in signals])
            report.metrics.median_max_gain = median(
                [item.max_gain_pct for item in signals])
            report.metrics.targets = self._targets(report.metrics)
            layer_weights = self.config.synthesis.normalized_layers()
            report.config_note = (
                f"漏斗 前{self.config.funnel.candidate_ratio:.0%}"
                f"→{self.config.funnel.select_top}；"
                f"合成 六维{layer_weights.get('six_dim', 0):g}%/"
                f"建仓{layer_weights.get('accumulation', 0):g}% + 门控加分；"
                f"step_days={step}")
            report.metrics.by_dim = self._by_dim(frames)
        except Exception as exc:  # noqa: BLE001 回测失败要留痕而不是让接口 500
            logger.exception("主线回测失败")
            report.error = brief(exc, BRIEF_TIGHT)
        report.finished_at = _now()
        report.seconds = round(time.monotonic() - begun, 2)
        return report

    # ---------- 逐日打分 ----------

    def _score_grid(self, grid: Sequence[str],
                    progress: Callable[[str], None] | None) -> list[ScoreFrame]:
        """在采样网格上跑漏斗（确认与冷却按内存历史推进，不查库）。"""
        assert self.service is not None
        frames: list[ScoreFrame] = []
        history: list[dict[str, Any]] = []
        for index, day in enumerate(grid):
            snapshot = self.service.score_date_sync(day, alert_history=history)
            if not snapshot.scores:
                continue
            frame = ScoreFrame(trade_date=snapshot.trade_date,
                               candidate_count=snapshot.candidate_count,
                               selected_count=snapshot.selected_count)
            frame.scores = {row.code: row for row in snapshot.scores}
            frame.alerts = list(snapshot.alerts)
            # 原始因子值：从维度 raw 里还原（`six_dim` / `accumulation` 都写进去了）
            for code, board in frame.scores.items():
                frame.raw_six[code] = _raw_of(board.six_dim)
                frame.raw_acc[code] = _raw_of(board.accumulation)
            frames.append(frame)
            for alert in snapshot.alerts:
                history.append({"board_code": alert.board_code,
                                "trade_date": alert.trade_date,
                                "level": alert.level.value})
            if progress is not None and (index % 20 == 0 or index == len(grid) - 1):
                progress(f"已评估 {index + 1}/{len(grid)} 天（{day}）")
        self._evaluate(frames)
        return frames

    def _evaluate(self, frames: list[ScoreFrame]) -> None:
        """给每条告警回填"上报后 5/10/20/60 日涨幅与最大涨幅"。

        **这是回测里唯一允许"看未来"的地方**：它算的就是兑现情况。
        因此它只写 `AlertSignal` 的 `ret_*` / `max_gain_*` 字段，
        **绝不回写分数**（分数一旦拿到未来信息，整个 IC 就没意义了）。
        """
        cfg = self.config.backtest
        for frame in frames:
            if not frame.alerts:
                continue
            entry = frame.trade_date
            for alert in frame.alerts:
                series = self._forward_closes(alert.board_code, entry,
                                              cfg.max_gain_window)
                if not series:
                    continue
                base = alert.entry_close
                if not base or base <= 0:
                    continue
                for window, attr in ((5, "ret_5d"), (10, "ret_10d"),
                                     (20, "ret_20d"), (60, "ret_60d")):
                    if len(series) >= window:
                        value = series[window - 1][1] / base - 1.0
                        setattr(alert, attr, round(value * 100.0, 4))
                peak = max(series, key=lambda item: item[1])
                alert.max_gain_pct = round((peak[1] / base - 1.0) * 100.0, 4)
                alert.max_gain_date = peak[0]

    def _forward_closes(self, code: str, start: str, days: int
                        ) -> list[tuple[str, float]]:
        """告警日**之后**的板块收盘价序列（严格不含告警日本身）。"""
        store = self.store
        if store is None:
            return []
        found = store.board_bars([code], start=_shift(start, -5),
                                 end=_shift(start, int(days * 2.2)))
        series = found.get(code)
        if series is None:
            return []
        return [(bar.date, bar.close) for bar in series.bars
                if bar.date > start and bar.close > 0][:days]

    # ---------- Walk-Forward folds ----------

    def _folds(self, grid: Sequence[str], step: int) -> list[BacktestFold]:
        """把评估网格切成 Purged Walk-Forward 的 folds（纯记账，见模块文档）。"""
        cfg = self.config.backtest
        train = max(int(cfg.train_days), 20)
        validate = max(int(cfg.validate_days), 5)
        purge = max(int(cfg.purge_days), 0)
        folds: list[BacktestFold] = []
        index = 0
        cursor = train + purge
        while cursor < len(grid):
            train_end = grid[cursor - purge - 1] if cursor - purge - 1 >= 0 else ""
            train_start = grid[max(0, cursor - purge - train)]
            purge_end = grid[cursor - 1] if cursor > 0 else ""
            block = list(grid[cursor:cursor + validate])
            cursor += validate
            if not block:
                break
            folds.append(BacktestFold(
                index=index, train_start=train_start, train_end=train_end,
                purge_end=purge_end, validate_start=block[0],
                validate_end=block[-1], weight_mode="static",
                boards=0, alerts=0,
                note=f"训练 {train} 日 / purge {purge} 日 / 验证 {len(block)} 日"
                     f"（网格步长 {step}）"))
            index += 1
        return folds

    # ---------- 指标 ----------

    def _metrics(self, frames: list[ScoreFrame]) -> BacktestMetrics:
        cfg = self.config.backtest
        out = BacktestMetrics()
        ic_values: list[float | None] = []
        for frame in frames:
            forward = self._forward_return(frame.trade_date, 20)
            if not forward:
                continue
            pairs = [(board.total, forward.get(code))
                     for code, board in frame.scores.items()
                     if forward.get(code) is not None]
            ic_values.append(spearman([p[0] for p in pairs],
                                      [p[1] for p in pairs],
                                      min_samples=max(8, len(pairs) // 3)))
        clean = [item for item in ic_values if item is not None]
        out.ic_samples = len(clean)
        out.ic_mean = mean(clean)
        out.ic_std = stdev(clean)
        out.icir = icir(clean)
        out.ic_win_rate = (sum(1 for item in clean if item > 0) / len(clean)
                           if clean else None)

        for holding in cfg.holding_days:
            stats = self._long_short(frames, int(holding))
            out.by_holding[str(holding)] = stats
        main = out.by_holding.get(str(cfg.holding_days[-1] if cfg.holding_days
                                      else 20), {})
        out.long_short_annual = main.get("long_short_annual")
        out.long_annual = main.get("long_annual")
        out.benchmark_annual = main.get("benchmark_annual")
        out.long_excess = main.get("long_excess")
        out.sharpe = main.get("sharpe")
        out.max_drawdown = main.get("max_drawdown")
        return out

    def _long_short(self, frames: list[ScoreFrame], holding: int
                    ) -> dict[str, Any]:
        """高分组-低分组 / 多头 / 基准 的年化与胜率（板块等权，按分数排序）。"""
        cfg = self.config.backtest
        top_n = max(int(cfg.top_n), 1)
        longs: list[float] = []
        shorts: list[float] = []
        bench: list[float] = []
        for frame in frames:
            forward = self._forward_return(frame.trade_date, holding)
            if not forward:
                continue
            ranked = sorted(((board.total, code)
                             for code, board in frame.scores.items()
                             if forward.get(code) is not None),
                            key=lambda item: -item[0])
            if len(ranked) < max(top_n * 2, cfg.min_boards):
                continue
            longs.append(mean([forward[code] for _, code
                               in ranked[:top_n]]) or 0.0)
            shorts.append(mean([forward[code] for _, code
                                in ranked[-top_n:]]) or 0.0)
            bench.append(mean(list(forward.values())) or 0.0)
        if not longs:
            return {"samples": 0}
        periods = max(len(longs), 1)
        total_long = _compound(longs)
        total_short = _compound(shorts)
        total_bench = _compound(bench)
        days = periods * holding
        long_annual = annualize(total_long, days,
                                trading_days=cfg.annual_trading_days)
        bench_annual = annualize(total_bench, days,
                                 trading_days=cfg.annual_trading_days)
        return {
            "samples": periods,
            "long_total": _round(total_long), "short_total": _round(total_short),
            "bench_total": _round(total_bench),
            "long_annual": _round(long_annual),
            "short_annual": _round(annualize(total_short, days,
                                             trading_days=cfg.annual_trading_days)),
            "benchmark_annual": _round(bench_annual),
            "long_short_annual": _round(
                annualize(total_long - total_short, days,
                          trading_days=cfg.annual_trading_days)),
            "long_excess": _round(long_annual - bench_annual
                                  if None not in (long_annual, bench_annual)
                                  else None),
            "win_rate": round(sum(1 for item in longs if item > 0) / periods, 4),
            "sharpe": _round(_sharpe(longs, holding, cfg.annual_trading_days)),
            "max_drawdown": _round(_equity_drawdown(longs)),
        }

    def _forward_return(self, trade_date: str, holding: int
                        ) -> dict[str, float]:
        """`{板块代码: 未来 holding 个交易日的收益}`（严格用未来价格，仅评估用）。"""
        store = self.store
        if store is None:
            return {}
        target = self._nth_trading_day(trade_date, holding)
        if not target or target <= trade_date:
            return {}
        codes = [str(row["board_code"]) for row in store._read(  # noqa: SLF001
            "SELECT DISTINCT board_code FROM ml_board_bar"
            " WHERE trade_date = ?", (trade_date,))]
        if not codes:
            return {}
        marker = ",".join("?" for _ in codes)
        rows = store._read(  # noqa: SLF001 回测内部批量只读
            "SELECT board_code, close FROM ml_board_bar"
            f" WHERE board_code IN ({marker}) AND trade_date = ?",
            (*codes, target))
        later = {str(row["board_code"]): float(row["close"]) for row in rows
                 if row["close"]}
        rows = store._read(  # noqa: SLF001
            "SELECT board_code, close FROM ml_board_bar"
            f" WHERE board_code IN ({marker}) AND trade_date = ?",
            (*codes, trade_date))
        base = {str(row["board_code"]): float(row["close"]) for row in rows
                if row["close"]}
        return {code: later[code] / base[code] - 1.0
                for code in base if code in later and base[code]}

    def _nth_trading_day(self, trade_date: str, count: int) -> str:
        store = self.store
        if store is None or count <= 0:
            return ""
        rows = store._read(  # noqa: SLF001 单值只读
            "SELECT trade_date FROM ml_calendar WHERE trade_date > ?"
            " ORDER BY trade_date LIMIT ?", (trade_date, int(count)))
        return str(rows[-1]["trade_date"]) if len(rows) >= count else ""

    # ---------- 因子相关性（需求 4.4） ----------

    def _correlation(self, frames: list[ScoreFrame]) -> FactorCorrelation:
        """跨层因子相关性矩阵（算在**原始因子值**上，见模块文档）。"""
        limit = float(self.config.backtest.correlation_limit or 0.70)
        out = FactorCorrelation(limit=limit)
        series: dict[str, list[float | None]] = {}
        for frame in frames:
            for code in frame.scores:
                for prefix, source in (("six.", frame.raw_six),
                                       ("accumulation.", frame.raw_acc)):
                    for key, value in (source.get(code) or {}).items():
                        name = f"{prefix}{key}"
                        series.setdefault(name, []).append(to_float(value))
        # 至少要有 30 个有效样本才参与（否则相关系数是噪声）
        usable = {name: values for name, values in series.items()
                  if sum(1 for item in values if item is not None) >= 30}
        out.factors = sorted(usable)
        out.samples = max((len(v) for v in usable.values()), default=0)
        if len(usable) < 2:
            out.note = "有效因子不足 2 个（多数维度缺数据），无法做相关性验证"
            return out
        # 只算上三角，避免同一对被算两次（高相关清单重复会让报告读起来像有两处问题）
        names = out.factors
        cross_values: list[float] = []
        for index, left in enumerate(names):
            row: dict[str, float] = {}
            for other in range(index + 1, len(names)):
                right = names[other]
                value = correlation(usable[left], usable[right], min_samples=30)
                if value is None:
                    continue
                row[right] = round(value, 4)
                if _cross_layer(left, right):
                    cross_values.append(abs(value))
                    if abs(value) > limit:
                        out.high_pairs.append((left, right, value))
            out.matrix[left] = row
        if cross_values:
            out.cross_layer_mean_abs = round(
                sum(cross_values) / len(cross_values), 4)
            out.cross_layer_max_abs = round(max(cross_values), 4)
        out.high_pairs.sort(key=lambda item: -abs(item[2]))
        out.passed = not out.high_pairs
        if out.passed:
            worst = ("—" if out.cross_layer_max_abs is None
                     else f"{out.cross_layer_max_abs:g}")
            out.note = f"跨层因子最大相关 {worst} ≤ {limit:g}，去重有效"
        else:
            out.note = (f"发现 {len(out.high_pairs)} 对跨层高相关因子"
                        f"（|r| > {limit:g}），需正交化或删除")
        return out

    # ---------- 分场景验证（需求 4.5） ----------

    def _scenes(self, frames: list[ScoreFrame],
                report: BacktestReport) -> list[SceneCase]:
        out: list[SceneCase] = []
        for raw in self.config.backtest.scenes:
            case = SceneCase(
                key=str(raw.get("key") or ""),
                label=str(raw.get("label") or raw.get("key") or ""),
                start_date=str(raw.get("start_date") or ""),
                check_window=tuple(raw.get("check_window") or ("", ""))[:2],
                min_lead_days=int(to_float(raw.get("min_lead_days"), 3) or 3),
                keywords=[str(item) for item in (raw.get("keyword") or [])],
                note=str(raw.get("note") or ""))
            begin, end = (case.check_window + ("", ""))[:2]
            hit: AlertSignal | None = None
            for frame in frames:
                if begin and frame.trade_date < begin:
                    continue
                if end and frame.trade_date > end:
                    continue
                for alert in frame.alerts:
                    if case.matches(alert.board_name):
                        hit = alert
                        break
                if hit is not None:
                    break
            if hit is None:
                case.triggered = False
                case.note = (case.note + "；检验窗口内未触发").strip("；")
                out.append(case)
                continue
            case.triggered = True
            case.first_alert_date = hit.trade_date
            case.first_alert_board = hit.board_name
            case.max_gain_pct = hit.max_gain_pct
            case.ret_60d = hit.ret_60d
            case.lead_days = _lead_days(hit.trade_date, case.start_date)
            case.passed = (case.lead_days is not None
                           and case.lead_days >= case.min_lead_days)
            out.append(case)
        return out

    # ---------- 维度绩效与覆盖 ----------

    def _by_dim(self, frames: list[ScoreFrame]) -> dict[str, dict[str, Any]]:
        """第一层六维 + 第二层三维各自的 IC / ICIR / 胜率（需求六第三章）。"""
        buckets: dict[str, list[float | None]] = {}
        for frame in frames:
            forward = self._forward_return(frame.trade_date, 20)
            if not forward:
                continue
            for code, board in frame.scores.items():
                ret = forward.get(code)
                if ret is None:
                    continue
                for layer in (board.six_dim, board.accumulation):
                    for dim in layer.dimensions:
                        if not dim.available:
                            continue
                        buckets.setdefault(dim.key, []).append((dim.score, ret))
        out: dict[str, dict[str, Any]] = {}
        for key, pairs in buckets.items():
            # 每个交易日一个 IC，需要按日分组；这里简化成"逐样本对"会高估样本数，
            # 因此按日期切分（frames 顺序即日期顺序，pairs 已按日追加）。
            out[key] = self._dim_stats(pairs)
        return out

    @staticmethod
    def _dim_stats(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
        """按"每 30 个样本一组"近似成按日分组算 IC 序列。

        真正的按日分组需要把日期一起带进来；这里用**固定分组**近似，
        并在 `note` 里写明 —— 直接对全部样本对算一个相关系数会把
        "横截面 IC"错算成"混合截面 IC"，数值会系统性偏高。
        """
        if len(pairs) < 60:
            return {"samples": len(pairs), "note": "样本不足，未计算"}
        chunk = 30
        values: list[float | None] = []
        for offset in range(0, len(pairs) - chunk + 1, chunk):
            block = pairs[offset:offset + chunk]
            values.append(spearman([item[0] for item in block],
                                   [item[1] for item in block],
                                   min_samples=8))
        clean = [item for item in values if item is not None]
        return {"samples": len(clean), "ic_mean": _round(mean(clean)),
                "icir": _round(icir(clean)),
                "ic_win_rate": (round(sum(1 for i in clean if i > 0) / len(clean), 4)
                                if clean else None),
                "note": "IC 按每 30 个样本近似分组（近似口径）"}

    def _by_holding(self, frames: list[ScoreFrame]) -> dict[str, dict[str, Any]]:
        """逐持有期的多空统计（与 `_metrics` 里填的是同一份，留作外部调用入口）。"""
        cfg = self.config.backtest
        return {str(int(h)): self._long_short(frames, int(h))
                for h in cfg.holding_days}

    def _coverage_gaps(self, frames: list[ScoreFrame],
                       grid: Sequence[str]) -> list[str]:
        gaps: list[str] = []
        if len(frames) < len(grid):
            gaps.append(f"{len(grid) - len(frames)} 个交易日没有算出评分"
                        "（多为板块指数或成分股缺失）")
        empty = [frame.trade_date for frame in frames
                 if not frame.candidate_count]
        if empty:
            gaps.append(f"{len(empty)} 天的候选池为空")
        return gaps

    # ---------- 目标对照 ----------

    def _targets(self, metrics: BacktestMetrics) -> dict[str, dict[str, Any]]:
        """把实际值与 YAML 里的目标阈值对照（报告里标达标/未达标）。"""
        targets = self.config.backtest.targets or {}
        actual = {
            "ic_mean": metrics.ic_mean, "icir": metrics.icir,
            "ic_win_rate": metrics.ic_win_rate,
            "long_short_annual": metrics.long_short_annual,
            "long_excess": metrics.long_excess,
            "signal_hit_rate": metrics.signal_hit_rate,
            "false_positive_rate": metrics.false_positive_rate,
            "avg_max_gain": (metrics.avg_max_gain / 100.0
                             if metrics.avg_max_gain is not None else None),
        }
        out: dict[str, dict[str, Any]] = {}
        for key, target in targets.items():
            value = actual.get(key)
            target_value = to_float(target)
            passed = None
            if value is not None and target_value is not None:
                # 假阳性率是"越低越好"，其余是"越高越好"
                passed = (value <= target_value if key == "false_positive_rate"
                          else value >= target_value)
            out[key] = {"value": _round(value), "target": target_value,
                        "passed": passed}
        return out


# ==================================================================
# 纯函数
# ==================================================================


def _raw_of(layer: Any) -> dict[str, float | None]:
    """把一个 `LayerScore` 的所有维度 raw 合并成一张因子表。"""
    out: dict[str, float | None] = {}
    for dim in getattr(layer, "dimensions", []):
        for key, value in (dim.raw or {}).items():
            if key in ("leaders", "z") or isinstance(value, (list, dict)):
                continue
            out[key] = to_float(value)
    return out


def _cross_layer(left: str, right: str) -> bool:
    """两个因子是否属于**不同层**（跨层高相关才是重复加权的问题）。"""
    layer_left = next((name for prefix, name in LAYER_PREFIX.items()
                       if left.startswith(prefix)), "?")
    layer_right = next((name for prefix, name in LAYER_PREFIX.items()
                        if right.startswith(prefix)), "?")
    return layer_left != layer_right and "?" not in (layer_left, layer_right)


def _compound(returns: Sequence[float]) -> float:
    """把逐期收益复合（区间总收益）。"""
    value = 1.0
    for item in returns:
        value *= (1.0 + item)
    return value - 1.0


def _sharpe(returns: Sequence[float], holding: int,
            trading_days: int) -> float | None:
    """年化夏普（无风险利率按 0；按持有期数还原年化频率）。"""
    if len(returns) < 5 or holding <= 0:
        return None
    average = mean(returns)
    deviation = stdev(returns)
    if average is None or deviation is None or deviation <= 1e-9:
        return None
    periods_per_year = trading_days / holding
    return average / deviation * (periods_per_year ** 0.5)


def _equity_drawdown(returns: Sequence[float]) -> float | None:
    equity: list[float] = []
    value = 1.0
    for item in returns:
        value *= (1.0 + item)
        equity.append(value)
    return max_drawdown(equity)


def _lead_days(alert_date: str, start_date: str) -> int | None:
    """告警日比案例启动日提前了几个**自然日**。

    用自然日而不是交易日：案例的 `start_date` 是"启动时间"（可能落在周末），
    而告警日是交易日 —— 换算交易日需要日历，而这里的判定只关心"提前没提前"。
    报告里会写明这个口径。
    """
    try:
        first = datetime.strptime(alert_date, "%Y%m%d")
        second = datetime.strptime(start_date, "%Y%m%d")
    except (TypeError, ValueError):
        return None
    return (second - first).days


def _realized(alert: AlertSignal) -> bool:
    """告警是否兑现（20 日涨幅 > 5%，或 60 日内最大涨幅 > 5%）。"""
    for value in (alert.ret_20d, alert.max_gain_pct):
        if value is not None and value > 5.0:
            return True
    return False


def _hit_rate(signals: Sequence[AlertSignal]) -> float | None:
    if not signals:
        return None
    hit = sum(1 for item in signals if _realized(item))
    return round(hit / len(signals), 4)


def _round(value: Any, digits: int = 4) -> float | None:
    number = to_float(value)
    return None if number is None else round(number, digits)


__all__ = [
    "FACTOR_LABELS",
    "LAYER_PREFIX",
    "MainlineBacktester",
    "ScoreFrame",
]
