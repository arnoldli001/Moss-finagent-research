"""ETF 环境调节层（`regime_multiplier`）四条规则的独立回测验证。

## 为什么要有这个模块

需求文档要求「规则1 触发时全场 +15%」并要求「输出报告：各规则的触发次数、
平均收益、胜率」。但 `docs/ETF_FLOW_BACKTEST.md` 已经证明**同形态的信号在
牛市/震荡市是负收益**（熊市 +8.78%/76.6%，牛市 -1.53%/36.7%，
震荡 -0.70%/46.5%）。所以"规则 1 该不该无条件 +15%"是一个**必须用数据回答**
的问题，不能照文档实现。

本模块逐日重放 2018 至今，对每条规则统计触发次数与触发后
T+5/10/20/34 的**标的指数/ETF 收益**，用来决定：

    规则 1  是否必须加熊市条件（对应 `rule1_require_bear`）
    规则 2  顶部警示的方向是否成立
    规则 3  风格切换流入前 3 名是否真有超额
    规则 4  极端流入是否真的预示反转

## 口径

**收益基准是指数/ETF 自身**，不是板块评分 —— 这个模块回答的是
"这条规则对后市方向的判断力"，不是"接进评分后能提多少分"。
后者还要经过主线三层漏斗，无法在 ETF 数据上单独验证。

**两个收益口径**（与 `etf_flow_backtest` 一致）：`T收盘` 乐观、
`T+1收盘` 保守。份额次日 8:30 才更新，**只有 T+1 口径是实盘可成交的**。

**规则 4 的分位用滚动窗口**：每个交易日只用**当日之前**的 `rule4_window`
个样本来算分位，绝不使用未来数据（用全样本分位会让结论虚高）。
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.mainline.etf_flow import (
    EtfIndicator,
    FlowConfig,
    MarketRegime,
    build_indicators,
    load_config,
)
from src.mainline.etf_flow_backtest import (
    REGIME_LABELS,
    REGIME_WINDOW,
    _regime_of,
)
from src.mainline.etf_regime import (
    RULE1,
    RULE2,
    RULE3,
    RULE4,
    RULE_LABELS,
    load_multiplier_config,
)

logger = logging.getLogger(__name__)

HORIZONS = (5, 10, 20, 34)


@dataclass
class RuleStats:
    """一条规则在一个观察期的统计。"""

    horizon: int = 0
    samples: int = 0
    median: float | None = None
    win_rate: float | None = None
    median_t1: float | None = None
    win_rate_t1: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"horizon": self.horizon, "samples": self.samples,
                "median": self.median, "win_rate": self.win_rate,
                "median_t1": self.median_t1, "win_rate_t1": self.win_rate_t1}


@dataclass
class RuleReport:
    """一条规则的整体结论。"""

    rule: str = ""
    label: str = ""
    #: `{horizon: RuleStats}`
    stats: dict[int, RuleStats] = field(default_factory=dict)
    triggers: int = 0
    by_regime: dict[str, int] = field(default_factory=dict)
    #: `{regime: {horizon: RuleStats}}` —— 定"规则 1 是否该要求熊市"的依据
    by_regime_stats: dict[str, dict[int, RuleStats]] = field(
        default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "label": self.label,
                "triggers": self.triggers, "by_regime": dict(self.by_regime),
                "stats": {str(k): v.to_dict() for k, v in self.stats.items()},
                "by_regime_stats": {
                    key: {str(h): s.to_dict() for h, s in rows.items()}
                    for key, rows in self.by_regime_stats.items()},
                "note": self.note}


@dataclass
class RegimeBacktestReport:
    """四条规则的完整回测报告。"""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    range_start: str = ""
    range_end: str = ""
    days: int = 0
    rules: list[RuleReport] = field(default_factory=list)
    markdown: str = ""
    gaps: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "started_at": self.started_at,
                "finished_at": self.finished_at, "seconds": self.seconds,
                "range": [self.range_start, self.range_end], "days": self.days,
                "rules": [item.to_dict() for item in self.rules],
                "gaps": self.gaps, "error": self.error}


def _stats(rows: Sequence[tuple[float, float]], horizon: int) -> RuleStats:
    """`rows` 是 `[(T收盘收益, T+1收盘收益), ...]`。"""
    out = RuleStats(horizon=horizon, samples=len(rows))
    if not rows:
        return out
    base = [item[0] for item in rows]
    t1 = [item[1] for item in rows]
    out.median = round(statistics.median(base), 4)
    out.win_rate = round(sum(1 for v in base if v > 0) / len(base), 4)
    out.median_t1 = round(statistics.median(t1), 4)
    out.win_rate_t1 = round(sum(1 for v in t1 if v > 0) / len(t1), 4)
    return out


def _slice(rows: Sequence[Any], index: int) -> list[Any]:
    return list(rows[: index + 1])


def run(store: Any, *, config: FlowConfig | None = None, start: str = "",
        end: str = "", progress: Any = None) -> RegimeBacktestReport:
    """逐日重放，统计四条规则的触发与后续收益（全内存，不逐日查库）。"""
    import time

    begun = time.monotonic()
    config = config or load_config()
    report = RegimeBacktestReport(
        run_id=f"regimebt-{datetime.now():%Y%m%dT%H%M%S}",
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    if not config.loaded:
        report.error = config.gap or "ETF 份额监控配置不可用"
        return report
    cfg = load_multiplier_config(config)

    codes = [spec.code for spec in config.all_etfs]
    index_codes = [group.index for group in config.groups if group.index]
    span_start = start or "20180101"
    end = end or _latest(store)
    if not end:
        report.error = "本地 ml_etf 没有数据"
        return report
    report.range_end = end

    etf_rows = _prepare_etf(store.etf_bars(codes, start=span_start, end=end))
    index_bars = store.index_bars(index_codes, start=span_start, end=end)
    calendar = sorted({str(row.get("trade_date") or "")
                       for rows in etf_rows.values() for row in rows})
    if len(calendar) < 200:
        report.error = f"交易日不足（{len(calendar)} 天），无法验证规则"
        return report
    report.range_start = calendar[0]

    # 锚点（沪深300 系列）与行业组
    anchor_codes = _group_codes(config, "hs300") or _group_codes(
        config, config.groups[0].key if config.groups else "")
    industry_codes = [spec.code for group in config.groups
                      if group.level == "industry" for spec in group.etfs]
    anchor_index = _group_index(config, "hs300")

    samples: dict[str, dict[int, list[tuple[float, float]]]] = {
        RULE1: {h: [] for h in HORIZONS}, RULE2: {h: [] for h in HORIZONS},
        RULE3: {h: [] for h in HORIZONS}, RULE4: {h: [] for h in HORIZONS}}
    #: 分环境的样本 —— 规则 1 是否该加熊市条件完全取决于这张表，
    #: 只看合并值会重演 etf_flow 那次"把三种环境平均掉得出信号无效"的误判
    by_regime_samples: dict[str, dict[str, dict[int, list[tuple[float, float]]]]] = {
        rule: {} for rule in (RULE1, RULE2, RULE3, RULE4)}
    regimes: dict[str, dict[str, int]] = {rule: {} for rule in samples}
    triggers = {rule: 0 for rule in samples}

    anchor_prices = [close for _, close in index_bars.get(anchor_index) or []]
    horizon_max = max(HORIZONS)
    total = len(calendar) - horizon_max
    cursor = REGIME_WINDOW + cfg.rule4_window
    while cursor < total:
        day = calendar[cursor]
        if progress is not None and cursor % 200 == 0:
            progress(f"{cursor}/{total} {day}")

        indicators = build_indicators(
            {code: _upto(rows, day) for code, rows in etf_rows.items()},
            config, trade_date=day)
        share = _median_change(indicators, anchor_codes)
        industry_share = _median_change(indicators, industry_codes)
        position = _percentile(index_bars.get(anchor_index), day,
                               cfg.percentile_window)
        regime = _regime_of(anchor_prices, cursor) if anchor_prices else MarketRegime()

        index_at = _index_at(index_bars.get(anchor_index), day)
        if index_at is None:
            cursor += 1
            continue
        forward = _forward(index_bars.get(anchor_index), index_at)

        # ---- 规则 1 ----
        if share is not None and position is not None and \
                position <= cfg.rule1_max_percentile and share > cfg.rule1_min_share_5d:
            triggers[RULE1] += 1
            regimes[RULE1][regime.key] = regimes[RULE1].get(regime.key, 0) + 1
            _add(samples[RULE1], forward)
            _track(by_regime_samples[RULE1], regime.key, forward)

        # ---- 规则 2 ----
        if share is not None and position is not None and \
                position >= cfg.rule2_min_percentile and share < cfg.rule2_max_share_5d:
            triggers[RULE2] += 1
            regimes[RULE2][regime.key] = regimes[RULE2].get(regime.key, 0) + 1
            # 风险信号：方向对了指数应该跌，所以收益取负号后统一按"越高越好"看
            _add(samples[RULE2], forward)
            _track(by_regime_samples[RULE2], regime.key, forward)

        # ---- 规则 3：风格切换 ----
        if share is not None and industry_share is not None and \
                share < cfg.rule3_broad_max_share_5d and \
                industry_share > cfg.rule3_industry_min_share_5d:
            triggers[RULE3] += 1
            regimes[RULE3][regime.key] = regimes[RULE3].get(regime.key, 0) + 1
            _add(samples[RULE3], forward)
            _track(by_regime_samples[RULE3], regime.key, forward)

        # ---- 规则 4：行业 ETF 极端流入（对各行业 ETF 分别判定）----
        for code in industry_codes:
            rows = etf_rows.get(code) or []
            at = _index_at(rows, day)
            if at is None or at < cfg.rule4_window:
                continue
            history = _changes_5d(rows, at, cfg.rule4_window)
            current = _change_5d_at(rows, at)
            if current is None or len(history) < max(cfg.rule4_window // 4, 20):
                continue
            rank = sum(1 for value in history if value <= current) / len(history)
            if rank <= cfg.rule4_min_percentile:
                continue
            triggers[RULE4] += 1
            regimes[RULE4][regime.key] = regimes[RULE4].get(regime.key, 0) + 1
            _add(samples[RULE4], _forward(rows, at))
            _track(by_regime_samples[RULE4], regime.key, _forward(rows, at))
        cursor += 1

    report.days = total - cursor + (cursor - (REGIME_WINDOW + cfg.rule4_window))
    notes = {
        RULE1: ("需求原样：分位低 + 份额大增即 +15%。**必须看 by_regime** —— "
                "该形态在牛市/震荡市历史上是负收益，无条件加成会抬高错误时点的评分。"),
        RULE2: ("风险警示：触发后指数应当走弱。表中数值是**指数自身收益**，"
                "中位数为负才算方向正确。"),
        RULE3: ("风格切换判定用的是**沪深300指数**的后续收益，因此它回答的是"
                "「宽基流出+行业流入时，大盘接下来怎么走」，"
                "不代表流入前 3 的行业本身有超额。"),
        RULE4: ("行业 ETF 极端流入后的**该 ETF 自身**后续收益。"
                "中位数为负说明反转效应成立（规则 4 的抑制方向正确）。"),
    }
    for rule in (RULE1, RULE2, RULE3, RULE4):
        report.rules.append(RuleReport(
            rule=rule, label=RULE_LABELS.get(rule, rule),
            triggers=triggers[rule], by_regime=dict(regimes[rule]),
            stats={h: _stats(samples[rule][h], h) for h in HORIZONS},
            by_regime_stats={
                key: {h: _stats(rows[h], h) for h in HORIZONS}
                for key, rows in by_regime_samples[rule].items()},
            note=notes[rule]))
    report.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report.seconds = round(time.monotonic() - begun, 2)
    return report


# ==================================================================
# 数据准备
# ==================================================================


def _latest(store: Any) -> str:
    try:
        rows = store._read("SELECT MAX(trade_date) AS d FROM ml_etf")  # noqa: SLF001
        return str(rows[0]["d"] or "") if rows else ""
    except Exception:  # noqa: BLE001
        return ""


def _prepare_etf(bars: dict[str, list[dict[str, Any]]]
                 ) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for code, rows in (bars or {}).items():
        cleaned = [{"trade_date": str(row.get("trade_date") or ""),
                    "shares": row.get("shares"), "close": row.get("close")}
                   for row in rows]
        out[code] = [row for row in cleaned if row["trade_date"]]
    return out


def _upto(rows: Sequence[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("trade_date") or "") > day:
            break
        out.append(row)
    return out


def _group_codes(config: FlowConfig, key: str) -> list[str]:
    for group in config.groups:
        if group.key == key:
            return [spec.code for spec in group.etfs]
    return []


def _group_index(config: FlowConfig, key: str) -> str:
    for group in config.groups:
        if group.key == key and group.index:
            return group.index
    return ""


def _median_change(indicators: dict[str, EtfIndicator],
                   codes: Sequence[str]) -> float | None:
    values = [float(indicators[code].change_5d) for code in codes
              if code in indicators and indicators[code].change_5d is not None]
    if not values:
        return None
    values.sort()
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def _percentile(rows: Sequence[tuple[str, float]] | None, day: str,
                window: int) -> float | None:
    if not rows:
        return None
    closes = [close for date, close in rows if date <= day]
    if len(closes) < max(int(window), 5):
        return None
    tail = closes[-int(window):]
    current = tail[-1]
    return sum(1 for value in tail if value <= current) / len(tail)


def _index_at(rows: Sequence[Any] | None, day: str) -> int | None:
    for index, row in enumerate(rows or ()):
        key = row[0] if isinstance(row, tuple) else str(row.get("trade_date") or "")
        if key == day:
            return index
        if key > day:
            return None
    return None


def _forward(rows: Sequence[Any] | None, index: int
             ) -> dict[int, tuple[float, float]]:
    """`{horizon: (T收盘收益, T+1收盘收益)}`（窗口不足的 horizon 直接缺席）。"""
    series = [(row[1] if isinstance(row, tuple) else row.get("close"))
              for row in (rows or ())]
    base = series[index] if index < len(series) else None
    base_t1 = series[index + 1] if index + 1 < len(series) else None
    out: dict[int, tuple[float, float]] = {}
    if not base:
        return out
    for horizon in HORIZONS:
        target = index + horizon
        if target >= len(series):
            continue
        value = series[target]
        if not value:
            continue
        t1 = value / base_t1 - 1.0 if base_t1 else None
        out[horizon] = (value / base - 1.0, t1 if t1 is not None
                        else value / base - 1.0)
    return out


def _add(bucket: dict[int, list[tuple[float, float]]],
         forward: dict[int, tuple[float, float]]) -> None:
    for horizon, pair in forward.items():
        bucket[horizon].append(pair)


def _track(store: dict[str, dict[int, list[tuple[float, float]]]],
           regime_key: str, forward: dict[int, tuple[float, float]]) -> None:
    """把同一个样本同时记进"分环境"桶（与合并桶并存）。"""
    bucket = store.setdefault(regime_key, {h: [] for h in HORIZONS})
    _add(bucket, forward)


def _changes_5d(rows: Sequence[dict[str, Any]], index: int,
                window: int) -> list[float]:
    """`index` **之前** `window` 个交易日的 5 日份额变化（不含当日，避免泄漏）。"""
    out: list[float] = []
    start = max(index - int(window), 5)
    for at in range(start, index):
        value = _change_5d_at(rows, at)
        if value is not None:
            out.append(value)
    return out


def _change_5d_at(rows: Sequence[dict[str, Any]], index: int) -> float | None:
    if index < 5 or index >= len(rows):
        return None
    now = _f(rows[index].get("shares"))
    base = _f(rows[index - 5].get("shares"))
    if now is None or base is None or base <= 0:
        return None
    return now / base - 1.0


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


# ==================================================================
# 报告
# ==================================================================


def render(report: RegimeBacktestReport) -> str:
    lines = ["# ETF 环境调节层 · 四条规则回测", ""]
    lines.append(f"- 区间：`{report.range_start}` ~ `{report.range_end}`"
                 f"（{report.days} 个交易日）；耗时 {report.seconds:.1f} 秒")
    if report.error:
        lines.append(f"- ⚠️ **未跑完**：{report.error}")
    lines.append("")
    lines.append("> 收益基准是**指数/ETF 自身**，回答「这条规则对后市方向的判断力」，"
                 "不是「接进评分能提多少分」。`T+1收盘` 是实盘可成交口径"
                 "（份额次日 8:30 更新）。")
    lines.append("")
    for item in report.rules:
        lines.append(f"## {item.label}（`{item.rule}`）")
        lines.append("")
        lines.append(f"- 触发次数：**{item.triggers}**"
                     + ("；分环境 " + "、".join(
                         f"{REGIME_LABELS.get(k, k)} {v}"
                         for k, v in sorted(item.by_regime.items()))
                        if item.by_regime else ""))
        lines.append(f"- {item.note}")
        lines.append("")
        if not item.triggers:
            lines.append("（区间内未触发）")
            lines.append("")
            continue
        lines.append("| 观察期 | 样本 | T收盘中位数 | 胜率 | T+1中位数 | T+1胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for horizon in sorted(item.stats):
            stats = item.stats[horizon]
            if not stats.samples:
                continue
            lines.append(f"| T+{horizon} | {stats.samples} | "
                         f"{_pct(stats.median)} | {_pct(stats.win_rate)} | "
                         f"{_pct(stats.median_t1)} | {_pct(stats.win_rate_t1)} |")
        lines.append("")
        if item.by_regime_stats:
            lines.append("**分市场环境**（决定要不要给这条规则加环境条件）：")
            lines.append("")
            lines.append("| 环境 | 观察期 | 样本 | T收盘中位数 | 胜率 | "
                         "T+1中位数 | T+1胜率 |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for key in ("bull", "bear", "range"):
                rows = item.by_regime_stats.get(key)
                if not rows:
                    continue
                for horizon in sorted(rows):
                    stats = rows[horizon]
                    if not stats.samples:
                        continue
                    lines.append(
                        f"| {REGIME_LABELS.get(key, key)} | T+{horizon} | "
                        f"{stats.samples} | {_pct(stats.median)} | "
                        f"{_pct(stats.win_rate)} | {_pct(stats.median_t1)} | "
                        f"{_pct(stats.win_rate_t1)} |")
            lines.append("")
    if report.gaps:
        lines.append("## 数据缺口")
        lines.append("")
        for gap in report.gaps:
            lines.append(f"- {gap}")
        lines.append("")
    lines.append("> 本报告为量化统计结果，不构成投资建议。")
    return "\n".join(lines)


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

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass

    parser = argparse.ArgumentParser(description="ETF 环境调节规则回测")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--out", default="docs/ETF_REGIME_RULES_BACKTEST.md")
    args = parser.parse_args(argv)

    from pathlib import Path

    from src.mainline.config import load_config as load_main_config
    from src.mainline.datastore import MainlineDataStore

    store = MainlineDataStore(config=load_main_config())
    report = run(store, start=args.start, end=args.end,
                 progress=lambda text: print("  ", text, flush=True))
    if report.error:
        print("回测失败：", report.error)
        return 1
    report.markdown = render(report)
    target = Path(args.out)
    target.write_text(report.markdown, encoding="utf-8")
    print(f"已写入 {target}")
    for item in report.rules:
        latest = item.stats.get(max(HORIZONS)) if item.stats else None
        if latest is not None and latest.samples:
            print(f"  {item.rule} 触发 {item.triggers} 次，T+34 中位数 "
                  f"{_pct(latest.median)}，胜率 {_pct(latest.win_rate)}，"
                  f"T+1 中位数 {_pct(latest.median_t1)}")
        else:
            print(f"  {item.rule} 触发 {item.triggers} 次（样本不足）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = ["HORIZONS", "RegimeBacktestReport", "RuleReport", "RuleStats",
           "render", "run"]
