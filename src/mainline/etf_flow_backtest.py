"""ETF 份额监控：回测与绩效报告。

## 回测什么

对 2018-01 至今的每个交易日，用**截至当日**的 ETF 份额与指数收盘价生成信号，
再看信号触发后 T+5/10/20/34 个交易日的**指数收益**。

## 三个必须说清的计量口径

**1. 信号是 T+1 确认的。** 份额数据次日 8:30 才更新，所以"T 日信号"实际是在
T+1 早上才能看到。回测里用 **T 日收盘价**作为买入基准是**乐观**的 ——
真实可成交价是 T+1 的某个价格。本模块因此**同时给出两个口径**：

    t_close    以信号日收盘价为基准（乐观，与需求文档的 T+N 口径一致）
    t1_close   以次日收盘价为基准（保守，更接近"看到信号后第二天收盘买入"）

两个差距大就说明这个信号的收益主要来自"信号日当天已经涨完的部分"，
实盘拿不到 —— 这比只报一个漂亮数字诚实。

**2. 收益基准是标的指数，不是 ETF 净值。** 份额变化与 ETF 二级市场价格之间
还隔着折溢价，用指数收益衡量的是"信号对后市的判断力"，不含交易摩擦。

**3. 最大回撤按"信号后持有到窗口结束"的路径算**，不是买入后一直持有的回撤。
它回答的是"最坏情况下这笔要忍受多少浮亏"。

## 市场环境怎么分

按信号日**指数近 120 日收益**分类：> +15% 牛市 / < -15% 熊市 / 其余震荡。
用相对收益而不是绝对点位：同样是 3500 点，从 3000 涨上来和从 4000 跌下来
是完全不同的环境，而分位数窗口（34 日）太短、不足以刻画环境。

环境判定与门控规则都**复用 `etf_flow`**（`classify_regime` /
`apply_regime_policy`）—— 回测与实盘必须用同一套口径，否则回测出来的
门控效果跟实盘对不上，这个偏差是查不出来的。

## 为什么要单列「放行 / 被门控」两张表

机会信号在非熊市环境、风险信号在熊市，都会被 `evaluate` 降级为弱信号
（`gated=True`）。如果报告只按 `kind` 汇总，被降级的和被放行的会混在一起，
看不出门控到底改了什么。所以本模块额外给出：

    opportunity_live     机会信号中**环境放行且实际告警**的（强/中等级）
    opportunity_gated    机会信号中**被门控降级**的（实盘只进观察列表）
    risk_live / risk_gated   风险信号的同口径拆分

"放行"按**等级**判定而不是按 `gated` 标志：走"份额异动但指数不在极端区"
那条路径的信号本来就是弱信号（观察列表），它们 `gated` 也是 False 却从不告警，
算进 live 会把真正放行的极端区信号稀释掉（第一版分桶的 live 桶有 359 个样本，
而熊市实际只有 77 个）。

两桶的差异就是门控的价值。`kind × regime` 交叉表进一步说明每个环境
各自的贡献 —— 只看 `by_regime` 会把四种信号类型的表现混在一起，
这是最初误判"机会信号无效"的直接原因。
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.mainline.etf_flow import (
    KIND_INDUSTRY_REVERSAL,
    KIND_OPPORTUNITY,
    KIND_RISK,
    LEVEL_MEDIUM,
    LEVEL_STRONG,
    LEVEL_WEAK,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_LABELS,
    REGIME_RANGE,
    FlowConfig,
    FlowSignal,
    MarketRegime,
    apply_regime_policy,
    build_indicators,
    build_positions,
    classify_regime,
    evaluate,
    load_config,
)
from src.mainline.etf_flow_stats import (
    NEGATIVE_KINDS,
    independent_episodes,
    resolve_targets,
    target_hit,
    wilson_interval,
)

logger = logging.getLogger(__name__)

#: `alert_levels` 的别名：只有强/中等级的信号才会真正告警（弱信号只进观察列表）。
#: 提成模块级常量是因为它同时被主表分桶与样本外对照用到 —— 两处各写一遍
#: 一旦改动不一致，"放行"桶在两个地方的含义就会不同，而界面上看不出来。
ALERT_LEVELS = (LEVEL_STRONG, LEVEL_MEDIUM)

#: 市场环境分类的阈值（指数近 120 日收益）
REGIME_WINDOW = 120
REGIME_BULL_THRESHOLD, REGIME_BEAR_THRESHOLD = 0.15, -0.15


@dataclass
class SignalRecord:
    """一次信号触发 + 它的后续表现。"""

    signal: FlowSignal
    regime: str = "range"
    #: `{horizon: 收益率}`（以信号日收盘价为基准）
    forward: dict[int, float | None] = field(default_factory=dict)
    #: `{horizon: 收益率}`（以次日收盘价为基准，保守口径）
    forward_t1: dict[int, float | None] = field(default_factory=dict)
    #: 窗口内的最大浮盈 / 最大浮亏
    max_gain: float | None = None
    max_drawdown: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = self.signal.to_dict()
        data.update({
            "regime": self.regime,
            "regime_label": REGIME_LABELS.get(self.regime, self.regime),
            "forward": {str(k): v for k, v in self.forward.items()},
            "forward_t1": {str(k): v for k, v in self.forward_t1.items()},
            "max_gain": self.max_gain, "max_drawdown": self.max_drawdown})
        return data


@dataclass
class HorizonStats:
    """一个观察期的统计。"""

    horizon: int = 0
    samples: int = 0
    #: **前瞻窗口互不重叠**的独立时段个数（见 `etf_flow_stats`）。
    #: `samples` 是"多少条信号"，`independent` 是"几个独立事件" ——
    #: 两者差得越多，这个胜率越不可信。实测 `opportunity_live` 是 66 比 3。
    independent: int | None = None
    median: float | None = None
    mean: float | None = None
    win_rate: float | None = None
    #: 胜率的 Wilson 95% 置信区间。小样本下**必须**和点估计一起看：
    #: n=66 时 83.3% 的区间是 [72.6%, 90.4%]，看着还行；
    #: 换成真实有效样本 n=3 就完全没有统计意义。
    win_rate_low: float | None = None
    win_rate_high: float | None = None
    false_positive_rate: float | None = None
    median_t1: float | None = None
    win_rate_t1: float | None = None
    target_median: float | None = None
    target_win_rate: float | None = None
    #: 该组信号类型（`kind`）；混合类型的桶为 None（判达标时按做多口径）
    kind: str = ""
    passed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"horizon": self.horizon, "samples": self.samples,
                "independent": self.independent,
                "median": self.median, "mean": self.mean,
                "win_rate": self.win_rate,
                "win_rate_low": self.win_rate_low,
                "win_rate_high": self.win_rate_high,
                "false_positive_rate": self.false_positive_rate,
                "median_t1": self.median_t1, "win_rate_t1": self.win_rate_t1,
                "target_median": self.target_median,
                "target_win_rate": self.target_win_rate,
                "kind": self.kind,
                "passed": self.passed}


@dataclass
class BacktestReport:
    """ETF 份额信号回测报告。"""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    range_start: str = ""
    range_end: str = ""
    days: int = 0
    #: `{kind: {horizon: HorizonStats}}`
    by_kind: dict[str, dict[str, HorizonStats]] = field(default_factory=dict)
    #: `{etf_code: {horizon: HorizonStats}}`
    by_etf: dict[str, dict[str, HorizonStats]] = field(default_factory=dict)
    #: `{regime: {horizon: HorizonStats}}`
    by_regime: dict[str, dict[str, HorizonStats]] = field(default_factory=dict)
    #: `{"{kind}@{regime}": {horizon: HorizonStats}}` —— 只看 by_regime 会把
    #: 四种信号类型混在一起，正是最初误判"信号无效"的直接原因
    by_kind_regime: dict[str, dict[str, HorizonStats]] = field(
        default_factory=dict)
    #: 样本内 / 样本外切分点（`backtest.split`，`YYYYMMDD`）；空 = 不切分。
    #: 切分点**之前的信号用于看结论、之后的用于验证结论** ——
    #: 阈值是在全区间上肉眼调出来的，所以只有后半段才算真正的样本外。
    split: str = ""
    #: `{"{in|out}:{group}": {horizon: HorizonStats}}` —— 样本内 / 样本外对照。
    #: 只覆盖门控结论所依赖的那几组（见 `_validation_groups`），
    #: 不是把四张表整体再算一遍：`by_etf` 与等级维度在样本外只剩个位数样本，
    #: 铺开来看全是噪声，反而把真问题淹掉。
    validation: dict[str, dict[str, HorizonStats]] = field(default_factory=dict)
    records: list[SignalRecord] = field(default_factory=list)
    markdown: str = ""
    gaps: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self, *, record_limit: int = 300,
                collapse_runs: bool = False) -> dict[str, Any]:
        """序列化报告；`records` 只带**最近**的 `record_limit` 条，新的在前。

        ## 为什么是"最近 N 条"而不是"前 N 条"

        `records` 是按回测日历**从旧到新**逐步追加的（见 `run_backtest` 的
        `while cursor` 循环），所以 `records[:limit]` 拿到的是**最早**的那批 ——
        2018-01 起、约 300 条，正好把用户真正关心的近期信号全部挤掉。
        实测 20180102~20260918 共 1252 条，`[:300]` 的日期上限只到 2019 年附近，
        面板上就是"明细全是 2018 年的老数据"。

        取尾部 `[-limit:]` 才是"最近 N 条"。**并且倒序输出**（新的在前）：
        明细表默认只渲染前 200 行，若还把最旧的排在最前，那 200 行会截在
        "300 条里较早的一半"，等于又丢掉一次近期数据。

        `record_count` 仍是**全样本总数**，不受本参数影响 —— 它是"明细被截断了"
        的说明来源，改成截断后的长度会让用户以为回测只跑出这么点信号。

        `collapse_runs=True` 时先把"同一事件连报多日"合并成一条（见
        `collapse_signal_runs`），再取尾部 —— **顺序不能反**：先取尾部会把
        一个事件的中间几天当成独立样本，合并后数量远小于 limit，等于白截。
        """
        source = self.records
        collapsed = 0
        if collapse_runs:
            merged = collapse_signal_runs(source)
            collapsed = len(source) - len(merged)
            recent: list[Any] = (
                merged[-max(int(record_limit), 0):] if record_limit else [])
            rows = list(reversed(recent))
        else:
            recent = (self.records[-max(int(record_limit), 0):]
                      if record_limit else [])
            rows = [item.to_dict() for item in reversed(recent)]
        return {
            "run_id": self.run_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "seconds": self.seconds,
            "range": [self.range_start, self.range_end], "days": self.days,
            "by_kind": {kind: {str(h): s.to_dict() for h, s in rows.items()}
                        for kind, rows in self.by_kind.items()},
            "by_etf": {code: {str(h): s.to_dict() for h, s in rows.items()}
                       for code, rows in self.by_etf.items()},
            "by_regime": {key: {str(h): s.to_dict() for h, s in rows.items()}
                          for key, rows in self.by_regime.items()},
            "by_kind_regime": {key: {str(h): s.to_dict() for h, s in rows.items()}
                               for key, rows in self.by_kind_regime.items()},
            "records": rows,
            "record_count": len(self.records),
            #: 与 `record_count` 同义。`signals` 这个名字是 HTTP 层
            #: （`storage._etf_backtest_dict` 从库里的 `signals` 列）与面板
            #: 用的，两个名字指同一个数 —— 这里一并给出，免得调用方
            #: 拿 `result` 时找不到自己习惯的那个键。
            "signals": len(self.records),
            #: 因"同一事件连报多日"而被合并掉的行数（0 = 未合并）
            "collapsed": collapsed,
            #: 样本内 / 样本外切分点与对照表（切分点为空时 `validation` 为空）
            "split": self.split,
            "validation": {key: {str(h): s.to_dict() for h, s in rows.items()}
                           for key, rows in self.validation.items()},
            "gaps": self.gaps, "error": self.error,
        }


def collapse_signal_runs(records: Sequence[SignalRecord],
                         *, gap_days: int = 7) -> list[dict[str, Any]]:
    """把**同一个事件连续多日重复报**的信号合并成一条，返回 `[{...}, ...]`。

    ## 为什么必须合并（2026-09-22 实测）

    判据用的是"份额 **5 日累计**"变化，而份额是**存量** —— 一次资金流入发生后，
    接下来 5 天的累计值天天都超标，于是**同一个事件会连续报 5~21 天**。
    实测最近 300 条明细：

        512100.SH 风险信号        43 条 → 只有 9 个独立事件（最长连报 12 天）
        159516.SZ 行业反转警示    41 条 → 只有 4 个独立事件（最长连报 21 天）
        510050.SH 风险信号        37 条 → 只有 6 个独立事件（最长连报 18 天）

    **300 条明细只对应 63 个独立事件**，放大约 4.8 倍。用户打开明细看到的是
    "同一只 ETF 同一个信号连着十几行"—— 那既不是十几次机会，也不能当十几个
    独立样本看，除了制造"误报很多"的错觉没有任何信息量。

    ## 合并规则

    同一 `(code, kind)` 且相邻两个交易日期间隔 ≤ `gap_days` **自然日**的，
    视为同一个事件；只保留**第一次触发**那天（那是真正可行动的日子），
    并在 `repeat_days` 里如实给出这段连续报了多少天。

    ⚠️ 只作用于**回测明细的展示**，不改动：
    * `report.records` 本身（`by_kind` / `by_etf` 等统计仍按日计样本）；
    * 实时信号与告警（那个按 `alert_id = 日期+代码+类型` 逐日触发是有意的：
      资金还在流入就该每天提醒，这是"持续性确认"而不是重复）。
    """
    grouped: dict[tuple[str, str], list[SignalRecord]] = {}
    for item in records:
        grouped.setdefault((item.signal.code, item.signal.kind), []).append(item)

    out: list[tuple[str, dict[str, Any]]] = []
    for (code, kind), items in grouped.items():  # noqa: B007 键只用于分组
        ordered = sorted(items, key=lambda row: row.signal.date)
        run: list[SignalRecord] = []
        for item in ordered:
            if run:
                gap = _day_gap(run[-1].signal.date, item.signal.date)
                if gap is None or gap > gap_days:
                    out.append(_collapse(run))
                    run = []
            run.append(item)
        if run:
            out.append(_collapse(run))
    # 明细表按日期倒序展示，这里先按日期升序排好再翻转
    out.sort(key=lambda pair: (pair[0], pair[1].get("code") or ""))
    return [payload for _, payload in out]


def _collapse(run: Sequence[SignalRecord]) -> tuple[str, dict[str, Any]]:
    """一个连续事件 → `(首个日期, 明细字典)`；`repeat_days` = 这段连报了多少天。"""
    first = run[0]
    payload = first.to_dict()
    payload["repeat_days"] = len(run)
    payload["repeat_until"] = run[-1].signal.date if len(run) > 1 else ""
    return str(first.signal.date), payload


def _day_gap(later: str, earlier: str) -> int | None:
    """两个 `YYYYMMDD` 之间的自然日间隔（解析失败返回 None = 视为不连续）。"""
    try:
        return (datetime.strptime(str(earlier), "%Y%m%d")
                - datetime.strptime(str(later), "%Y%m%d")).days
    except (TypeError, ValueError):
        return None


# ==================================================================
# 回测
# ==================================================================


def _forward_returns(closes: Sequence[float], index: int,
                     horizons: Sequence[int]) -> tuple[dict[int, float | None],
                                                       dict[int, float | None]]:
    """信号在第 `index` 天时，各观察期的收益（T 收盘 / T+1 收盘两个基准）。"""
    base = closes[index]
    base_t1 = closes[index + 1] if index + 1 < len(closes) else None
    out: dict[int, float | None] = {}
    out_t1: dict[int, float | None] = {}
    for horizon in horizons:
        target = index + horizon
        if base and target < len(closes):
            out[horizon] = closes[target] / base - 1.0
        else:
            out[horizon] = None
        # T+1 口径：买在次日收盘，持有到 index+1+horizon-1 = index+horizon
        if base_t1 and target < len(closes):
            out_t1[horizon] = closes[target] / base_t1 - 1.0
        else:
            out_t1[horizon] = None
    return out, out_t1


def _path_stats(closes: Sequence[float], index: int,
                horizon: int) -> tuple[float | None, float | None]:
    """窗口内的最大浮盈与最大浮亏（以信号日收盘为基准）。"""
    base = closes[index]
    if not base:
        return None, None
    end = min(index + horizon, len(closes) - 1)
    window = closes[index:end + 1]
    if len(window) < 2:
        return None, None
    return (max(window) / base - 1.0, min(window) / base - 1.0)


def _regime_of(closes: Sequence[float], index: int) -> MarketRegime:
    """信号日的市场环境（委托 `etf_flow.classify_regime`，与实盘同一口径）。"""
    key, change = classify_regime(
        closes[:index + 1], window=REGIME_WINDOW,
        bull=REGIME_BULL_THRESHOLD, bear=REGIME_BEAR_THRESHOLD)
    return MarketRegime(key=key, label=REGIME_LABELS.get(key, key),
                        trade_date="", window=REGIME_WINDOW, change=change)


def _stats(values: Sequence[float | None], *, horizon: int,
           targets: dict[str, Any], kind: str = "",
           values_t1: Sequence[float | None] = (),
           dates: Sequence[str] = (),
           calendar: Sequence[str] = ()) -> HorizonStats:
    """一组收益的统计量。

    `dates` 必须**与 `values` 里非空的那部分一一对应**：独立样本数只在
    "算得出收益"的信号之间数。把无法评估的信号也算进去，会让它白白占掉一个
    独立时段的名额，把独立样本数算少。

    `kind` 决定达标方向（风险信号预测的是下跌，见 `etf_flow_stats.target_hit`）。
    """
    clean = [value for value in values if value is not None]
    # 目标线要按方向取：风险信号的门槛可能在 `median_return_short` 里，
    # 缺配才回退到做多那一条（对称口径）。`HorizonStats.target_median`
    # 报的就是**这条信号实际被比的那把尺子**，否则界面上的 tooltip 会撒谎。
    short = kind in NEGATIVE_KINDS
    target_median, target_win = resolve_targets(targets, horizon, short=short)
    out = HorizonStats(horizon=horizon, samples=len(clean), kind=kind,
                       target_median=target_median,
                       target_win_rate=target_win)
    if clean:
        out.median = round(statistics.median(clean), 4)
        out.mean = round(statistics.fmean(clean), 4)
        wins = sum(1 for value in clean if value > 0)
        out.win_rate = round(wins / len(clean), 4)
        # 假阳性率**恒等于** 1 - 胜率（口径见模块文档：收益 <= 0 即假阳性）。
        # 这里显式写成减法而不是再数一遍 `value <= 0`，是为了让这个恒等关系
        # 在代码里就看得见 —— 两者各数一遍时，一旦口径改动（比如把 0 算赢）
        # 会出现两个互相矛盾的数，而报表上完全看不出来。
        out.false_positive_rate = round(1.0 - out.win_rate, 4)
        interval = wilson_interval(wins, len(clean))
        if interval is not None:
            out.win_rate_low, out.win_rate_high = interval
    if dates:
        out.independent = len(independent_episodes(dates, calendar=calendar,
                                                   gap=horizon))
    t1 = [value for value in values_t1 if value is not None]
    if t1:
        out.median_t1 = round(statistics.median(t1), 4)
        out.win_rate_t1 = round(sum(1 for value in t1 if value > 0) / len(t1), 4)
    out.passed = target_hit(kind=kind, median=out.median, win_rate=out.win_rate,
                            target_median=target_median, target_win=target_win)
    return out


def run(store: Any, *, config: FlowConfig | None = None,
        start: str = "", end: str = "", progress: Any = None
        ) -> BacktestReport:
    """跑一次完整回测（全内存，不逐日查库）。"""
    import time

    begun = time.monotonic()
    config = config or load_config()
    report = BacktestReport(
        run_id=f"etfbt-{datetime.now():%Y%m%dT%H%M%S}",
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    if not config.loaded:
        report.error = config.gap or "ETF 份额监控配置不可用"
        return report

    bt = config.backtest or {}
    span_start = start or str(bt.get("start") or "20180101")
    span_end = end or str(bt.get("end") or "")
    # ⚠️ 空字符串不能直接当 SQL 上界：`trade_date BETWEEN '20180101' AND ''`
    # 匹配不到任何行，回测会以"本地没有宽基指数数据"收场 ——
    # 而真正的原因只是 end 没填。这里补齐成本地最新交易日。
    if not span_end:
        span_end = _latest_date(store) or datetime.now().strftime("%Y%m%d")
    horizons = [int(item) for item in (bt.get("horizons") or [5, 10, 20, 34])]
    targets = dict(bt.get("targets") or {})
    max_horizon = max(horizons)
    report.split = _normalize_split(bt.get("split"))
    if bt.get("split") and not report.split:
        report.gaps.append(
            f"backtest.split 的值 {bt.get('split')!r} 不是 YYYYMMDD 形式，"
            "已忽略 —— 样本内/样本外对照表因此为空")

    etf_codes = [item.code for item in config.all_etfs]
    index_codes = sorted({g.index for g in config.groups if g.index})
    etf_bars = store.etf_bars(etf_codes, start=span_start, end=span_end)
    index_bars = store.index_bars(index_codes, start=span_start, end=span_end)
    missing = [code for code in etf_codes if not etf_bars.get(code)]
    if missing:
        report.gaps.append(f"{len(missing)} 只 ETF 没有本地份额数据："
                           + "、".join(missing[:6]))
    if not index_bars:
        report.error = "本地没有宽基指数数据（先同步 index 数据集）"
        return report

    # 主指数（第一个 core 组的指数）的交易日作为回测日历
    primary = next((g.index for g in config.groups
                    if g.level == "core" and g.index), index_codes[0])
    primary_rows = index_bars.get(primary) or []
    if len(primary_rows) < REGIME_WINDOW + max_horizon:
        report.error = f"主指数 {primary} 历史不足"
        return report
    calendar = [day for day, _ in primary_rows]
    report.range_start, report.range_end = calendar[0], calendar[-1]
    # 每只 ETF 的份额序列（各序列自身带日期，切片时按主指数日历的当日截断）
    etf_rows = {code: rows for code, rows in etf_bars.items()}

    # 从"能算 20 日份额变化 + 指数分位"的第一天开始，到最后留出 max_horizon
    warmup = max(int(config.threshold("percentile_window", 34) or 34), 21)
    records: list[SignalRecord] = []
    cursor = warmup
    total = len(calendar) - max_horizon
    while cursor < total:
        day = calendar[cursor]
        if progress is not None and cursor % 200 == 0:
            progress(f"{cursor}/{total} {day}")
        day_index = {code: _slice_upto(rows, day) for code, rows in etf_rows.items()}
        indicators = build_indicators(day_index, config, trade_date=day)
        positions = build_positions(
            {code: _slice_pairs(rows, day) for code, rows in index_bars.items()},
            config=config, trade_date=day)
        # 环境必须在 evaluate 之前算好：机会信号能否告警取决于它
        closes = [close for _, close in primary_rows]
        regime = _regime_of(closes, cursor)
        apply_regime_policy(regime, config)
        signals = evaluate(indicators, positions, config=config,
                           trade_date=day, regime=regime)
        if signals:
            for signal in signals:
                # 用该信号对应组的指数算后续收益
                rows = index_bars.get(signal.index_code) or primary_rows
                series = [close for _, close in rows]
                idx = _index_of(rows, day)
                if idx is None:
                    continue
                fwd, fwd_t1 = _forward_returns(series, idx, horizons)
                gain, draw = _path_stats(series, idx, max(horizons))
                signal.forward = {str(k): v for k, v in fwd.items() if v is not None}
                records.append(SignalRecord(
                    signal=signal, regime=regime.key, forward=fwd,
                    forward_t1=fwd_t1, max_gain=gain, max_drawdown=draw))
        cursor += 1

    report.days = total - warmup
    report.records = records
    if not records:
        report.gaps.append("回测区间内没有任何信号触发（检查阈值是否过严）")
    # 切分点两侧的样本数要在 gaps 里如实说明：全都落在同一侧时对照表只剩
    # 半边，"样本外无结论"和"切分点写错了"在界面上长得一样。
    if report.split:
        inside = sum(1 for item in records if item.signal.date < report.split)
        if not inside or inside == len(records):
            report.gaps.append(
                f"样本切分点 {report.split} 把信号全分到了一侧（样本内 {inside}"
                f" / 共 {len(records)} 条）—— 样本内/外对照不可用，"
                "请检查 backtest.split 是否落在回测区间内")
    _aggregate(report, horizons, targets, calendar=calendar)
    report.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report.seconds = round(time.monotonic() - begun, 2)
    return report


def _normalize_split(raw: Any) -> str:
    """把 `backtest.split` 规范成 `YYYYMMDD`；无法识别时返回空串（= 不切分）。

    ## 为什么要校验格式，而不是直接拿字符串比较

    切分是**字符串比较**（`trade_date < split`），而 `ml_etf.trade_date` 一律是
    8 位 `YYYYMMDD`。任何不是这个形状的值都会让比较退化成字符序的巧合：
    `"23/01/01"` 首字符 `'3' > '0'`，于是它排在所有 `2018…` 之后 ——
    样本内一条不剩、样本外是全部，而两个半边**各自看起来都很正常**。
    宁可显式报"配置值无法识别"，也不要静默切出一个假的分组。

    `run()` 里另有一道兜底：即使格式合法，只要信号全落在同一侧就记进 `gaps`。
    两道检查都要有 —— 这道管"写法不对"，那道管"切分点落在区间之外"。
    """
    text = str(raw or "").strip()
    return text if len(text) == 8 and text.isdigit() else ""


def _latest_date(store: Any) -> str:
    """本地 ETF 数据的最新交易日（`end` 留空时的解析目标）。"""
    rows = store._read(  # noqa: SLF001 单值只读
        "SELECT MAX(trade_date) AS d FROM ml_etf")
    return str(rows[0]["d"] or "") if rows else ""


def _slice_upto(rows: Sequence[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """取某日及之前的行（升序序列的二分可用，但数据量小，线性足够）。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("trade_date") or "") > day:
            break
        out.append(row)
    return out


