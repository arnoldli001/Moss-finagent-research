"""集合竞价选股 —— 最近 N 个交易日的**事后正确率回测**。

用户问题（2026-09-22）：
    "根据集合竞价规则，帮我尽可能回测最近 10 个或 20 个交易日竞价选股入池的股，
     统计当天竞价买入，其收盘涨停率、日内实体涨幅。我要看下策略的正确率。"

## 口径与边界（先读这段，再看数）

"当天竞价买入" = 以该日 **9:25 集合竞价成交价**买入（即当日开盘价；已用
`auction_snapshot` 逐位核对：QMT 日线 `open` == 9:25 竞价成交价，见
`--verify` 输出）。于是两只收益口径都只用**当日日线**就能算：

    | 口径 | 公式 |
    |------|------|
    | 收盘涨停 | `close == 涨停价`（涨停价 = 交易所四舍五入规则：昨收 × (1+幅度)）|
    | 日内实体涨幅 | `(close / 竞价价 - 1) × 100%` —— 竞价买入、收盘卖出 |

## ⚠️ 哪些规则能重建，哪些不能

规则表（`rulebook.py`）里的否决分两层来源。**历史 9:15~9:25 分时序列**
（`auction_snapshot.series`）只有 4 天（0917/0918/0921/0922），所以：

**能重建（本脚本用）** —— 全部只依赖日线 + 流通股本：

    - 前置筛选 `_prefilter_gates`：剔非涨停 / 剔无涨跌幅限制 / 剔 ST /
      剔科创板 / 剔昨收 ≥ 45 元 / 剔昨收未站上 20 日均价 / 剔流通市值不在 (20, 150) 亿
    - bit 2  竞价低开 < min_open_gap_pct（竞价族豁免在重建里退化为"无豁免"）
    - bit 3  主板竞价 > 6%（含「昨日连板 > 2」豁免，该条**是**能重建的）
    - bit 11 昨日首板 + 竞价涨幅低 + 近期涨停少（近 N 次用可得的 20 日窗口近似）
    - bit 12 创业板昨日首板
    - bit 13 昨日首板 + 60/120/250 日均线压在 [昨收, 昨收×1.14]

**不能重建（本脚本不判，且**不**拿别的条件顶替）**：

    - bit 4  竞价量比上限 —— 需要竞价量；QMT 1 分钟线的 `0930` bar 就是竞价 bar，
             但本地缺口在用 `download_history_data` 补齐前不完整，故不参与
    - bit 5  强转弱（需要竞价快照的 pattern 字段）
    - bit 6  竞价形态「急剧下坠型」、bit 7「竞价抢跑」—— 需要 9:15~9:25 分时序列
    - bit 8/9 市场级环境闸门（炸板率、情绪周期）
    - bit 10 打分维度完整度 —— 需要完整打分链路
    - 排序（打分）—— `scoring.py` 的权重里有「承接强度 / 封流比 / 题材热度」等
             只有竞价快照才有的维度，无法重建

所以本回测给的是**规则集的近似复盘**，不是生产入池名单的精确复现。
为了让"近似的偏差有多大"可量化，`--tape-days` 会把 0917/0918/0921/0922
**真实入池票**（存在 `auction_pick` 里）逐只列出来对照。

## 用法

    .venv\\Scripts\\python.exe scripts\\backtest_auction.py --days 20
    .venv\\Scripts\\python.exe scripts\\backtest_auction.py --days 20 --verify
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.auction_select import config as auction_config  # noqa: E402
from src.auction_select.features import PATTERN_FALLING  # noqa: E402
from src.auction_select.local_source import LIMIT_UP_LOOKBACK_DAYS  # noqa: E402

logger = logging.getLogger("backtest_auction")

DB_PATH = ROOT / "data" / "moss_finagent.db"
OUT_DIR = ROOT / "data" / "backtest"

#: 涨停价容差：涨停价按分四舍五入，浮点比较留一点余量。
_LIMIT_TOL = 0.005
#: 本地重建时"近期涨停次数"的可用窗口。生产口径是 18 个交易日
#: （`veto_first_board_lookback_days`），但本回测还额外拿到 250 日均线，
#: 窗口本身没被截断 —— 见 `_reconstruct_notes` 的说明。
RECON_LIMIT_UP_WINDOW = LIMIT_UP_LOOKBACK_DAYS

#: 有 9:15~9:25 分时录像带（`auction_snapshot.series`）的交易日。
#: 只有这几天的"真实入池票"能和重建结果对照。
TAPE_DAYS = ("20260917", "20260918", "20260921", "20260922")

#: 各录像带日**当时实际生效**的选股范围。
#: 从 `auction_pick` 的「前置筛选剔除」原因文案里逐字复原（那些文案会把阈值写进
#: reason，例如"昨日收盘价 37.00 元 ≥ 37 元"），所以这不是猜的。
#:
#: 为什么必须按当天口径重建：拿今天的口径（45 元 / (20,150) 亿）去回测 0918，
#: 得到的候选池与当年真实入池票**根本不是同一批票**，正确率就没有可比性。
ERA_UNIVERSE: dict[str, dict[str, float]] = {
    "20260917": {"max_prev_close_price": 37.0,
                 "min_market_cap": 15e8, "max_market_cap": 110e8},
    "20260918": {"max_prev_close_price": 37.0,
                 "min_market_cap": 15e8, "max_market_cap": 110e8},
    "20260921": {"max_prev_close_price": 50.0,
                 "min_market_cap": 20e8, "max_market_cap": 150e8},
    "20260922": {"max_prev_close_price": 45.0,
                 "min_market_cap": 20e8, "max_market_cap": 150e8},
}


# ---------------------------------------------------------------- 基础工具


def _limit_rate(code: str) -> float:
    """按板块返回涨跌幅限制。"""
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def _limit_up_price(pre_close: float, code: str) -> float:
    """涨停价：交易所口径 = 昨收 × (1 + 幅度)，**四舍五入到分**。"""
    raw = Decimal(str(pre_close)) * (Decimal("1") + Decimal(str(_limit_rate(code))))
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _board(code: str) -> str:
    if code.startswith(("600", "601", "603", "605")) or \
       code.startswith(("000", "001", "002", "003")):
        return "主板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    return "其他"


def _is_main_board(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def _is_chinext(code: str) -> bool:
    return code.startswith(("300", "301"))


def _is_star(code: str) -> bool:
    return code.startswith(("688", "689"))


# ---------------------------------------------------------------- 数据装载


@dataclass
class DayView:
    """某一个交易日的全市场截面（只含本地重建需要的列）。"""

    trade_date: str
    prev_date: str
    frame: pd.DataFrame


@dataclass
class BacktestData:
    daily: pd.DataFrame                 # code, trade_date, open/high/low/close, volume, amount, pre_close
    float_shares: dict[str, float]      # code -> 流通股本（股）
    names: dict[str, str]               # code -> 证券名称（判 ST 用）
    trading_days: list[str] = field(default_factory=list)


def load_daily_from_warehouse(start: str, end: str | None = None) -> pd.DataFrame:
    """从本地量化仓库取**原始价**日线 + 官方涨停价 + 官方流通市值。

    为什么它当主源（而不是 QMT）：
      1. 仓库 `daily` 是 tushare 原始价，`stk_limit` 是**官方涨停价**
         —— 涨停判定不用自己按比例推，少一层口径风险；
      2. 实测仓库 `open` 与 QMT `open` 逐位一致（97,201 行 100% 相同），
         所以用仓库不会改变"竞价买入价"；
      3. **覆盖面是 QMT 比不了的**：`daily`/`daily_basic` 从 2006-01-04 起，
         `stk_limit`（官方涨停价）从 **2010-01-04** 起 —— 这就是长区间回测的
         真实起点（没有官方涨停价就算不出"收盘涨停率"）。
      4. 官方 `circ_mv` 比"流通股本 × 昨收"更准，能绕开 QMT 只给**当前**
         流通股本的问题（16 年回测里股本变化极大）。
    """
    from src.quant.warehouse import load_dataset

    daily, status = load_dataset(
        "daily", start=start,
        columns=["code", "trade_date", "open", "high", "low", "close",
                 "volume_lot", "amount", "pre_close"])
    if daily is None or not len(daily):
        raise RuntimeError("本地仓库 daily 取不到数据")
    limits, _ = load_dataset(
        "stk_limit", start=start, columns=["code", "trade_date", "up_limit"])
    basics, _ = load_dataset(
        "daily_basic", start=start,
        columns=["code", "trade_date", "circ_mv", "float_share"])
    frame = daily.copy()
    # ⚠️ 两个仓库陷阱（都会**静默**产出错数据）：
    #    ① 列名可能带引号（实测请求 `volume` 时返回的列名是 `"volume"`）；
    #    ② 请求一个**不存在**的列时，`load_dataset` 不报错，而是返回一列
    #       内容是列名字符串的占位列 —— 实测 `volume` 不存在（真名 `volume_lot`），
    #       于是 `pd.to_numeric` 全军覆没、`volume > 0` 静默全 False、整表清空。
    #       所以这里显式改名到 `volume`，并断言它真的是数值。
    frame.columns = [str(c).strip().strip('"').strip("'") for c in frame.columns]
    if "volume_lot" in frame.columns:
        frame = frame.rename(columns={"volume_lot": "volume"})
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    for extra, keys in ((limits, ["code", "trade_date"]), (basics, ["code", "trade_date"])):
        if extra is None or not len(extra):
            continue
        e = extra.copy()
        e.columns = [str(c).strip().strip('"').strip("'") for c in e.columns]
        e["trade_date"] = e["trade_date"].astype(str)
        e["code"] = e["code"].astype(str).str.zfill(6)
        frame = frame.merge(e, on=keys, how="left")
    if end:
        frame = frame[frame["trade_date"] <= str(end)]
    logger.info("仓库日线 %d 行（%s）", len(frame), status)
    return frame


def load_daily_from_qmt(start: str, end: str, codes: Iterable[str] | None = None
                        ) -> pd.DataFrame:
    """从 QMT 取日线（补仓库覆盖不到的最近几天）。

    ⚠️ 两个坑：
      1. 不要用 `download_history_data2`（回调式，实测 3 分钟一个 200 只分片都回不来）；
         批量 `get_market_data_ex` 直读实测全 A 5224 只只用 4.3 秒。
      2. **停牌日会留"沿用上一价、成交量为 0"的假 bar**（如 603175 连续 3 天
         `close=98.38 / volume=0`），必须按 `volume > 0` 剔掉，否则会把停牌价
         当成真实竞价价。
    """
    from xtquant import xtdata

    xtdata.connect()
    if codes is None:
        codes = xtdata.get_stock_list_in_sector("沪深A股")
    codes = [c for c in codes if c.split(".")[0][:1] in "036"]
    frames = []
    for i in range(0, len(codes), 600):
        chunk = list(codes[i:i + 600])
        data = xtdata.get_market_data_ex([], chunk, period="1d",
                                         start_time=start, end_time=end, count=-1)
        for full, df in (data or {}).items():
            if df is None or not len(df):
                continue
            sub = df[["open", "high", "low", "close", "volume", "amount",
                      "preClose"]].copy()
            sub = sub[sub["close"] == sub["close"]]          # 去掉 NaN 行
            sub = sub[sub["volume"] > 0]                     # 去掉停牌假 bar
            if not len(sub):
                continue
            sub["code"] = full.split(".")[0]
            sub["trade_date"] = [str(x)[:8] for x in sub.index]
            frames.append(sub.reset_index(drop=True))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.rename(columns={"preClose": "pre_close"})
    out["up_limit"] = np.nan                                 # 由 pre_close 现算
    return out


def merge_sources(warehouse: pd.DataFrame, qmt: pd.DataFrame,
                  cutover: str = "20260918") -> pd.DataFrame:
    """仓库（≤ cutover-1）为主，QMT 补 cutover 之后。

    两源在重叠日会交叉核对一次 —— 差异大就说明有一边不可信，要看得见。

    ⚠️ `trade_date` 必须**全程是字符串**。仓库那边的 `trade_date` 里混着
    int（sqlite 里存成整数），一旦混型，`concat` 会把整列强制成对象/整数，
    字符串比较就悄悄失灵 —— 实测表现是"合并后只剩 QMT 那几天 2990 行、
    逐日候选 0 只"，而不会报任何错。
    """
    for frame in (warehouse, qmt):
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["code"] = frame["code"].astype(str).str.zfill(6)
    w = warehouse[warehouse["trade_date"] < cutover].copy()
    q = qmt[qmt["trade_date"] >= cutover].copy()
    # 交叉核对：用仓库覆盖到的那几天比对 QMT 同日的 open（竞价价）
    overlap_w = warehouse[warehouse["trade_date"] >= "20260801"]
    overlap = overlap_w.merge(qmt, on=["code", "trade_date"],
                              how="inner", suffixes=("_w", "_q"))
    if len(overlap):
        diff = (overlap["open_w"] - overlap["open_q"]).abs()
        close_diff = (overlap["close_w"] - overlap["close_q"]).abs()
        print(f"      两源重叠 {len(overlap):,} 行：open 一致 "
              f"{100 * (diff < 1e-9).mean():.2f}%（最大 {diff.max():.4f}）、"
              f"close 一致 {100 * (close_diff < 1e-9).mean():.2f}%")
    merged = pd.concat([w, q], ignore_index=True)
    merged["trade_date"] = merged["trade_date"].astype(str)
    merged["code"] = merged["code"].astype(str).str.zfill(6)
    # ⚠️ 仓库的 `volume` 列是**字符串**（sqlite 里建的 TEXT 列）。不转数值的话
    #    `volume > 0` 会静默全 False，把整张表清空 —— 而且不报任何错。
    for col in ("open", "high", "low", "close", "volume", "amount", "pre_close"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")
    n_vol = int(merged["volume"].notna().sum()) if "volume" in merged.columns else 0
    if n_vol < len(merged):
        raise RuntimeError(
            f"成交量列有 {len(merged) - n_vol} 行不是数值 —— 大概率是列名不对"
            "（仓库真名是 volume_lot，请求不存在的列会得到字符串占位列）")
    merged = merged[merged["open"] == merged["open"]]
    merged = merged[merged["close"] == merged["close"]]
    merged = merged[merged["volume"].fillna(0) > 0]
    if not len(merged):
        raise RuntimeError("合并后日线为空 —— 检查 volume 列类型与日期过滤")
    return merged.sort_values(["code", "trade_date"]).reset_index(drop=True)


def load_meta_from_qmt(codes: Iterable[str]) -> tuple[dict[str, float], dict[str, str]]:
    """流通股本（股）与证券名称。名称用来判 ST。"""
    from xtquant import xtdata

    xtdata.connect()
    shares: dict[str, float] = {}
    names: dict[str, str] = {}
    for full in codes:
        code = full.split(".")[0]
        try:
            d = xtdata.get_instrument_detail(full) or {}
        except Exception:                                    # noqa: BLE001
            continue
        fv = d.get("FloatVolume")
        if fv:
            shares[code] = float(fv)
        nm = d.get("InstrumentName")
        if nm:
            names[code] = str(nm)
    return shares, names


def load_trading_days(daily: pd.DataFrame) -> list[str]:
    days = sorted({str(d) for d in daily["trade_date"].unique()})
    return days


# ---------------------------------------------------------------- 规则重建


@dataclass
class Pick:
    trade_date: str
    code: str
    name: str
    board: str
    prev_close: float
    auction_price: float
    open_gap_pct: float
    close: float
    limit_up_price: float
    up_limit_close: bool
    intraday_pct: float
    prev_streak: float
    limit_up_days: int
    ma20: float
    ma60: float
    ma120: float
    ma250: float
    circ_mv: float
    tags: tuple[str, ...]

    def as_row(self) -> dict[str, Any]:
        return {
            "交易日": self.trade_date,
            "代码": self.code,
            "名称": self.name,
            "板块": self.board,
            "昨收": round(self.prev_close, 2),
            "竞价价": round(self.auction_price, 2),
            "竞价涨幅%": round(self.open_gap_pct, 2),
            "收盘": round(self.close, 2),
            "涨停价": round(self.limit_up_price, 2),
            "收盘涨停": "是" if self.up_limit_close else "否",
            "日内实体涨幅%": round(self.intraday_pct, 2),
            "昨日连板": self.prev_streak,
            "近18日涨停": self.limit_up_days,
            "流通市值亿": round(self.circ_mv / 1e8, 1),
            "命中规则": "、".join(self.tags),
        }


def _streak_series(sealed: pd.Series) -> pd.Series:
    """每天的连板高度（当日收盘涨停 → 往前连续涨停天数）。

    用累加 + 打断处把累计值乘 0 的写法（避免 python 级循环）：
        h_i = (h_{i-1} + 1) × sealed_i
    """
    s = 0.0
    out = []
    for ok in sealed.to_numpy():
        s = (s + 1.0) * (1.0 if ok else 0.0)
        out.append(s)
    return pd.Series(out, index=sealed.index, dtype=float)


def build_features(daily: pd.DataFrame, float_shares: dict[str, float],
                   names: dict[str, str], cfg: Any) -> pd.DataFrame:
    """把日线摊平成"每 (code, trade_date) 一行"的特征表。

    所有"截至昨日"的列都通过 `shift(1)` 取，避免用到当日之后的信息。
    """
    df = daily.sort_values(["code", "trade_date"]).copy()
    df["limit_rate"] = df["code"].map(_limit_rate)
    df["board"] = df["code"].map(_board)
    df["is_main_board"] = df["code"].map(lambda c: 1.0 if _is_main_board(c) else 0.0)
    df["is_chinext"] = df["code"].map(lambda c: 1.0 if _is_chinext(c) else 0.0)
    df["is_star"] = df["code"].map(lambda c: 1.0 if _is_star(c) else 0.0)
    df["limit_up_price"] = [
        _limit_up_price(pc, c) if pc == pc else np.nan
        for pc, c in zip(df["pre_close"], df["code"])
    ]
    if "up_limit" in df.columns:
        # 仓库的官方涨停价优先；QMT 段没有，用自算值补上。
        official = pd.to_numeric(df["up_limit"], errors="coerce")
        used = official.where(official == official, df["limit_up_price"])
        n_off = int((official == official).sum())
        mismatch = (official - df["limit_up_price"]).abs()
        n_bad = int(((mismatch > 1e-6) & (official == official)).sum())
        logger.info("涨停价：官方 %d 行、自算回退 %d 行、两者不符 %d 行",
                    n_off, len(df) - n_off, n_bad)
        df["limit_up_price"] = used
    df["sealed"] = (df["close"] - df["limit_up_price"]).abs() < _LIMIT_TOL
    df["sealed_i"] = df["sealed"].astype(int)

    g = df.groupby("code", sort=False)
    # 昨日连板高度：昨日收盘涨停则 ≥1，往前连续。
    # 先在已按 (code, trade_date) 排序的帧上算「当日连板高度」，再整体 shift(1)。
    df["streak_raw"] = g["sealed"].transform(_streak_series)
    g = df.groupby("code", sort=False)
    # 昨日口径列
    df["prev_close"] = g["close"].shift(1)
    df["prev_open"] = g["open"].shift(1)
    df["prev_volume"] = g["volume"].shift(1)
    df["prev_streak"] = g["streak_raw"].shift(1)
    df["prev_sealed"] = g["sealed_i"].shift(1)
    df["prev_trade_date"] = g["trade_date"].shift(1)
    df["prev_suspended"] = g["suspended"].shift(1) if "suspended" in df.columns else 0
    # 次日收盘：今天 9:25 竞价买入、**持有到明天收盘**卖（回答"当天不卖会怎样"）
    df["next_close"] = g["close"].shift(-1)
    # 均线（截至**当日**收盘 → 判昨日用的是"截至昨日"那根，见下面 shift）
    for win in (20, 60, 120, 250):
        df[f"cma{win}"] = g["close"].transform(
            lambda s, w=win: s.rolling(w, min_periods=w).mean())
        df[f"ma{win}"] = df.groupby("code", sort=False)[f"cma{win}"].shift(1)
    # 近 N 个交易日涨停次数（**截至昨日**，含昨日）
    df["limit_up_days"] = g["sealed_i"].transform(
        lambda s: s.rolling(RECON_LIMIT_UP_WINDOW, min_periods=RECON_LIMIT_UP_WINDOW)
                   .sum()).groupby(df["code"]).shift(1)

    # 流通市值：优先用**官方 circ_mv**（它逐日变化，16 年回测里股本变化极大，
    # 拿 QMT 的"当前流通股本"倒推会严重失真）；缺的时候退回
    # 「流通股本 × 昨日收盘价」（与生产口径 `prev_close_cap` 同源）。
    derived_mv = df["code"].map(float_shares) * df["prev_close"]
    if "circ_mv" in df.columns:
        official_mv = pd.to_numeric(df["circ_mv"], errors="coerce")
        gap = (official_mv - derived_mv).abs() / official_mv.replace(0, np.nan)
        sane = (official_mv > 0) & (gap < 0.5)          # 与倒推值差 50% 以上视为脏
        df["circ_mv"] = official_mv.where(sane, derived_mv)
        logger.info("流通市值：官方 %d 行、倒推回退 %d 行",
                    int(sane.sum()), int((~sane).sum()))
    else:
        df["circ_mv"] = derived_mv
    df["name"] = df["code"].map(names).fillna("") if names else ""
    if "is_st" in df.columns:
        # 调用方已经算好了时点化的 ST（如全历史回测用"官方涨停价限幅比例"反推），
        # 就不要用**当前名称**去覆盖它 —— 名称只有当前值，判历史会全错。
        df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(int)
    else:
        df["is_st"] = df["name"].str.contains("ST").astype(int)

    # —— 竞价派生量 ——
    df["auction_price"] = df["open"]                     # 9:25 竞价成交价 == 当日开盘价
    df["open_gap_pct"] = (df["open"] / df["prev_close"] - 1) * 100
    df["intraday_pct"] = (df["close"] / df["auction_price"] - 1) * 100
    df["next_day_pct"] = (df["next_close"] / df["auction_price"] - 1) * 100
    df["up_limit_close"] = df["sealed"]
    return df


@dataclass
class RuleOutcome:
    kept: pd.DataFrame
    dropped: dict[str, int]
    drop_examples: dict[str, list[str]] = field(default_factory=dict)


def apply_prefilter(day: pd.DataFrame, cfg: Any) -> RuleOutcome:
    """前置筛选：与 `rulebook._prefilter_gates` 逐条对齐。"""
    uni = cfg.universe
    frame = day
    dropped: dict[str, int] = {}
    examples: dict[str, list[str]] = {}

    def _drop(reason: str, mask: pd.Series) -> None:
        nonlocal frame
        hit = frame[mask]
        if len(hit):
            dropped[reason] = dropped.get(reason, 0) + len(hit)
            examples.setdefault(reason, []).extend(hit["code"].head(5).tolist())
        frame = frame[~mask]

    if uni.require_yesterday_limit_up:
        _drop("剔非涨停", ~(frame["prev_sealed"] == 1))
    if uni.exclude_no_price_limit:
        # 新股上市初期无涨跌幅限制：以"上市满 5 个交易日"近似（日线根数）
        _drop("剔无涨跌幅限制", frame["bar_no"] < 5)
    if uni.exclude_st:
        _drop("剔 ST", frame["is_st"] == 1)
    if getattr(uni, "exclude_star", True):
        _drop("剔科创板", frame["is_star"] == 1)
    _drop("剔无昨日收盘价", frame["prev_close"].isna())
    _drop(f"剔昨收 ≥ {uni.max_prev_close_price:g} 元",
          frame["prev_close"] >= uni.max_prev_close_price)
    if uni.require_above_ma:
        ma_col = f"ma{uni.ma_window}"
        _drop(f"剔无 {uni.ma_window} 日均价", frame[ma_col].isna())
        _drop(f"剔昨收未站上 {uni.ma_window} 日均价",
              frame["prev_close"] <= frame[ma_col])
    _drop("剔无流通股本", frame["circ_mv"].isna())
    _drop(f"剔流通市值(昨收口径)不在({uni.min_market_cap/1e8:.0f}, "
          f"{uni.max_market_cap/1e8:.0f}) 亿",
          (frame["circ_mv"] <= uni.min_market_cap)
          | (frame["circ_mv"] >= uni.max_market_cap))
    return RuleOutcome(kept=frame, dropped=dropped, drop_examples=examples)


def apply_vetoes(day: pd.DataFrame, cfg: Any
                 ) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    """能重建的那些 bit。返回 (未否决, 各条命中数, 判不了的行数)。

    哪些 bit 能重建、哪些不能，取决于**这一天的数据到哪一层**（见
    `build_auction_dataset.py` 的分层）：

    ┌─────┬──────────────────────────────┬──────────────────────────┐
    │ bit │ 规则                         │ 需要的数据层             │
    ├─────┼──────────────────────────────┼──────────────────────────┤
    │ 2   │ 竞价低开 < 阈值              │ 日线（竞价价=开盘价）    │
    │ 3   │ 主板竞价 >6%（连板豁免）     │ 日线                     │
    │ 11  │ 首板+竞价低开+近期涨停少     │ 日线 + 涨停次数          │
    │ 12  │ 创业板昨日首板               │ 日线                     │
    │ 13  │ 首板+均线压力位              │ 日线                     │
    │ 4   │ 竞价量比超上限（抢筹豁免）   │ 1 分钟线（竞价摘要）     │
    │ 6   │ 竞价形态「急剧下坠型」       │ tick（竞价过程）         │
    │ 7   │ 竞价抢跑                     │ tick（竞价过程）         │
    └─────┴──────────────────────────────┴──────────────────────────┘

    ⚠️ bit4/6/7 **只在当天有过程数据时才判**；没有就**留空不猜**
    （既不算命中也不算通过），并把这一天记进 `unjudgeable` 计数。
    这一点是本脚本最容易被误读的地方：**"没判"不等于"通过"**。
    """
    vet = cfg.veto
    mask_alive = pd.Series(True, index=day.index)
    hits: dict[str, int] = {}
    unjudgeable: dict[str, int] = {}

    def _veto(label: str, mask: pd.Series, *, only_if: pd.Series | None = None,
              judgeable: pd.Series | None = None) -> None:
        nonlocal mask_alive
        if judgeable is not None:
            n_un = int((~judgeable & mask_alive).sum())
            if n_un:
                unjudgeable[label] = unjudgeable.get(label, 0) + n_un
        scope = mask_alive if judgeable is None else (mask_alive & judgeable)
        m = mask & scope
        if m.any():
            hits[label] = int(m.sum())
        mask_alive &= ~m

    # bit 2：竞价低开 < 阈值（重建里不做竞价族豁免）
    _veto(f"bit2 竞价涨幅 < {vet.min_open_gap_pct}%",
          day["open_gap_pct"] < vet.min_open_gap_pct)

    # bit 3：主板竞价 > 6%，「昨日连板 > 2」豁免
    streak_exempt = float(getattr(vet, "main_board_exempt_min_streak", 2))
    _veto(f"bit3 主板竞价涨幅 > {vet.main_board_max_open_gap_pct:g}%"
          f"（昨日连板 > {streak_exempt:g} 豁免）",
          (day["is_main_board"] == 1)
          & (day["open_gap_pct"] > vet.main_board_max_open_gap_pct)
          & ~(day["prev_streak"] > streak_exempt))

    # bit 11：昨日首板 + 竞价涨幅 < 3% + 近 18 日涨停 < 4 次
    _gap = float(getattr(vet, "veto_first_board_max_open_gap_pct", 3))
    _days = int(getattr(vet, "veto_first_board_max_limit_up_days", 4))
    _streak = float(getattr(vet, "veto_first_board_streak", 1.0))
    _veto(f"bit11 昨日首板+竞价 < {_gap:g}%+近{RECON_LIMIT_UP_WINDOW}日涨停 < {_days} 次",
          (day["prev_streak"] == _streak)
          & (day["open_gap_pct"] < _gap)
          & (day["limit_up_days"] < _days))

    # bit 12：创业板昨日首板
    if getattr(vet, "veto_chinext_first_board", True):
        _veto("bit12 创业板昨日首板",
              (day["is_chinext"] == 1) & (day["prev_streak"] == 1))

    # bit 13：首板 + 60/120/250 日均线压在 [昨收, 昨收 × ratio]
    if getattr(vet, "veto_first_board_ma_pressure", True):
        ratio = float(getattr(vet, "veto_ma_pressure_ratio", 1.14))
        wins = tuple(int(w) for w in (getattr(vet, "veto_ma_pressure_windows", ()) or ()))
        zone = pd.Series(False, index=day.index)
        for w in wins:
            col = day.get(f"ma{w}")
            if col is None:
                continue
            zone |= (col >= day["prev_close"]) & (col <= day["prev_close"] * ratio)
        _veto(f"bit13 首板+均线压力位{list(wins)}", (day["prev_streak"] == 1) & zone)

    # ---- 以下三条需要**竞价过程**（tick）。没有过程数据的行不判、不猜 ----
    has_proc = (day["pattern"].notna() if "pattern" in day.columns
                else pd.Series(False, index=day.index))

    if "pattern" in day.columns:
        # bit6：竞价形态「急剧下坠型」→ 直接否决
        # ⚠️ 生产里「抢筹优先于下坠型」那一步已经在建库时做掉了
        #    （`process_features_for_day` 复刻了 service.py 的覆盖逻辑），
        #    所以这里的 pattern 已经是"最终形态"。
        _veto("bit6 竞价形态「急剧下坠型」",
              (day["pattern"] == PATTERN_FALLING), judgeable=has_proc)

    if "jump_value" in day.columns:
        hard = float(getattr(cfg.features, "rush_out_hard_jump", 0.97))
        down = float(getattr(cfg.features, "rush_jump_down", 0.99))
        out_pct = float(getattr(cfg.features, "rush_out_volume_pct", 10.0))
        exempt_streak = float(getattr(cfg.features,
                                      "rush_out_exempt_max_streak", 1.0))
        exempt_vs = float(getattr(cfg.features,
                                  "rush_out_exempt_min_vs_yesterday", 2.0))
        exempt_gap = float(getattr(cfg.features,
                                   "rush_out_exempt_min_gap_pct", 1.0))
        jump = day["jump_value"]
        ratio = day.get("auction_volume_ratio")
        vs = day.get("auction_vs_yesterday")
        if ratio is None:
            ratio = pd.Series(np.nan, index=day.index)
        if vs is None:
            vs = pd.Series(np.nan, index=day.index)
        # 硬否决档：跳空 < 0.97 —— 不看竞价量比、不受任何豁免
        _veto(f"bit7 抢跑（硬线 跳空 < {hard:g}，无豁免）",
              (jump < hard), judgeable=has_proc)
        # 常规档：跳空 < 0.99 且竞价量比 > 10%，三条豁免同时成立才豁免
        exempt = ((day["prev_streak"] > exempt_streak)
                  & (vs < exempt_vs)
                  & (day["open_gap_pct"] > exempt_gap))
        _veto(f"bit7 抢跑（常规档 跳空 < {down:g} 且 量比 > {out_pct:g}%）",
              (jump < down) & (ratio > out_pct) & ~exempt.fillna(False),
              judgeable=has_proc)
        # bit4：竞价量比超上限（「抢筹/大量抢筹」豁免）
        max_ratio = float(getattr(vet, "max_auction_volume_ratio", 15.0))
        labels = (day["rush_labels"].fillna("") if "rush_labels" in day.columns
                  else pd.Series("", index=day.index))
        positive = labels.str.contains("抢筹")
        _veto(f"bit4 竞价量比 > {max_ratio:g}%（抢筹豁免）",
              (ratio > max_ratio) & ~positive, judgeable=has_proc)

    return day[mask_alive], hits, unjudgeable


def select_for_day(day: pd.DataFrame, cfg: Any) -> tuple[pd.DataFrame, RuleOutcome, dict[str, int]]:
    """一天的选股结果：前置筛选 → 可重建否决 → 按竞价强度排序。

    **不在这里截断名次** —— 返回排序后的全池。因为要同时看"每日前 3"与
    "每日前 5"两个口径，而前 3 一定是前 5 的子集（同一排序键），
    在调用方截断可以保证两个口径**共用同一批候选**，不会出现两套口径
    各自取数导致的偏差。
    """
    pre = apply_prefilter(day, cfg)
    alive, hits, unjudgeable = apply_vetoes(pre.kept, cfg)
    alive = alive.copy()
    # 排序代理：竞价涨幅优先（生产里打分权重最大的可见维度就是"开盘位置"+"竞价量能"）
    alive = alive.sort_values("open_gap_pct", ascending=False)
    detail = {**hits, **{f"（判不了）{k}": v for k, v in unjudgeable.items()}}
    return alive, pre, detail


# ---------------------------------------------------------------- 统计与输出


def summarize(frame: pd.DataFrame, label: str) -> dict[str, Any]:
    if frame is None or not len(frame):
        return {"口径": label, "样本": 0}
    up = frame["up_limit_close"].astype(bool)
    out = {
        "口径": label,
        "样本": int(len(frame)),
        "收盘涨停数": int(up.sum()),
        "收盘涨停率%": round(float(up.mean()) * 100, 2),
        "日内实体涨幅均值%": round(float(frame["intraday_pct"].mean()), 2),
        "日内实体涨幅中位%": round(float(frame["intraday_pct"].median()), 2),
        "日内正收益占比%": round(float((frame["intraday_pct"] > 0).mean()) * 100, 2),
        "日内涨幅>5%占比%": round(float((frame["intraday_pct"] > 5).mean()) * 100, 2),
        "日内亏损>5%占比%": round(float((frame["intraday_pct"] < -5).mean()) * 100, 2),
        "最好%": round(float(frame["intraday_pct"].max()), 2),
        "最差%": round(float(frame["intraday_pct"].min()), 2),
    }
    if "next_day_pct" in frame.columns and frame["next_day_pct"].notna().any():
        nd = frame["next_day_pct"].dropna()
        out["次日收盘涨幅均值%"] = round(float(nd.mean()), 2)
        out["次日正收益占比%"] = round(float((nd > 0).mean()) * 100, 2)
    return out


def tape_picks(days: list[str]) -> pd.DataFrame:
    """已有录像带那几天的**真实入池票**（`auction_pick`）。

    ⚠️ `auction_pick` 表里**不只装入池票**：`decision` 有
    `buy / reject / watch / 前置筛选剔除` 四种，整表是这个交易日的全量判定结果。
    只取 `decision='buy'` 才是真正的入池名单 —— 取错会把落选票当成选中的票，
    正确率会算成完全不同的数（实测差了 2 倍多）。
    """
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(auction_pick)")}
        if not cols:
            return pd.DataFrame()
        want = [c for c in ("trade_date", "code", "name", "rank", "total_score",
                            "decision", "buy_reason", "open_gap_pct",
                            "auction_price", "open_price", "prev_close") if c in cols]
        rows = conn.execute(
            f"SELECT {', '.join(want)} FROM auction_pick "
            f"WHERE trade_date IN ({','.join('?' * len(days))}) "
            f"AND decision = 'buy' ORDER BY trade_date, rank", days).fetchall()
    finally:
        conn.close()
    return pd.DataFrame(rows, columns=want)


def attach_outcome(picks: pd.DataFrame, feat: pd.DataFrame) -> pd.DataFrame:
    """把真实入池票接上当日实际涨跌，算事后正确率。"""
    key = feat[["code", "trade_date", "open", "close", "limit_up_price",
                "intraday_pct", "up_limit_close", "prev_close"]].copy()
    key["code"] = key["code"].astype(str).str.zfill(6)
    key["trade_date"] = key["trade_date"].astype(str)
    p = picks.copy()
    p["code"] = p["code"].astype(str).str.zfill(6)
    p["trade_date"] = p["trade_date"].astype(str)
    merged = p.merge(key, on=["code", "trade_date"], how="left", suffixes=("", "_real"))
    # 竞价价一律以日线 `open` 为准（已与快照逐位核对）
    merged["竞价价"] = merged["open"]
    merged["日内实体涨幅%"] = merged["intraday_pct"]
    merged["收盘涨停"] = merged["up_limit_close"].map({True: "是", False: "否"})
    return merged


def top_n_by_day(frame: pd.DataFrame, top: int) -> pd.DataFrame:
    """每天取前 `top` 名（序号由 `select_for_day` 的排序决定）。"""
    if frame is None or not len(frame):
        return pd.DataFrame()
    return (frame.sort_values(["_day", "_rank"])
                 .groupby("_day", sort=False)
                 .head(top)
                 .reset_index(drop=True))


def format_group(name: str, frame: pd.DataFrame) -> dict[str, Any]:
    """一个分组的各项指标 —— 列名与 `summarize` 对齐，便于横向比较。"""
    if frame is None or not len(frame):
        return {"分组": name, "样本": 0}
    row = summarize(frame, name)
    row.pop("口径", None)
    out: dict[str, Any] = {"分组": name}
    out.update(row)
    out.pop("收盘涨停数", None)          # 分组表里"样本+涨停率"已经够用
    return out


#: 「昨日连板」的口径：**≥2 板**（首板单独一档，避免两档重叠）
GROUP_STREAK_MIN = 2
#: 连板高度逐档列到几板；更高的一档合并成「≥N+1 板」。
#: 实测样本里出现过 6 板，所以至少要到 6。
GROUP_STREAK_MAX = 6


def group_breakdown(frame: pd.DataFrame) -> pd.DataFrame:
    """把一个 Top-N 入池池拆成 主板 / 创业板 / 连板合计 / 各个连板高度档。

    口径说明：
      - **昨日连板** 取 `≥2 板`（首板另列一档）—— 入池票本来就是"昨日涨停"，
        若把连板定义成 ≥1 板，它会等于"全部入池"而与"昨日首板"完全重叠，
        两行看同一个数容易误读。所以这里让"首板"与"连板"互斥、相加为全体。
      - **连板高度逐档列出（1/2/3/4/5/6/≥7 板）**。只列到 3 板是不够的：
        实测 4 板 11 只、5 板 8 只、6 板 2 只都在样本里，而且它们的
        "涨停率很高、日内实体为负"特征比 3 板更极端（竞价高开 8%~9%）。
      - `≥7 板` 单独合并：主板 10% 涨跌幅下 7 板以上极罕见，样本通常是 0；
        但**保留这一行**，让"0 只"表现为"统计里确实没有"，
        而不是让人以为统计漏了。
    """
    if frame is None or not len(frame):
        return pd.DataFrame()
    streak = frame["prev_streak"]
    rows = [
        format_group("全部入池", frame),
        format_group("主板", frame[frame["is_main_board"] == 1]),
        format_group("创业板", frame[frame["is_chinext"] == 1]),
        format_group(f"昨日连板（≥{GROUP_STREAK_MIN}板）",
                     frame[streak >= GROUP_STREAK_MIN]),
    ]
    for level in range(1, GROUP_STREAK_MAX + 1):
        rows.append(format_group(f"　昨日{level}板（={level}板）",
                                 frame[streak == level]))
    rows.append(format_group(f"　昨日≥{GROUP_STREAK_MAX + 1}板",
                             frame[streak >= GROUP_STREAK_MAX + 1]))
    return pd.DataFrame(rows)


def format_detail(frame: pd.DataFrame) -> pd.DataFrame:
    """把内部列名翻成给人看的表头 —— 打印与 Excel 共用同一份，避免两处不一致。"""
    if frame is None or not len(frame):
        return pd.DataFrame()
    rows = []
    for r in frame.to_dict("records"):
        def _num(key: str, nd: int = 2) -> Any:
            v = r.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if f != f else round(f, nd)
        rows.append({
            "交易日": r.get("trade_date"),
            "代码": r.get("code"),
            "名称": r.get("name"),
            "板块": r.get("board"),
            "昨收": _num("prev_close"),
            "竞价价": _num("auction_price"),
            "竞价涨幅%": _num("open_gap_pct"),
            "收盘": _num("close"),
            "涨停价": _num("limit_up_price"),
            "收盘涨停": "是" if r.get("up_limit_close") else "否",
            "日内实体涨幅%": _num("intraday_pct"),
            "次日收盘涨幅%": _num("next_day_pct"),
            "昨日连板": _num("prev_streak", 0),
            "近18日涨停": r.get("limit_up_days"),
            "流通市值亿": (round(float(r["circ_mv"]) / 1e8, 1)
                           if r.get("circ_mv") == r.get("circ_mv") else None),
        })
    return pd.DataFrame(rows)


def align_with_golden(feat: pd.DataFrame, cfg: Any, trade_date: str = "20260918") -> None:
    """把重建的前置筛选结果与金标准计数对照。

    金标准来源：`tests/unit/test_auction_golden.py::test_prefilter_counts_match_golden`
    （2026-09-22 重锚）：0918 这天 `build_candidates` 保留 **36** 只，
    其中「剔昨收 ≥ 45 元」4 只、「剔昨收未站上 20 日均价」1 只、
    「剔流通市值不在 (20,150) 亿」6 只。

    这里**不是**要求逐位相等：金标准的输入是生产的预热数据（eltdx 竞价 +
    仓库日线），重建用的是 QMT 日线 + QMT 流通股本，两个数据源的
    `流通市值`/`均线` 本来就有微差。差异就是"近似口径的误差"，要看清有多少。
    """
    day = feat[feat["trade_date"] == trade_date]
    if not len(day):
        print(f"      （日线里没有 {trade_date}，无法对齐）")
        return
    out = apply_prefilter(day, cfg)
    cap = cfg.universe.max_prev_close_price
    print(f"      {trade_date} 前置筛选后保留 {len(out.kept)} 只（金标准 36）")
    print(f"      （当日昨日涨停候选 {int((day['prev_sealed'] == 1).sum())} 只）")
    for rule, n in sorted(out.dropped.items(), key=lambda kv: -kv[1]):
        print(f"        {rule}: {n}")
    want = {
        f"剔昨收 ≥ {cap:g} 元": 4,
        "剔昨收未站上 20 日均价": 1,
        "剔流通市值(昨收口径)不在(20, 150) 亿": 6,
    }
    for k, v in want.items():
        got = out.dropped.get(k, 0)
        flag = "✓" if got == v else "✗"
        print(f"      {flag} {k}: 重建 {got} / 金标准 {v}")
    print("      金标准的 3 只入池票（600371 / 600127 / 600354）在重建里的状态：")
    for code in ("600371", "600127", "600354"):
        row = out.kept[out.kept["code"] == code]
        if len(row):
            r = row.iloc[0]
            print(f"        {code} 保留（竞价 {r['open_gap_pct']:+.2f}%、"
                  f"昨收 {r['prev_close']:.2f}、流通市值 {r['circ_mv']/1e8:.1f} 亿）")
        else:
            hit = [k for k, v in out.drop_examples.items() if code in v]
            print(f"        {code} 被剔（{hit or '不在当日候选'}）")


def apply_universe_override(cfg: Any, day: str, era: str = "tape") -> Any:
    """按 `day` 当时的真实阈值覆盖选股范围。

    era="tape"（默认）：录像带日（0917~0922）用 `ERA_UNIVERSE` 里复原的历史阈值，
    其余日子用当前配置 —— 这样每一天都是"当天真实生效的口径"。
    era="now"：全部用当前配置（用于横向比较"新口径会选出什么"）。
    """
    if era == "now" or day not in ERA_UNIVERSE:
        return cfg
    import copy

    cfg = copy.deepcopy(cfg)
    for key, value in ERA_UNIVERSE[day].items():
        setattr(cfg.universe, key, value)
    return cfg


def verify_auction_price(feat: pd.DataFrame, sample: int = 30) -> None:
    """核对：QMT 日线 `open` == `auction_snapshot` 里的 9:25 竞价成交价。"""
    if not DB_PATH.exists():
        print("（无数据库，跳过核对）")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT trade_date, code, open_price, open_volume_hand FROM auction_snapshot "
            "ORDER BY trade_date DESC LIMIT ?", (sample,)).fetchall()
    finally:
        conn.close()
    if not rows:
        print("（无竞价快照，跳过核对）")
        return
    key = feat.set_index(["trade_date", "code"])
    ok = bad = miss = 0
    worst = 0.0
    for td, code, op, _vol in rows:
        try:
            row = key.loc[(str(td), str(code).zfill(6))]
        except KeyError:
            miss += 1
            continue
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if op is None or row["open"] != row["open"]:
            miss += 1
            continue
        diff = abs(float(row["open"]) - float(op))
        worst = max(worst, diff)
        if diff < 1e-6:
            ok += 1
        else:
            bad += 1
            print(f"  ✗ {td} {code}: 日线 open={row['open']} vs 快照 9:25={op}")
    print(f"竞价价核对：一致 {ok}、不一致 {bad}、无日线 {miss}（最大偏差 {worst:.6f}）")
    print("  → 一致即说明「竞价买入价 = 当日开盘价」这条口径成立")


def main() -> int:
    ap = argparse.ArgumentParser(description="集合竞价选股事后正确率回测")
    ap.add_argument("--days", type=int, default=20, help="回测最近多少个交易日")
    ap.add_argument("--end", default="", help="窗口最后一个交易日（默认=数据完整的最末日）")
    ap.add_argument("--tape-real", action="store_true", default=True,
                    help="把 0918~0922 的真实入池票单独作为对照（默认开）")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存，重新取数")
    ap.add_argument("--real-candidates", action="store_true",
                    help="用仓库官方涨停价定候选（与生产同源，最准）")
    ap.add_argument("--era", choices=("tape", "now"), default="tape",
                    help="选股范围口径：tape=按当日历史阈值重建（默认）、now=一律用当前配置")
    ap.add_argument("--top", type=int, default=5, help="每天取前几名（默认 5，与入池规模同量级）")
    ap.add_argument("--start", default="20250801", help="日线取数起点（要够算 250 日均线）")
    ap.add_argument("--long-start", default="20091201",
                    help="长区间回测的取数起点（默认 2009-12，覆盖官方涨停价起点 2010-01-04）")
    ap.add_argument("--full", action="store_true",
                    help="跑**全部可用历史**（2010-01-04 ~ 最新），而不是最近 N 个交易日")
    ap.add_argument("--verify", action="store_true", help="核对竞价价口径")
    ap.add_argument("--align", action="store_true",
                    help="与 tests/unit/test_auction_golden.py 的前置筛选计数对齐（0918）")
    ap.add_argument("--json", default="", help="把结果另存为 JSON")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # 控制台是 GBK 时，输出里的 ✓/✗ 会直接抛 UnicodeEncodeError —— 先兜住。
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:                                        # noqa: BLE001
        pass
    cfg = auction_config.load_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("集合竞价选股 —— 事后正确率回测")
    print("=" * 78)

    # ---------- 1. 数据 ----------
    # 长区间（--full / 显式 --start）用独立缓存与独立起点：仓库从 2010 起有
    # 官方涨停价，QMT 只补 0918 之后那几天（仓库覆盖到 0917）。
    span_start = args.long_start if args.full else args.start
    wh_cache = OUT_DIR / f"warehouse_daily_{span_start}.parquet"
    qmt_cache = OUT_DIR / "qmt_daily_20260918_20260922.parquet"
    if wh_cache.exists() and qmt_cache.exists() and not args.refresh:
        print(f"[1/5] 读日线缓存（仓库 + QMT）  {wh_cache.name}")
        wdf = pd.read_parquet(wh_cache)
        qdf = pd.read_parquet(qmt_cache)
    else:
        print(f"[1/5] 取日线：仓库（起点 {span_start}）+ QMT（补 0918 之后）…")
        wdf = load_daily_from_warehouse(span_start)
        wdf.to_parquet(wh_cache, index=False)
        qdf = load_daily_from_qmt(span_start, "20260922")
        qdf.to_parquet(qmt_cache, index=False)
    daily = merge_sources(wdf, qdf)
    print(f"      合并日线 {len(daily):,} 行 / {daily['code'].nunique():,} 只 / "
          f"{daily['trade_date'].min()}~{daily['trade_date'].max()}")
    per_day = daily.groupby("trade_date").size()
    tail = per_day.tail(6)
    print("      每日有效行数：" + "、".join(f"{d} {n}" for d, n in tail.items()))

    codes_full = sorted({f"{c}.SH" if c.startswith(("6", "9")) else f"{c}.SZ"
                         for c in daily["code"].unique()})
    meta_cache = OUT_DIR / "meta.json"
    if meta_cache.exists():
        meta = json.loads(meta_cache.read_text(encoding="utf-8"))
        float_shares = {k: float(v) for k, v in meta["float_shares"].items()}
        names = dict(meta["names"])
    else:
        print("      取流通股本/名称（QMT instrument_detail）…")
        float_shares, names = load_meta_from_qmt(codes_full)
        meta_cache.write_text(json.dumps(
            {"float_shares": float_shares, "names": names}, ensure_ascii=False),
            encoding="utf-8")
    print(f"      流通股本 {len(float_shares):,} 只 / 名称 {len(names):,} 只")

    # ---------- 2. 特征 ----------
    print("[2/5] 重建特征（连板高度 / 均线 / 近 18 日涨停 / 流通市值）…")
    daily = daily.copy()
    daily["bar_no"] = daily.groupby("code").cumcount()
    feat = build_features(daily, float_shares, names, cfg)
    days = load_trading_days(feat)

    # 数据完整度：QMT 本地库在 0918~0922 只有 ~1000 只（应约 5200），
    # 拿这些天做统计会把"整个市场只剩五分之一"混进正确率里。所以先判完整度，
    # 再默认把窗口收在最后一个**完整**交易日（除非显式 --end）。
    per_day_all = feat.groupby("trade_date").size()
    recent = per_day_all.tail(40)
    baseline = float(recent.median())
    complete = {d: (n >= baseline * 0.9) for d, n in per_day_all.items()}
    incomplete = [d for d in days[-8:] if not complete.get(d, True)]
    if incomplete:
        print(f"      ⚠ 数据不完整（不足 90%×{baseline:.0f}）："
              + "、".join(f"{d}({per_day_all[d]})" for d in incomplete))
    if args.end:
        end_day = str(args.end)
    else:
        end_day = next((d for d in reversed(days) if complete.get(d, True)), days[-1])
    end_idx = days.index(end_day)
    window = days[max(0, end_idx - int(args.days) + 1): end_idx + 1]
    print(f"      回测窗口 {window[0]} ~ {window[-1]}（{len(window)} 个交易日，"
          f"全部数据完整）")

    if args.verify:
        print("[核对] 竞价价口径")
        verify_auction_price(feat)

    if args.align:
        print("[核对] 与金标准前置筛选计数对齐（20260918）")
        align_with_golden(feat, cfg)

    # ---------- 3. 逐日选股 ----------
    print("[3/5] 逐日执行「前置筛选 + 可重建否决」…")
    # 基准线：不做任何筛选，"昨日涨停"这一整批今天的成绩是多少。
    # 没有这条线，"入池涨停率 53%" 是看不出策略有没有加分的。
    baseline_frames: list[pd.DataFrame] = []
    ranked_frames: list[pd.DataFrame] = []          # 每日排序后的全池（带 _rank）
    pool_frames: list[pd.DataFrame] = []
    day_rows: list[dict[str, Any]] = []
    for d in window:
        idx = days.index(d)
        prev = days[idx - 1] if idx else ""
        day = feat[feat["trade_date"] == d]
        day_cfg = apply_universe_override(cfg, d, args.era)
        raw_candidates = day[day["prev_sealed"] == 1]
        if len(raw_candidates):
            baseline_frames.append(raw_candidates.assign(_day=d))
        alive, pre, hits = select_for_day(day, day_cfg)
        if not len(pre.kept):
            day_rows.append({"交易日": d, "前日": prev, "前置筛选后": 0,
                             "否决后": 0, "入池": 0, "收盘涨停": 0,
                             "涨停率%": None, "日内实体均值%": None})
            continue
        pool_frames.append(alive.assign(_day=d))
        if len(alive):
            ranked = alive.reset_index(drop=True).copy()
            ranked["_day"] = d
            ranked["_rank"] = range(1, len(ranked) + 1)
            ranked_frames.append(ranked)
        # 逐日表固定按 args.top 口径展示
        selected = alive.head(int(args.top))
        day_rows.append({
            "交易日": d, "前日": prev,
            "候选(昨涨停)": int((day["prev_sealed"] == 1).sum()),
            "前置筛选后": int(len(pre.kept)),
            "否决后": int(len(alive)),
            "入池": int(len(selected)),
            "收盘涨停": int(selected["up_limit_close"].sum()) if len(selected) else 0,
            "涨停率%": (round(float(selected["up_limit_close"].mean()) * 100, 1)
                        if len(selected) else None),
            "日内实体均值%": (round(float(selected["intraday_pct"].mean()), 2)
                              if len(selected) else None),
        })
        top_note = "、".join(f"{r.code}{r.name}(竞价{r.open_gap_pct:+.1f}%"
                            f"→收盘{r.intraday_pct:+.1f}%)"
                            for r in selected.itertuples()) if len(selected) else "无"
        mark = "" if complete.get(d, True) else "  ⚠数据不全"
        print(f"      {d} 前置{len(pre.kept):>3} 否决后{len(alive):>3} "
              f"入池{len(selected):>2}{mark}")
        if len(selected):
            print(f"          {top_note}")

    day_table = pd.DataFrame(day_rows)
    ranked = (pd.concat(ranked_frames, ignore_index=True)
              if ranked_frames else pd.DataFrame())
    pool = pd.concat(pool_frames, ignore_index=True) if pool_frames else pd.DataFrame()
    baseline = (pd.concat(baseline_frames, ignore_index=True)
                if baseline_frames else pd.DataFrame())

    # ---------- 4. 统计 ----------
    print("[4/5] 统计")
    # 两个名次口径都从**同一批排序后的候选**里截断（见 select_for_day 的说明），
    # 所以「前 3」恒为「前 5」的子集，两个口径可以直接比。
    picks_top5 = top_n_by_day(ranked, 5)
    picks_top3 = top_n_by_day(ranked, 3)
    picks = picks_top5                       # 兼容下文既有引用

    stats: list[dict[str, Any]] = []
    stats.append(summarize(baseline, "【基准】昨日涨停全部（不筛，竞价买入）"))
    stats.append(summarize(picks_top5, "入池票（每日前 5，竞价买入）"))
    stats.append(summarize(picks_top3, "入池票（每日前 3，竞价买入）"))
    stats_table = pd.DataFrame(stats)

    groups_top5 = group_breakdown(picks_top5)
    groups_top3 = group_breakdown(picks_top3)

    # 真实入池票对照：录像带覆盖到的那几天（0917/0918/0921/0922）里、
    # **落在回测窗口内**的日期各自单独统计 —— 这是"生产口径实际选出来的票"，
    # 与重建口径的差异就是重建近似的误差，必须能看见。
    real = pd.DataFrame()
    real_rows: list[dict[str, Any]] = []
    tape_days = [d for d in days if d in TAPE_DAYS and d >= window[0]]
    if tape_days:
        raw = tape_picks(tape_days)
        if len(raw):
            real = attach_outcome(raw, feat)
            real = real[real["close"] == real["close"]]
            for d in tape_days:
                sub = real[real["trade_date"] == d]
                if len(sub):
                    row = summarize(sub, f"真实入池 {d}")
                    real_rows.append(row)
            real_rows.append(summarize(real, "真实入池 全窗口合计"))

    # ---------- 5. 输出 ----------
    print("[5/5] 落盘")
    print()
    print("─" * 78)
    print("【总览】")
    print(stats_table.to_string(index=False))
    if len(baseline) and len(picks_top5):
        b_up = float(baseline["up_limit_close"].astype(bool).mean()) * 100
        b_rt = float(baseline["intraday_pct"].mean())
        print()
        print("【与基准的差】")
        for label, frame in (("前 5", picks_top5), ("前 3", picks_top3)):
            if not len(frame):
                continue
            p_up = float(frame["up_limit_close"].astype(bool).mean()) * 100
            p_rt = float(frame["intraday_pct"].mean())
            print(f"  入池{label}：收盘涨停率 {p_up:.2f}%（不筛 {b_up:.2f}%，"
                  f"{p_up - b_up:+.2f}pt）、日内实体 {p_rt:+.2f}%"
                  f"（不筛 {b_rt:+.2f}%，{p_rt - b_rt:+.2f}pt）")

    # 竞价缺口诊断：这是回答"当天在 9:25 买是不是已经买贵了"的关键
    if len(baseline) and len(picks_top5):
        print()
        print("【竞价缺口诊断】")
        rows_gap = [("昨日涨停全体（基准）", baseline),
                    ("入池票 前 5", picks_top5),
                    ("入池票 前 3", picks_top3)]
        for label, frame in rows_gap:
            if not len(frame):
                continue
            prem = (frame["auction_price"] / frame["prev_close"] - 1) * 100
            print(f"  {label}：竞价相对昨收 均值 {prem.mean():+.2f}%、"
                  f"中位 {prem.median():+.2f}%（即 9:25 已高开的幅度）")
        print("  → 若「竞价缺口均值」明显大于 0 而「日内实体涨幅均值」≤ 0，")
        print("     说明这批票在 9:25 就已经把当日涨幅透支完了：不是选股不准，")
        print("     而是 9:25 这个买点本身偏贵（前面那段涨幅被让给了隔夜持仓者）。")
    # 重建保真度：重建入池 vs 生产真实入池（只对录像带日、且按当天历史阈值）
    if len(real) and len(picks_top5):
        print()
        print("【重建保真度：重建入池 vs 生产真实入池】")
        for d in tape_days:
            true_codes = set(real[real["trade_date"] == d]["code"].astype(str))
            for label, frame in (("前5", picks_top5), ("前3", picks_top3)):
                mine = set(frame[frame["trade_date"] == d]["code"].astype(str))
                if not true_codes and not mine:
                    continue
                inter = true_codes & mine
                hit = f"{len(inter)}/{len(true_codes)}" if true_codes else "—"
                print(f"  {d} [{label}]: 生产 {len(true_codes)} 只、"
                      f"重建 {len(mine)} 只、交集 {len(inter)} 只（覆盖率 {hit}）")
                if true_codes - mine:
                    print(f"      生产选中但重建没选："
                          f"{'、'.join(sorted(true_codes - mine))}")
                if mine - true_codes:
                    print(f"      重建选中但生产没选："
                          f"{'、'.join(sorted(mine - true_codes))}")
    if real_rows:
        real_summary = pd.DataFrame(real_rows)
        print()
        print("【真实入池票对照（auction_pick，仅录像带覆盖的交易日）】")
        print(real_summary.to_string(index=False))
        print()
        print("  ✅ 每日选股范围已按**当天历史阈值**重建（`ERA_UNIVERSE`，从")
        print("     `auction_pick` 的拒绝原因文案逐字复原），所以逐日数字可比。")
        print("  ⚠️ 但「真实入池票」那 13 只横跨 4 套不同门槛，只宜看趋势、不宜当结论。")
        keep = [c for c in ("trade_date", "code", "name", "rank", "total_score",
                            "日内实体涨幅%", "收盘涨停") if c in real.columns]
        print()
        for d in tape_days:
            sub = real[real["trade_date"] == d]
            if not len(sub):
                continue
            up = int(sub["up_limit_close"].astype(bool).sum())
            print(f"  {d}: {len(sub)} 只，收盘涨停 {up} 只（{up / len(sub) * 100:.1f}%），"
                  f"日内实体均值 {sub['intraday_pct'].mean():+.2f}%")
    print()
    print("【逐日】（口径：每日前 5）")
    print(day_table.to_string(index=False))

    for top_n, groups, frame in ((5, groups_top5, picks_top5),
                                 (3, groups_top3, picks_top3)):
        print()
        print("=" * 78)
        print(f"【入池票 每日前 {top_n} —— 分档统计】"
              f"（{len(frame)} 只；竞价买入、当日收盘卖）")
        print("=" * 78)
        if not len(groups):
            print("（窗口内没有入池票）")
            continue
        print(groups.to_string(index=False))

    print()
    print("【入池明细（每日前 5）】")
    detail = format_detail(picks_top5)
    if len(detail):
        print(detail.to_string(index=False))
    else:
        print("（窗口内没有入池票）")
    print()
    print("【入池明细（每日前 3）】")
    detail3 = format_detail(picks_top3)
    if len(detail3):
        print(detail3.to_string(index=False))
    else:
        print("（窗口内没有入池票）")

    out_xlsx = OUT_DIR / f"auction_backtest_{window[0]}_{window[-1]}_top{args.top}.xlsx"
    # ⚠️ Excel 开着的时候写盘会 PermissionError。不要静默失败：自动改写带时间戳的
    #    副本，并明确告诉用户原文件为什么没更新。
    try:
        with open(out_xlsx, "a+b"):
            pass
    except PermissionError:
        from datetime import datetime

        stamp = datetime.now().strftime("%H%M%S")
        alt = out_xlsx.with_name(f"{out_xlsx.stem}_{stamp}.xlsx")
        print(f"\n⚠️ {out_xlsx.name} 被占用（Excel 可能开着），改写为 {alt.name}")
        out_xlsx = alt
    try:
        with pd.ExcelWriter(out_xlsx, engine="openpyxl") as xw:
            stats_table.to_excel(xw, sheet_name="总览", index=False)
            day_table.to_excel(xw, sheet_name="逐日", index=False)
            # 分档统计：两个名次口径各一张
            if len(groups_top5):
                groups_top5.to_excel(xw, sheet_name="前5_分档统计", index=False)
            if len(groups_top3):
                groups_top3.to_excel(xw, sheet_name="前3_分档统计", index=False)
            for sheet, frame in (("前5_入池明细", detail),
                                 ("前3_入池明细", detail3),
                                 ("基准_昨日涨停全体", format_detail(baseline))):
                if not len(frame):
                    continue
                out = frame.copy()
                # ⚠️ 代码列必须写成**文本**，否则 Excel 会把 "002790" 读成数字 2790，
                #    代码一丢前导零就没法回查了。
                out["代码"] = out["代码"].astype(str).str.zfill(6)
                out["交易日"] = out["交易日"].astype(str)
                out.to_excel(xw, sheet_name=sheet, index=False)
                ws = xw.sheets[sheet]
                col = list(out.columns).index("代码") + 1
                for row in range(2, len(out) + 2):
                    ws.cell(row=row, column=col).number_format = "@"
            if len(real):
                r = real.copy()
                if "code" in r.columns:
                    r["code"] = r["code"].astype(str).str.zfill(6)
                r.to_excel(xw, sheet_name="真实入池票对照", index=False)
        print(f"\n已写出 Excel：{out_xlsx}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"\n（Excel 写出失败：{type(exc).__name__}: {exc}）")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "window": [window[0], window[-1]],
            "stats": stats,
            "groups_top5": groups_top5.to_dict("records") if len(groups_top5) else [],
            "groups_top3": groups_top3.to_dict("records") if len(groups_top3) else [],
            "per_day": day_rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出 JSON：{args.json}")

    print()
    print("─" * 78)
    print("口径边界（必读）：本回测用日线重建了前置筛选 + bit2/3/11/12/13；")
    print("bit4（竞价量比）/bit5（强转弱）/bit6（急剧下坠）/bit7（抢跑）/")
    print("bit8/9（市场闸门）/bit10（维度完整度）需要 9:15~9:25 分时序列或")
    print("完整打分链路，本地只有 0917/0918/0921/0922 四天有，故**未参与**；")
    print("排序用「竞价涨幅」作代理（生产打分含承接强度/封流比/题材热度）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
