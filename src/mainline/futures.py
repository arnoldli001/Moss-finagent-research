"""主线挖掘：期货先行信号（需求五）。

## 这个子模块回答的问题

A 股板块的启动常常**先有商品价格的异动**：锂价先动、锂矿股后动；螺纹钢先动、
钢铁股后动。因此这里把 89 个品种（内盘商品 55 / 外盘 25 / 非期货 9）的异动
算成强度分，再通过映射表对应到 A 股板块，作为"可能的主线候选"提前提示。

## 四类信号（需求 5.4）

    一 价格动量       5 日收益的 60 日 z-score > 1.5，或 10 日收益 > 5%
    二 相关性跃升     期货与板块 20 日滚动相关从 <0.3 升到 >0.6
    三 持仓量异动     持仓量 5 日增幅 > 10% 且价格同步上行
    四 期限结构      近月-远月价差由贴水转升水

## 三个必须说清的工程边界

**1. 主力连续序列的"跳空"是假的。**
主力连续在换月那天会把新旧合约的价格拼在一起，产生一个**不存在的跳空**。
因此所有收益率都用 `pre_close`（由数据源给出的当日真实前收）计算，
而不是 `close[i] / close[i-1] - 1` —— 后者在换月日会凭空产生 3%~8% 的
"异动"，直接触发误报。`fut_daily` 给了 `pre_close`，必须用它。

**2. 期限结构需要全合约数据，默认降级并标注。**
`fut_daily(trade_date=...)` 一次返回当日全部合约（含各月份），据此才能算
近月/远月价差。本模块把它落到 `ml_future_curve`（一次调用一天）。
**没有这张表时该信号记 gap 而不是记 0** —— 用 0 表示"贴水转升水未发生"
与"我们没算"是两件事。

**3. 外盘品种没有本地行情源时如实记缺口。**
Tushare 的 `fut_daily` 只覆盖内盘。外盘（CBOT/COMEX/NYMEX/LME 等）需要
AKShare / 新浪外盘接口。映射表里已经把这些品种标注为 `kind: foreign`，
在缺少行情时它们的 `intensity` 为 0 且在 `gaps` 里列出，**不伪造价格**。

## 与主线挖掘面板的集成（需求 5.5）

    futures.dashboard() → FutureDashboard
        仪表盘（按 intensity 排序）/ 期股联动热力图 / 映射详情 / 期货告警
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.config import MainlineConfig, load_config, load_yaml_config
from src.mainline.datastore import MainlineDataStore
from src.mainline.models import (
    FutureAlert,
    FutureDashboard,
    FutureKind,
    FutureMapping,
    FutureSignal,
    FutureSignalKind,
    SignalLevel,
)
from src.mainline.scoring import (
    clamp,
    correlation,
    correlation_series,
    to_float,
    zscore,
)

logger = logging.getLogger(__name__)

#: 仪表盘强度分的分项权重（合计 1.0）。与 `six_dim.SUB_WEIGHTS` 同理，
#: 属于"模型内部怎么组合"而不是研究口径，因此不放进 YAML。
INTENSITY_WEIGHTS: dict[str, float] = {
    "momentum": 0.45, "open_interest": 0.25, "correlation": 0.20,
    "term_structure": 0.10,
}

#: 外盘品种在 Tushare 侧没有行情（只有内盘 `fut_daily`）。映射表里 kind=foreign
#: 的品种若本地也没有行情，就在这里统一说明，避免每条 gaps 重复一遍。
FOREIGN_GAP = ("外盘品种行情需 AKShare/新浪外盘接口，本地仓未接入时不计入强度"
               "（不伪造价格）")


@dataclass
class FuturesService:
    """期货先行信号服务（同步；API 层用 `asyncio.to_thread` 包）。"""

    config: MainlineConfig = field(default_factory=load_config)
    store: MainlineDataStore | None = None
    _mappings: list[FutureMapping] | None = field(default=None, init=False)
    _mapping_gap: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = MainlineDataStore(config=self.config)

    # ---------- 映射表 ----------

    @property
    def mappings(self) -> list[FutureMapping]:
        if self._mappings is None:
            self._mappings = self._load_mappings()
        return self._mappings

    @property
    def mapping_gap(self) -> str:
        if self._mappings is None:
            self._mappings = self._load_mappings()
        return self._mapping_gap

    def _load_mappings(self) -> list[FutureMapping]:
        """加载品种↔板块映射表（缺失时返回空表 + gap，不抛错）。"""
        name = self.config.futures.mapping_file
        raw = load_yaml_config(name)
        if raw is None:
            self._mapping_gap = f"期货映射表不可用：configs/{name}"
            return []
        out: list[FutureMapping] = []
        for item in raw.get("mappings") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("future_code") or "").strip()
            if not code:
                continue
            kind_raw = str(item.get("kind") or "domestic")
            kind = (FutureKind(kind_raw) if kind_raw in FutureKind._value2member_map_
                    else FutureKind.DOMESTIC)
            out.append(FutureMapping(
                future_code=code,
                future_name=str(item.get("future_name") or code),
                board_code=str(item.get("board_code") or ""),
                board_name=str(item.get("board_name") or ""),
                direction=str(item.get("direction") or "positive"),
                strength=int(to_float(item.get("strength"), 3) or 3),
                lead_days=int(to_float(item.get("lead_days"), 5) or 5),
                logic=str(item.get("logic") or ""),
                chain=str(item.get("chain") or ""),
                kind=kind))
        if not out:
            self._mapping_gap = f"期货映射表为空：configs/{name}"
        else:
            self._attach_stocks(out)
        return out

    @staticmethod
    def _attach_stocks(mappings: list[FutureMapping]) -> None:
        """把 `futures_stocks.py` 算出的"联动最强个股"挂到映射上。

        联动数据放在**独立文件** `configs/mainline_futures_stocks.yaml`
        （自动生成），不写回手工维护的映射表 —— 后者的逐条 `logic` 是人工写的
        研究判断，让脚本重写它等于把人工成果覆盖掉。

        文件不存在是**正常状态**（还没跑过 `--build`），此时 `stocks` 为空列表，
        接口与面板照常工作。
        """
        try:
            from src.mainline.futures_stocks import load_links

            links = load_links()
        except Exception as exc:  # noqa: BLE001 联动只是附加信息，读不到不影响映射
            logger.info("个股联动读取失败（映射仍可用）：%s",
                        brief(exc, BRIEF_TIGHT))
            return
        if not links:
            return
        for mapping in mappings:
            items = links.get(mapping.future_code) or []
            mapping.stocks = [item.to_dict() for item in items]

    def system_titles(self) -> dict[str, str]:
        """产业链 key → 中文名（仪表盘分组用）。"""
        raw = load_yaml_config(self.config.futures.mapping_file) or {}
        out: dict[str, str] = {}
        for item in raw.get("chains") or []:
            if isinstance(item, dict) and item.get("key"):
                out[str(item["key"])] = str(item.get("label") or item["key"])
        return out

    # ---------- 主入口 ----------

    def dashboard(self, trade_date: str = "", *, top: int = 89
                  ) -> FutureDashboard:
        """算全部品种的异动信号并组装仪表盘载荷。"""
        store = self.store
        assert store is not None
        gaps: list[str] = []
        if not self.config.futures.enabled:
            return FutureDashboard(gaps=["期货先行信号已在配置中关闭"])
        if self.mapping_gap:
            gaps.append(self.mapping_gap)
        codes = [item.future_code for item in self.mappings]
        if not codes:
            return FutureDashboard(gaps=gaps or ["映射表为空"])

        # ⚠️ 必须先把空日期解析成**本地最新有数据的那一天**再进 SQL。
        # 空串当成 `trade_date <= ''` 会一行都匹配不到，于是全部品种被判成
        # "本地无行情" —— 而这个结论看起来完全合理（外盘确实没接入），
        # 让人不会去怀疑是日期参数的问题。
        target_hint = trade_date or self._latest_bar_date() or ""
        bars = store.future_bars(codes, start=_shift(target_hint, -400),
                                 end=target_hint)
        have = [code for code in codes if bars.get(code)]
        missing = sorted(set(codes) - set(have))
        if missing:
            kinds = {item.future_code: item.kind for item in self.mappings}
            foreign = [code for code in missing
                       if kinds.get(code) is FutureKind.FOREIGN]
            other = [code for code in missing if code not in set(foreign)]
            if foreign:
                gaps.append(FOREIGN_GAP)
            if other:
                gaps.append(f"{len(other)} 个品种本地无行情："
                            + "、".join(other[:8]))
        if not have:
            return FutureDashboard(
                generated_at=_now(), trade_date=trade_date, gaps=gaps,
                disclaimer=self.config.disclaimer)

        board_names = sorted({item.board_name for item in self.mappings
                              if item.board_name})
        board_series = self._board_series(board_names, target_hint)
        if not board_series:
            gaps.append("本地缺少映射板块的指数数据，期股相关性无法计算")

        signals = self._signals(bars, board_series, target_hint)
        alerts = self._alerts(signals, gaps)
        matrix = self._correlation_matrix(signals, bars, board_series)
        target = signals[0].trade_date if signals else target_hint
        counts = {"domestic": 0, "foreign": 0, "non_futures": 0,
                  "alert": len(alerts)}
        for item in signals:
            counts[item.kind.value] = counts.get(item.kind.value, 0) + 1
        return FutureDashboard(
            generated_at=_now(), trade_date=target,
            signals=signals[:top], alerts=alerts, correlation=matrix,
            mappings=self.mappings, counts=counts, source_notes=[
                f"映射表 {len(self.mappings)} 条 / 有行情 {len(have)} 个品种"],
            gaps=gaps, disclaimer=self.config.disclaimer)

    # ---------- 信号计算 ----------

    def _signals(self, bars: dict[str, list[dict[str, float]]],
                 board_series: dict[str, Any], trade_date: str
                 ) -> list[FutureSignal]:
        cfg = self.config.futures
        by_code: dict[str, FutureMapping] = {}
        for item in self.mappings:
            by_code.setdefault(item.future_code, item)
        out: list[FutureSignal] = []
        for code in sorted(bars):
            rows = bars[code]
            if len(rows) < 30:
                continue
            mapping = by_code.get(code) or FutureMapping(future_code=code,
                                                         future_name=code)
            signal = FutureSignal(
                code=code, name=mapping.future_name, kind=mapping.kind,
                trade_date=str(rows[-1].get("trade_date") or trade_date),
                close=_opt(rows[-1].get("close")),
                chain=mapping.chain,
                boards=[item.board_name for item in self.mappings
                        if item.future_code == code and item.board_name])
            self._fill_metrics(signal, rows, cfg)
            self._fill_correlation(signal, rows, board_series, mapping, cfg)
            self._fill_term_structure(signal, code)
            signal.intensity, signal.kinds, signal.reasons, signal.level = (
                _judge(signal, cfg))
            out.append(signal)
        out.sort(key=lambda item: -item.intensity)
        return out

    def _fill_metrics(self, signal: FutureSignal,
                      rows: list[dict[str, float]], cfg: Any) -> None:
        """动量与持仓量：**全部用 `pre_close` 算日收益**（见模块文档的换月跳空）。"""
        closes = [_opt(row.get("close")) for row in rows]
        pres = [_opt(row.get("pre_close")) for row in rows]
        returns: list[float | None] = []
        for index in range(1, len(rows)):
            base = pres[index] or closes[index - 1]
            now = closes[index]
            if base and now:
                returns.append(now / base - 1.0)
            else:
                returns.append(None)
        clean = [item for item in returns if item is not None]
        if not clean:
            return
        signal.change_pct = (clean[-1] * 100.0) if clean else None
        for window, attr in ((5, "ret_5d"), (10, "ret_10d"), (20, "ret_20d")):
            if len(clean) >= window:
                value = 1.0
                for item in clean[-window:]:
                    value *= (1.0 + item)
                setattr(signal, attr, value - 1.0)
        window = max(int(cfg.momentum.zscore_window), 20)
        tail = clean[-window:]
        if len(tail) >= 20:
            # 5 日滚动收益序列 vs 其自身历史：`zscore` 在 std≈0 时返回 None，
            # 调用方据此不产生信号（横盘品种的"偏离 0 倍标准差"没有意义）
            series: list[float | None] = []
            for index in range(len(tail)):
                if index < 4:
                    series.append(None)
                    continue
                series.append(sum(tail[index - 4:index + 1]))
            values = [item for item in series if item is not None]
            if len(values) >= 20:
                scores = zscore(values)
                for item in reversed(scores):
                    if item is not None:
                        signal.z_5d = item
                        break
        oi = [_opt(row.get("oi")) for row in rows]
        oi_clean = [item for item in oi if item is not None]
        window_oi = max(int(cfg.open_interest.window), 1)
        if len(oi_clean) > window_oi and oi_clean[-1 - window_oi]:
            signal.oi_change_5d = (oi_clean[-1] / oi_clean[-1 - window_oi] - 1.0)

    def _fill_correlation(self, signal: FutureSignal,
                          rows: list[dict[str, float]],
                          board_series: dict[str, Any], mapping: FutureMapping,
                          cfg: Any) -> None:
        """期股 20 日滚动相关：记录当前值与"是否刚从低位跃升"。

        `signal.term_structure` 与相关性分开存放；跃升判定写在 `_judge` 里
        （需要前后两个窗口的值），这里只把当前相关系数算出来放进
        `signal.__dict__` 的私有槽 `_corr` 供判定使用。
        """
        name = mapping.board_name
        series = board_series.get(name)
        if series is None or not getattr(series, "bars", None):
            return
        board_closes = [bar.close for bar in series.bars if bar.close > 0]
        future_closes = [to_float(row.get("close")) for row in rows]
        size = min(len(board_closes), len(future_closes))
        if size < 30:
            return
        window = max(int(cfg.correlation.window), 10)
        left = _returns(board_closes[-size:])
        right = _returns(future_closes[-size:])
        values = correlation_series(left, right, window=window)
        signal.__dict__["_corr_series"] = values
        current = None
        for item in reversed(values):
            if item is not None:
                current = item
                break
        signal.__dict__["_corr"] = current

    def _fill_term_structure(self, signal: FutureSignal, code: str) -> None:
        """期限结构：从 `ml_future_curve` 读近月/远月价差（无表则记 None）。"""
        store = self.store
        if store is None:
            return
        rows = store._read(  # noqa: SLF001 单表两列只读
            "SELECT trade_date, near, far FROM ml_future_curve"
            " WHERE code = ? ORDER BY trade_date DESC LIMIT 6", (code,))
        if not rows:
            return
        latest = rows[0]
        near, far = _opt(latest["near"]), _opt(latest["far"])
        if near and far:
            signal.term_structure = (far - near) / near
            signal.__dict__["_term_prev"] = None
            if len(rows) > 1 and _opt(rows[1]["near"]) and _opt(rows[1]["far"]):
                prev = ((_opt(rows[1]["far"]) - _opt(rows[1]["near"]))
                        / _opt(rows[1]["near"]))
                signal.__dict__["_term_prev"] = prev

    # ---------- 告警 ----------

    def _alerts(self, signals: Sequence[FutureSignal],
                gaps: list[str]) -> list[FutureAlert]:
        cfg = self.config.futures.alerts
        out: list[FutureAlert] = []
        chains: dict[str, list[FutureSignal]] = {}
        for item in signals:
            if item.level is not SignalLevel.NONE:
                chains.setdefault(item.chain, []).append(item)
                out.append(FutureAlert(
                    date=item.trade_date, code=item.code, name=item.name,
                    level=item.level, title=_alert_title(item),
                    detail="；".join(item.reasons), boards=item.boards,
                    chain=item.chain, kinds=list(item.kinds)))
        # 产业链级异动：同链 N 个以上品种同时异动
        min_varieties = max(int(cfg.chain_min_varieties), 2)
        for chain, items in chains.items():
            if not chain or len(items) < min_varieties:
                continue
            names = "、".join(item.name for item in items[:6])
            out.append(FutureAlert(
                date=items[0].trade_date, code=f"chain:{chain}",
                name=f"{chain} 产业链", level=SignalLevel.STRONG,
                title=f"产业链级异动：{len(items)} 个品种同时异动",
                detail=f"{names}；对应板块："
                       + "、".join(sorted({b for i in items for b in i.boards}))[:120],
                boards=sorted({b for item in items for b in item.boards}),
                chain=chain,
                kinds=sorted({k for item in items for k in item.kinds},
                             key=lambda item: item.value)))
        order = {SignalLevel.STRONG: 0, SignalLevel.MEDIUM: 1,
                 SignalLevel.WEAK: 2, SignalLevel.NONE: 3}
        out.sort(key=lambda item: (order.get(item.level, 9), item.date, item.code))
        return out

    # ---------- 相关性矩阵 ----------

    def _correlation_matrix(self, signals: Sequence[FutureSignal],
                            bars: dict[str, list[dict[str, float]]],
                            board_series: dict[str, Any]
                            ) -> dict[str, dict[str, float]]:
        """期股联动热力图：`{期货代码: {板块名: 相关系数}}`（只算有映射的组合）。"""
        out: dict[str, dict[str, float]] = {}
        size = max(int(self.config.futures.correlation.window), 10)
        for mapping in self.mappings:
            rows = bars.get(mapping.future_code)
            series = board_series.get(mapping.board_name)
            if not rows or series is None or not getattr(series, "bars", None):
                continue
            board_closes = [bar.close for bar in series.bars if bar.close > 0]
            future_closes = [to_float(row.get("close")) for row in rows]
            count = min(len(board_closes), len(future_closes))
            if count < size + 1:
                continue
            value = correlation(_returns(board_closes[-count:])[-size:],
                                _returns(future_closes[-count:])[-size:],
                                min_samples=max(8, size // 2))
            if value is None:
                continue
            out.setdefault(mapping.future_code,
                           {})[mapping.board_name] = round(value, 4)
        return out

    # ---------- 板块指数 ----------

    def _latest_bar_date(self) -> str:
        """本地期货行情里最新的交易日（空日期参数的解析目标）。"""
        store = self.store
        if store is None:
            return ""
        rows = store._read(  # noqa: SLF001 单值只读
            "SELECT MAX(trade_date) AS d FROM ml_future")
        return str(rows[0]["d"] or "") if rows else ""

    def _board_series(self, names: Sequence[str], trade_date: str
                      ) -> dict[str, Any]:
        """按**板块名**取本地指数序列（映射表给的是名字）。"""
        store = self.store
        if store is None or not names:
            return {}
        catalog = store.boards()
        wanted = {item.name: item.code for item in catalog if item.name in set(names)}
        if not wanted:
            return {}
        series = store.board_bars(list(wanted.values()),
                                  start=_shift(trade_date, -400),
                                  end=trade_date)
        return {name: series.get(code) for name, code in wanted.items()
                if series.get(code)}

    # ---------- 季度校准（需求 5.3） ----------

    def calibrate(self, *, as_of: str = "") -> dict[str, Any]:
        """用过去 250 个交易日的滚动相关校准映射强度。

        校准结果**不覆盖**人工设定的 `strength`，只写进
        `calibrated_strength`（见 `models.FutureMapping` 的说明）——
        "人工口径被自动逻辑改掉"这件事必须永远可见。
        """
        cfg = self.config.futures.recalibration
        window = max(int(cfg.window_days), 60)
        store = self.store
        if store is None:
            return {"updated": 0, "gap": "无数据仓"}
        end = as_of or store.calendar("", "")[-1] if store.calendar("", "") else ""
        if not end:
            return {"updated": 0, "gap": "本地无交易日历"}
        start = _shift(end, -int(window * 1.6))
        names = sorted({item.board_name for item in self.mappings if item.board_name})
        board_series = self._board_series(names, end)
        codes = sorted({item.future_code for item in self.mappings})
        bars = store.future_bars(codes, start=start, end=end)
        rows: list[dict[str, Any]] = []
        for mapping in self.mappings:
            series = board_series.get(mapping.board_name)
            data = bars.get(mapping.future_code)
            if not data or series is None or not getattr(series, "bars", None):
                continue
            board_closes = [bar.close for bar in series.bars if bar.close > 0]
            future_closes = [to_float(row.get("close")) for row in data]
            count = min(len(board_closes), len(future_closes))
            if count < max(60, window // 3):
                continue
            value = correlation(_returns(board_closes[-count:])[-window:],
                                _returns(future_closes[-count:])[-window:],
                                min_samples=max(20, window // 4))
            if value is None:
                continue
            # 强度映射：|corr| 0→1 星、0.8+→5 星；方向为负时按反向传导记录
            stars = max(1, min(5, int(round(abs(value) * 5 + 0.5))))
            rows.append({"future_code": mapping.future_code,
                         "future_name": mapping.future_name,
                         "board_name": mapping.board_name,
                         "strength": mapping.strength,
                         "calibrated_strength": stars,
                         "correlation": value,
                         "calibrated_at": _now(),
                         "window_days": window})
        if not rows:
            return {"updated": 0, "gap": "没有可校准的映射（缺行情或板块指数）"}
        try:
            import asyncio

            repo = getattr(self, "repo", None)
            if repo is not None:
                asyncio.run(repo.save_calibrations(rows))
        except Exception as exc:  # noqa: BLE001
            logger.warning("映射校准落库失败：%s", brief(exc, BRIEF_TIGHT))
        return {"updated": len(rows), "window_days": window, "as_of": end,
                "rows": rows}


# ==================================================================
# 纯函数
# ==================================================================


def _returns(values: Sequence[Any]) -> list[float | None]:
    """价格序列 → 日收益序列（与入参等长，首元素为 None）。"""
    out: list[float | None] = [None]
    for index in range(1, len(values)):
        base = to_float(values[index - 1])
        now = to_float(values[index])
        out.append((now / base - 1.0) if base and now else None)
    return out


def _judge(signal: FutureSignal, cfg: Any
           ) -> tuple[float, list[FutureSignalKind], list[str], SignalLevel]:
    """按四类信号算强度分、命中的信号类型、理由与等级。"""
    kinds: list[FutureSignalKind] = []
    reasons: list[str] = []
    parts: dict[str, float] = {}

    z = to_float(signal.z_5d)
    ret10 = to_float(signal.ret_10d)
    momentum_hit = False
    if z is not None and z > float(cfg.momentum.zscore_trigger):
        momentum_hit = True
        reasons.append(f"5 日动量 z={z:.2f} > {cfg.momentum.zscore_trigger:g}")
    if ret10 is not None and ret10 > float(cfg.momentum.ret_10d_trigger):
        momentum_hit = True
        reasons.append(f"10 日涨幅 {ret10 * 100:.2f}% > "
                       f"{cfg.momentum.ret_10d_trigger * 100:g}%")
    if momentum_hit:
        kinds.append(FutureSignalKind.MOMENTUM)
    parts["momentum"] = (_normalize_z(z) if z is not None else 0.0)

    oi = to_float(signal.oi_change_5d)
    price_up = (to_float(signal.ret_5d) or 0.0) > 0
    oi_hit = (oi is not None and oi > float(cfg.open_interest.change_trigger)
              and price_up)
    if oi_hit:
        kinds.append(FutureSignalKind.OPEN_INTEREST)
        reasons.append(f"持仓量 5 日 {oi * 100:+.1f}% 且价格上行")
    parts["open_interest"] = (clamp(abs(oi) / max(
        float(cfg.open_interest.change_trigger), 1e-6) * 60.0)
        if oi is not None and price_up else 0.0)

    series = signal.__dict__.get("_corr_series") or []
    current = to_float(signal.__dict__.get("_corr"))
    jump = False
    if series and current is not None:
        lookback = max(int(cfg.correlation.lookback_days), 1)
        past = [item for item in series[-(lookback + 1):-1] if item is not None]
        if past and min(past) < float(cfg.correlation.low_threshold) \
                and current > float(cfg.correlation.high_threshold):
            jump = True
            kinds.append(FutureSignalKind.CORRELATION_JUMP)
            reasons.append(f"期股相关性由 {min(past):.2f} 跃升至 {current:.2f}")
    parts["correlation"] = (_normalize_z(current / max(
        float(cfg.correlation.high_threshold), 1e-6))
        if current is not None else 0.0)

    term = to_float(signal.term_structure)
    prev = to_float(signal.__dict__.get("_term_prev"))
    term_hit = term is not None and term > 0 and prev is not None and prev <= 0
    if term_hit:
        kinds.append(FutureSignalKind.TERM_STRUCTURE)
        reasons.append(f"期限结构由贴水 {prev * 100:+.2f}% 转升水 "
                       f"{term * 100:+.2f}%")
    parts["term_structure"] = (60.0 if term_hit else
                               (_normalize_z(term * 10) if term is not None
                                else 0.0))

    intensity = sum(parts.get(key, 0.0) * weight
                    for key, weight in INTENSITY_WEIGHTS.items())
    signal.gaps = []
    if term is None:
        signal.gaps.append("期限结构未计算（缺 ml_future_curve 全合约数据）")
    if current is None:
        signal.gaps.append("期股相关性未计算（缺映射板块指数或样本不足）")

    level = SignalLevel.NONE
    if jump or (momentum_hit and oi_hit):
        level = SignalLevel.STRONG
    elif momentum_hit or oi_hit:
        level = SignalLevel.MEDIUM
    elif intensity >= 30.0:
        level = SignalLevel.WEAK
    return clamp(intensity), kinds, reasons, level


def _normalize_z(value: Any, *, scale: float = 2.0) -> float:
    """把 z 或"相对门槛的倍数"折成 0-100（50 = 中性，tanh 平滑到边界）。"""
    number = to_float(value)
    if number is None:
        return 0.0
    return clamp(50.0 + 50.0 * math.tanh(number / max(scale, 1e-6)))


def _alert_title(signal: FutureSignal) -> str:
    kinds = set(signal.kinds)
    if FutureSignalKind.CORRELATION_JUMP in kinds:
        return "期股联动启动（相关性跃升）"
    if {FutureSignalKind.MOMENTUM, FutureSignalKind.OPEN_INTEREST} <= kinds:
        return "内盘品种 5 日涨幅与持仓量同步异动"
    if FutureSignalKind.MOMENTUM in kinds:
        return "价格动量异动"
    if FutureSignalKind.OPEN_INTEREST in kinds:
        return "持仓量异动"
    return "期限结构变化"


def _shift(stamp: str, days: int) -> str:
    base = stamp
    if not base:
        base = datetime.now().strftime("%Y%m%d")
    try:
        moment = datetime.strptime(base, "%Y%m%d")
    except (TypeError, ValueError):
        return base
    from datetime import timedelta

    return (moment + timedelta(days=int(days))).strftime("%Y%m%d")


def _opt(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return None if number != number else number
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


__all__ = [
    "FOREIGN_GAP",
    "INTENSITY_WEIGHTS",
    "FuturesService",
]