def _slice_pairs(rows: Sequence[tuple[str, float]], day: str
                 ) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for row in rows:
        if row[0] > day:
            break
        out.append(row)
    return out


def _index_of(rows: Sequence[tuple[str, float]], day: str) -> int | None:
    for index, row in enumerate(rows):
        if row[0] == day:
            return index
        if row[0] > day:
            return None
    return None


def _aggregate(report: BacktestReport, horizons: Sequence[int],
               targets: dict[str, Any],
               *, calendar: Sequence[str] = ()) -> None:
    """按信号类型 / ETF / 市场环境分组统计，并做样本内 / 样本外对照。

    `calendar` 是主指数的交易日列表，只用于把"两个信号的间隔"折成交易日 ——
    独立样本数要按观察期长度做去重，而观察期是交易日口径（见 `etf_flow_stats`）。
    """

    def bucket(records: Sequence[SignalRecord],
               kind: str = "") -> dict[int, HorizonStats]:
        """一组信号在各观察期上的统计。

        `kind` 只影响**达标方向**（风险信号预测的是下跌）。混合类型的桶
        ——等级维度、按 ETF、按环境——传空串走做多口径：那种桶里正负两类信号
        本来就不该共用一个"达标"判断，硬判只会得到一个没有意义的标记。
        """
        out: dict[int, HorizonStats] = {}
        for horizon in horizons:
            pairs = [(item.signal.date, item.forward.get(horizon))
                     for item in records]
            out[horizon] = _stats(
                [value for _, value in pairs], horizon=horizon,
                targets=targets, kind=kind,
                values_t1=[item.forward_t1.get(horizon) for item in records],
                dates=[day for day, value in pairs if value is not None],
                calendar=calendar)
        return out

    report.by_kind = {}
    for kind in (KIND_OPPORTUNITY, KIND_RISK, KIND_INDUSTRY_REVERSAL):
        rows = [item for item in report.records if item.signal.kind == kind]
        if rows:
            report.by_kind[kind] = bucket(rows, kind)
    # 等级维度也统计（强/中/弱），帮助判断"共振确认"是否真的更有效
    report.by_kind["_by_level"] = {}
    for level in (LEVEL_STRONG, LEVEL_MEDIUM, LEVEL_WEAK):
        rows = [item for item in report.records if item.signal.level == level]
        if rows:
            report.by_kind[f"level:{level}"] = bucket(rows)

    # 门控效果对照：机会/风险信号各拆成"环境放行"与"被降级"两桶。
    # 两桶的差异就是门控的价值 —— 只报合并值会看不出门控做了什么。
    #
    # ⚠️ "放行"必须按**等级**判定，不能只按 `gated is False`：
    # 走"份额异动但指数不在极端区"那条路径的信号本来就是弱信号（观察列表），
    # 它们 gated 也是 False，却从不告警。若把它们算进 live，会把真正放行的
    # 极端区信号和一堆观察项混在一起，把放行桶的成绩稀释掉 ——
    # 这正是第一版分桶的错误（live 桶 359 个样本 vs 熊市实际仅 77 个）。
    for kind_key, prefix in ((KIND_OPPORTUNITY, "opportunity"),
                             (KIND_RISK, "risk")):
        for suffix, selector in (
                ("live", lambda item: not item.signal.gated
                 and item.signal.level in ALERT_LEVELS),
                ("gated", lambda item: item.signal.gated)):
            rows = [item for item in report.records
                    if item.signal.kind == kind_key and selector(item)]
            if rows:
                report.by_kind[f"{prefix}_{suffix}"] = bucket(rows, kind_key)

    report.by_etf = {}
    for code in sorted({item.signal.code for item in report.records}):
        rows = [item for item in report.records if item.signal.code == code]
        report.by_etf[code] = bucket(rows)

    report.by_regime = {}
    for regime in (REGIME_BULL, REGIME_BEAR, REGIME_RANGE):
        rows = [item for item in report.records if item.regime == regime]
        if rows:
            report.by_regime[regime] = bucket(rows)

    report.by_kind_regime = {}
    for kind in (KIND_OPPORTUNITY, KIND_RISK, KIND_INDUSTRY_REVERSAL):
        for regime in (REGIME_BULL, REGIME_BEAR, REGIME_RANGE):
            rows = [item for item in report.records
                    if item.signal.kind == kind and item.regime == regime]
            if rows:
                report.by_kind_regime[f"{kind}@{regime}"] = bucket(rows, kind)
    report.by_kind = {k: v for k, v in report.by_kind.items() if k != "_by_level"}
    _aggregate_validation(report, bucket)


def _validation_groups() -> list[tuple[str, Any, str]]:
    """样本内 / 样本外对照要覆盖的分组：`(键, 选择器, 信号类型)`。

    ## 为什么只覆盖这些，而不是把四张表整体再算一遍

    对照表的价值在于"同一格在前后两段是否一致"。`by_etf`（9 只 × 4 个观察期）
    与等级维度（强/中/弱）在样本外只剩个位数样本 —— 铺开来看是噪声，
    还会把真正要验证的那几格淹掉。而门控的**每一条**结论都能落到下面这些
    格子上（见 `configs/etf_flow.yaml` 的 `regime` 段注释）：

        opportunity@{bull,bear,range}   机会信号是否只在熊市为正
        risk@{bull,bear,range}          风险信号在熊市反向（那条"13 个样本"的结论）
        opportunity_live / _gated       门控两桶的差值在样本外还在不在
        risk_live / _gated              同上
        industry_reversal               行业反转警示是否稳定为负

    ## 为什么用工厂函数而不是在循环里写 lambda

    λ 捕获的是变量本身而不是当时的值，循环里直接写会让所有选择器一起指向
    最后一个 `kind` —— 分组会**静默地**全算错，而数字看起来完全正常。
    每类选择器各一个工厂函数，"按值捕获"就不再依赖记性。
    """
    def kind_of(kind: str) -> Any:
        return lambda item: item.signal.kind == kind

    def live_of(kind: str) -> Any:
        return lambda item: (item.signal.kind == kind and not item.signal.gated
                             and item.signal.level in ALERT_LEVELS)

    def gated_of(kind: str) -> Any:
        return lambda item: item.signal.kind == kind and item.signal.gated

    def cell_of(kind: str, regime: str) -> Any:
        return lambda item: item.signal.kind == kind and item.regime == regime

    groups: list[tuple[str, Any, str]] = [
        (kind, kind_of(kind), kind)
        for kind in (KIND_OPPORTUNITY, KIND_RISK, KIND_INDUSTRY_REVERSAL)]
    for kind, prefix in ((KIND_OPPORTUNITY, "opportunity"), (KIND_RISK, "risk")):
        groups.append((f"{prefix}_live", live_of(kind), kind))
        groups.append((f"{prefix}_gated", gated_of(kind), kind))
    for kind in (KIND_OPPORTUNITY, KIND_RISK):
        for regime in (REGIME_BULL, REGIME_BEAR, REGIME_RANGE):
            groups.append((f"{kind}@{regime}", cell_of(kind, regime), kind))
    return groups


def _aggregate_validation(report: BacktestReport, bucket: Any) -> None:
    """样本内 / 样本外对照：把同一组信号按 `report.split` 前后拆开各算一遍。

    ## 为什么这件事必须做

    所有阈值（3% / 30% / 70% / 34 日、环境线的 ±15%）都是在**全区间**上看着
    回测结果调出来的。再把同一份全区间结果当成"验证"，就是循环论证 ——
    它只能证明"当初挑出来的那一段历史上确实好看"，那是挑参数的必然结果。
    切分之后 `in:` 那半边是"当初看的那批"，`out:` 那半边才是没看过的。

    ## 为什么两边都要给独立样本数

    样本外最常见的结局不是"结论翻转"，而是"只剩一两个独立时段"。这时候
    "结论仍然成立"和"结论根本无法验证"在数字上长得一模一样（都是 +8%、
    胜率 80%），**只有独立样本数能把它们区分开**。
    """
    report.validation = {}
    if not report.split:
        return
    split = str(report.split)
    for label, keep in (("in", lambda day: str(day) < split),
                        ("out", lambda day: str(day) >= split)):
        for key, selector, kind in _validation_groups():
            rows = [item for item in report.records
                    if selector(item) and keep(item.signal.date)]
            if rows:
                report.validation[f"{label}:{key}"] = bucket(rows, kind)


# ==================================================================
# 报告
# ==================================================================


def render(report: BacktestReport) -> str:
    """生成 Markdown 报告。"""
    lines: list[str] = ["# ETF 份额信号回测报告", ""]
    lines.append(f"- 回测区间：`{report.range_start}` ~ `{report.range_end}`"
                 f"（{report.days} 个交易日）")
    lines.append(f"- 运行 ID：`{report.run_id}`；耗时 {report.seconds:.1f} 秒")
    lines.append(f"- 信号总数：**{len(report.records)}**")
    if report.error:
        lines.append(f"- ⚠️ **本次回测未跑完**：{report.error}")
    lines.append("")
    lines.append("> **两个收益口径**：`T收盘` 以信号日收盘价为基准（乐观，"
                 "与需求文档的 T+N 口径一致）；`T+1收盘` 买在次日收盘（保守）。"
                 "份额数据次日 8:30 才更新，真实可成交的是后者的量级 —— "
                 "两者差距大说明收益主要来自信号日当天已涨完的部分。")
    lines.append("")
    lines.append("> **`样本` 与 `独立时段` 必须一起看。** `样本` 是信号条数，"
                 "`独立时段` 是**前瞻窗口互不重叠**的时段个数（按观察期长度去重）。"
                 "判据用\"份额 5 日累计\"而份额是存量，一次资金流入会让同一个事件"
                 "连续多日超标；同系列 ETF 又常在同一天一起触发 —— "
                 "两者叠加会让\"样本\"虚高数倍。" + _independence_example(report))
    lines.append("")
    lines.append("> **`胜率95%CI` 是 Wilson 区间。** 小样本下点估计没有意义 ——"
                 " 同样报 83.3% 的胜率，66 个样本时区间约 [72.6%, 90.4%]，"
                 "而只有两三个样本时区间宽到没有统计意义。")
    lines.append("")
    lines.append("> **假阳性率不单列**：本模块的定义是\"收益 <= 0\"的比例，"
                 "它恒等于 `1 - 胜率`，单列一栏等于把同一个条件印两遍。"
                 "**`达标` 按信号方向判**：机会/行业类看 `中位数 >= 目标`，"
                 "风险类看 `中位数 <= -目标` —— 拿做多口径去判一个正确的看空信号，"
                 "会让它在报表里永远显示失败。")
    lines.append("")

    for title, source in (
            ("一、按信号类型", {k: v for k, v in report.by_kind.items()
                                if not k.startswith("level:")}),
            ("二、按信号等级", {k: v for k, v in report.by_kind.items()
                                if k.startswith("level:")}),
            ("三、按市场环境", report.by_regime),
            ("四、按 ETF 产品", report.by_etf)):
        lines.append(f"## {title}")
        lines.append("")
        if not source:
            lines.append("（无样本）")
            lines.append("")
            continue
        lines.extend(_stats_header())
        for group, rows in source.items():
            for horizon in sorted(rows, key=lambda item: int(item)):
                lines.append(_stats_row(_group_label(group), horizon,
                                        rows[horizon]))
        lines.append("")

    lines.append("## 五、信号类型 × 市场环境")
    lines.append("")
    lines.append("> 这张表是定门控规则的依据。只看「三、按市场环境」会把机会、风险、"
                 "行业反转四种类型混在一起统计，各类型方向相反时互相抵消 —— "
                 "那正是最初误判「机会信号无效」的直接原因。")
    lines.append("")
    if not report.by_kind_regime:
        lines.append("（无样本）")
        lines.append("")
    else:
        lines.extend(_stats_header())
        for group, rows in report.by_kind_regime.items():
            for horizon in sorted(rows, key=lambda item: int(item)):
                lines.append(_stats_row(_kind_regime_label(group), horizon,
                                        rows[horizon]))
        lines.append("")

    lines.append("## 六、样本内 / 样本外对照")
    lines.append("")
    if not report.split:
        lines.append("（未配置 `backtest.split`，本次不做切分）")
        lines.append("")
    elif not report.validation:
        lines.append("（切分后两侧都没有样本）")
        lines.append("")
    else:
        lines.append(f"> 切分点 `{report.split}`：之前的信号用于**看结论**"
                     "（阈值就是在这段上看着结果调出来的），之后那段才是**样本外**。"
                     "只看全区间等于用同一份数据既调参又验证，是循环论证。")
        lines.append("")
        lines.append("> 样本外要特别注意 `独立时段`：最常见的结局不是结论翻转，"
                     "而是样本外只剩一两个独立时段 —— 那时候\"结论仍然成立\""
                     "和\"无法验证\"在胜率上长得一模一样。")
        lines.append("")
        for sample, title in (("in", "样本内"), ("out", "样本外")):
            block = {key.split(":", 1)[1]: rows
                     for key, rows in report.validation.items()
                     if key.startswith(f"{sample}:")}
            where = "之前" if sample == "in" else "及之后"
            lines.append(f"### {title}（`{sample}:`，{report.split} {where}）")
            lines.append("")
            if not block:
                lines.append("（无样本）")
                lines.append("")
                continue
            lines.extend(_stats_header())
            for group, rows in block.items():
                for horizon in sorted(rows, key=lambda item: int(item)):
                    lines.append(_stats_row(_group_label(group), horizon,
                                            rows[horizon]))
            lines.append("")

    lines.append("## 七、信号流水（最近 80 条）")
    lines.append("")
    if not report.records:
        lines.append("（无信号）")
    else:
        lines.append("| 日期 | ETF | 类型 | 等级 | 环境 | 指数分位 | 份额日环比 | "
                     "5日累计 | 共振 | T+34 | T+34(T+1) | 最大浮亏 |")
        lines.append("|---|---|---|---|---|---:|---:|---:|:---:|---:|---:|---:|")
        for item in sorted(report.records, key=lambda r: r.signal.date,
                           reverse=True)[:80]:
            sig = item.signal
            lines.append(
                f"| {sig.date} | {sig.name}({sig.code}) | {sig.kind_label} | "
                f"{sig.level_label}{'（门控）' if sig.gated else ''} | "
                f"{sig.regime_label} | "
                f"{_pct(sig.index_percentile)} | "
                f"{_pct(sig.change_1d)} | {_pct(sig.change_5d)} | "
                f"{'⭐' if sig.resonance else '—'} | "
                f"{_pct(item.forward.get(34))} | "
                f"{_pct(item.forward_t1.get(34))} | "
                f"{_pct(item.max_drawdown)} |")
    lines.append("")
    if report.gaps:
        lines.append("## 附 · 数据缺口")
        lines.append("")
        for gap in report.gaps:
            lines.append(f"- {gap}")
        lines.append("")
    lines.append("> 本报告为量化统计结果，不构成投资建议。"
                 "份额数据次日更新，信号为 T+1 确认，**不适合日内做 T**。")
    return "\n".join(lines)


KIND_CN = {KIND_OPPORTUNITY: "🟢 机会信号", KIND_RISK: "🔴 风险信号",
           KIND_INDUSTRY_REVERSAL: "🟡 行业反转",
           "opportunity_live": "🟢 机会信号（放行·会告警）",
           "opportunity_gated": "⚪ 机会信号（被门控降级）",
           "risk_live": "🔴 风险信号（放行·会告警）",
           "risk_gated": "⚪ 风险信号（被门控降级）"}
LEVEL_CN = {"strong": "🔴 强信号", "medium": "🟡 中信号", "weak": "🟢 弱信号"}


def _kind_regime_label(key: str) -> str:
    """把 `"opportunity@bear"` 这种键渲染成「🟢 机会信号 / 熊市」。"""
    kind, _, regime = key.partition("@")
    return f"{KIND_CN.get(kind, kind)} / {REGIME_LABELS.get(regime, regime)}"


def _group_label(key: str) -> str:
    """分组键 → 中文标签。三种形状都要认：

        `kind@regime`（交叉表）→ 「🟢 机会信号 / 熊市」
        `level:strong`（等级）  → 「🔴 强信号」
        `opportunity_live`      → 「🟢 机会信号（放行·会告警）」
        其余（`bear` / ETF 代码）→ 环境标签或原样返回

    抽成一个函数是因为它现在被**两处**用到（四张统计表 + 样本外对照），
    各写一遍迟早会漏掉某一种形状，而漏掉的那一支只会让标签变成英文键，
    看起来像"还没翻译"，不会报错。
    """
    if "@" in key:
        return _kind_regime_label(key)
    if key.startswith("level:"):
        return LEVEL_CN.get(key.split(":", 1)[1], key)
    return KIND_CN.get(key, REGIME_LABELS.get(key, key))


def _independence_example(report: BacktestReport) -> str:
    """正文里"样本 vs 独立时段"那句话，**数字从报告实时取值**。

    ## 为什么不能把数字写在字符串里

    这里原本写死成"实测 `opportunity_live` 是 **66 条信号 / 3 个独立时段**"，
    而真实计算值是 **5** —— 于是同一份报告里，正文说 3、下面的表格说 5，
    **自相矛盾且不报任何错**（这个错值来自早期一个用"信号日集合"当伪日历的
    临时脚本，改用真实交易日历修正后没有回头改这句话）。

    写死的示例数字迟早会漂移，所以改成按当前回测结果取；取不到
    （该分桶本次无样本）时退化成不带数字的说法，而不是编一个。
    """
    live = (report.by_kind.get("opportunity_live") or {}).get(34)
    if live is not None and live.independent:
        return (f"实测 `opportunity_live`（T+34）是 **{live.samples} 条信号 / "
                f"{live.independent} 个独立时段** —— 只看条数会把少数几个事件的"
                "结论读成统计事实。")
    fallback = (report.by_kind.get(KIND_OPPORTUNITY) or {}).get(34)
    if fallback is not None and fallback.independent:
        return (f"实测机会信号（T+34）是 **{fallback.samples} 条信号 / "
                f"{fallback.independent} 个独立时段**。")
    return "只看样本条数会把少数几个事件的结论读成统计事实。"


def _stats_header() -> list[str]:
    """统计表的表头（两行：列名 + 对齐）。四张表与样本外对照共用。"""
    return [
        "| 分组 | 观察期 | 样本 | 独立时段 | T收盘中位数 | 胜率 | 胜率95%CI | "
        "T+1中位数 | T+1胜率 | 达标 |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|:---:|"]


def _stats_row(label: str, horizon: Any, stats: HorizonStats) -> str:
    """一行统计。列的顺序必须与 `_stats_header` 一致。"""
    independent = "—" if stats.independent is None else str(stats.independent)
    return (f"| {label} | T+{horizon} | {stats.samples} | {independent} | "
            f"{_pct(stats.median)} | {_pct(stats.win_rate)} | {_ci(stats)} | "
            f"{_pct(stats.median_t1)} | {_pct(stats.win_rate_t1)} | "
            f"{_mark(stats.passed)} |")


def _ci(stats: HorizonStats) -> str:
    """胜率的 Wilson 95% 区间（无样本时为 `—`）。"""
    if stats.win_rate_low is None or stats.win_rate_high is None:
        return "—"
    return (f"[{stats.win_rate_low * 100:.1f}%, "
            f"{stats.win_rate_high * 100:.1f}%]")


def _mark(passed: bool | None) -> str:
    """达标标记。`None` = 没配目标线或中位数为空（**不表态**，不等于未通过）。"""
    if passed is None:
        return "—"
    return "✅" if passed else "⚠️"


def _pct(value: Any) -> str:
    if value is None:
        return "无数据"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    # Windows 控制台默认 GBK，报告里的 emoji 标签直接抛 UnicodeEncodeError。
    # 报告文件本身写 UTF-8 不受影响，这里只保证控制台不中断收尾输出。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover 非标准流
                pass

    parser = argparse.ArgumentParser(description="ETF 份额信号回测")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--out", default="docs/ETF_FLOW_BACKTEST.md")
    args = parser.parse_args(argv)

    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore

    store = MainlineDataStore(config=load_config())
    report = run(store, start=args.start, end=args.end,
                 progress=lambda text: print("  ", text, flush=True))
    if report.error:
        print("回测失败：", report.error)
        return 1
    report.markdown = render(report)
    from pathlib import Path

    target = Path(args.out)
    target.write_text(report.markdown, encoding="utf-8")
    print(f"已写入 {target}（{len(report.records)} 条信号）")
    # 控制台用纯文本标签：Windows 默认 GBK 编码，emoji 会直接抛
    # UnicodeEncodeError（报告文件本身是 UTF-8，不受影响）
    plain = {KIND_OPPORTUNITY: "机会信号", KIND_RISK: "风险信号",
             KIND_INDUSTRY_REVERSAL: "行业反转",
             "opportunity_live": "机会信号(放行)",
             "opportunity_gated": "机会信号(门控)",
             "risk_live": "风险信号(放行)",
             "risk_gated": "风险信号(门控)"}
    for kind, rows in report.by_kind.items():
        stats = rows.get(max(rows, key=lambda item: int(item))) if rows else None
        if stats is not None and stats.samples:
            label = plain.get(kind, LEVEL_CN.get(kind.split(":")[-1], kind))
            print(f"  {label}: 最长观察期样本 {stats.samples}，"
                  f"中位数 {_pct(stats.median)}，胜率 {_pct(stats.win_rate)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = [
    "REGIME_LABELS",
    "BacktestReport",
    "HorizonStats",
    "SignalRecord",
    "render",
    "run",
]
