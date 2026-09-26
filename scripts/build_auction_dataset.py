"""竞价选股 —— **统一数据集**（把散落的数据集中成 2 张表）。

## 为什么要集中

之前的数据散在五处：仓库日线、`minute_auction.parquet`、`tick_auction_<日期>.parquet`、
`candidates.parquet`、录像带 `auction_snapshot`。回测时要在它们之间反复 join，
而且**哪一天有哪一层**全靠人记，极易把"缺数据"当成"条件不满足"。

本模块把它们合成 **2 张表**：

    data/auction_hist/auction_dataset.parquet      ← 主表：一行 = 一个候选股日
    data/auction_hist/auction_series.parquet       ← 竞价分时序列（长表，仅 tick 覆盖日）

## 主表的列（一个候选股日的全部输入）

    ── 标识 ──
    trade_date, prev_date, code, board, is_st
    ── 日K基础（含上一交易日）──
    open/high/low/close/volume_lot/amount/pct_chg, pre_close, up_limit, down_limit,
    circ_mv, float_share, turnover_rate
    ── 竞价摘要（1 分钟线 `0930` bar，全年可得）──
    auction_price(9:25价), auction_volume_hand, auction_amount
    ── 量能比值 ──
    auction_volume_ratio     今竞价量 ÷ 昨日**全天**量 × 100
    auction_vs_yesterday     今竞价量 ÷ 昨日**竞价**量（倍数）
    ── 涨停历史（用户要求：前 18 个交易日涨停次数）──
    limit_up_days_8/18/20/60   窗口截至**上一交易日**
    ── 竞价过程特征（需 tick；无覆盖时整列留空，绝不猜）──
    jump_value, jump_points, jump_fallback
    pattern, pattern_shape, pattern_amplitude
    rush_tag, rush_labels
    process_available           该行有没有过程数据
    data_layers                 'daily+minute' / 'daily+minute+tick'

## 幂等

只依赖已落盘的 parquet 与仓库，可反复重跑（`--refresh` 重建）。
不联网、不碰 QMT。

## 用法

    .venv\\Scripts\\python.exe scripts/build_auction_dataset.py
    .venv\\Scripts\\python.exe scripts/build_auction_dataset.py --from 20250801
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("build_auction_dataset")

OUT = ROOT / "data" / "auction_hist"
DB = ROOT / "data" / "quant" / "warehouse.db"

#: 用户口径（2026-09-22）：市值 20~110 亿、昨收 < 45 元、非科创板/北证
MIN_MV, MAX_MV, MAX_PRICE = 20e8, 110e8, 45.0
#: 涨停次数窗口
LU_WINDOWS = (8, 18, 20, 60)
#: 竞价摘要可得的起点（QMT 1 分钟线实测）
MINUTE_START = "20250919"
#: 竞价过程可得的起点（QMT tick 实测）
PROCESS_START = "20260824"


# ------------------------------------------------------------------ 工具


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def trading_days(start: str, end: str) -> list[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM quant_daily "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start, end)).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def _board(code: str) -> str:
    c = str(code)
    if c.startswith(("600", "601", "603", "605")): return "沪主板"
    if c.startswith(("000", "001", "002", "003")): return "深主板"
    if c.startswith(("300", "301")): return "创业板"
    if c.startswith(("688", "689")): return "科创板"
    return "北证/其他"


def _st_from_limit(pre_close: pd.Series, up_limit: pd.Series) -> pd.Series:
    """用官方涨停价反推限幅比例判 ST（≈5% 即当年的 ST/*ST）。

    仓库目录只有**当前**名称，拿它判历史会全错（`000007` 现在叫"好上好"、
    当年叫"ST 零七"）。限幅是交易所当时的真实规则，逐日可查。
    """
    pc = pd.to_numeric(pre_close, errors="coerce")
    ul = pd.to_numeric(up_limit, errors="coerce")
    ratio = (ul / pc - 1) * 100
    return ((ratio >= 3.5) & (ratio <= 6.5)).fillna(False)


# ------------------------------------------------------------------ 竞价摘要


def auction_summary() -> pd.DataFrame:
    """从 `minute_auction.parquet` 取**竞价摘要**（9:25 价/量/额），全年可得。"""
    p = OUT / "minute_auction.parquet"
    if not p.exists():
        logger.warning("缺 %s，竞价摘要整层为空", p.name)
        return pd.DataFrame(columns=["trade_date", "code", "auction_price",
                                     "auction_volume_hand", "auction_amount"])
    m = pd.read_parquet(p)
    m["trade_date"] = m["trade_date"].astype(str)
    m["code"] = m["code"].astype(str).str.zfill(6)
    m["_bar"] = m["bar_time"].astype(str).str[8:12]
    au = m[m["_bar"] == "0930"].copy()          # 0930 bar = 竞价 bar
    au = au.rename(columns={"open": "auction_price", "volume": "auction_volume_hand",
                            "amount": "auction_amount"})
    keep = ["trade_date", "code", "auction_price", "auction_volume_hand",
            "auction_amount"]
    return au[keep].drop_duplicates(["trade_date", "code"]).reset_index(drop=True)


# ------------------------------------------------------------------ 竞价过程


def series_from_stored_tick(day: str, codes: set[str] | None = None
                            ) -> pd.DataFrame:
    """把已落盘的 tick 还原成竞价分时序列（与 `auction_snapshot.series` 同构）。

    `time` / `time_seconds` / `price` / `matched` / `unmatched`
    价格取 `bidPrice[0]`（竞价期间 `bid1 == ask1` 即撮合参考价；9:25 那笔取成交价）。

    ## ⚠️ `time_seconds` 必须用文件里存的，不要从 `time` 重算

    QMT 的 `time` 是 **epoch 毫秒（UTC 基准）**，而"当天秒数"要的是**本地**墙上时间。
    从 epoch 反推需要显式带时区；一旦写成 `(ms // 1000) % 86400` 就会拿到 UTC
    当天秒数，**比本地少 8 小时**（28800 秒）——09:15 会变成 01:15，
    `09:19:20~09:25:00` 窗口里一个点都取不到，形态判定全线退化成「未识别」。
    采集时已经写过正确的 `time_seconds`，所以**直接读那一列**；只有旧文件缺列时
    才用带时区的 datetime 反推。
    """
    p = OUT / f"tick_auction_{day}.parquet"
    if not p.exists():
        return pd.DataFrame()
    want = ["code", "time", "lastPrice", "bidPrice", "askPrice", "bidVol", "askVol"]
    head = pd.read_parquet(p, columns=None)
    have_ts = "time_seconds" in head.columns
    have_local = "time_local" in head.columns
    t = head[[c for c in want if c in head.columns]
             + (["time_seconds"] if have_ts else [])
             + (["time_local"] if have_local else [])].copy()
    del head
    if not len(t):
        return pd.DataFrame()
    t["code"] = t["code"].astype(str).str.zfill(6)
    if codes is not None:
        t = t[t["code"].isin(codes)]
    if not len(t):
        return pd.DataFrame()

    def _p0(x):
        try:
            return float(x[0]) if x is not None and len(x) else None
        except Exception:                                      # noqa: BLE001
            return None

    last = pd.to_numeric(t["lastPrice"], errors="coerce")
    bid = t["bidPrice"].map(_p0)
    ask = t["askPrice"].map(_p0)
    price = last.where(last > 0, bid.where(bid.notna() & (bid - ask).abs() < 1e-9,
                                           np.nan))
    if have_ts:
        secs = pd.to_numeric(t["time_seconds"], errors="coerce")
    else:                                                      # 旧文件：带时区反推
        ms = t["time"].astype("int64")
        loc = pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert(
            datetime.now().astimezone().tzinfo)
        secs = (loc.dt.hour * 3600 + loc.dt.minute * 60 + loc.dt.second)
    out = pd.DataFrame({
        "trade_date": day,
        "code": t["code"],
        "time_seconds": secs.astype("Int64"),
        "price": price,
        "matched": t["bidVol"].map(_p0),
        "unmatched": t["askVol"].map(_p0),
    })
    out = out[out["price"].notna() & (out["price"] > 0)
              & out["time_seconds"].notna()]
    out = out.sort_values(["code", "time_seconds"]).reset_index(drop=True)
    out["time"] = out["time_seconds"].astype("int64").map(
        lambda s: "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60))
    return out


def process_features_for_day(day: str, *, streaks: dict[str, float],
                             gap_pct: dict[str, float],
                             vs_yesterday: dict[str, float],
                             volume_ratio_pct: dict[str, float]) -> pd.DataFrame:
    """用**生产函数**逐字复算当日的竞价过程特征。

    对齐 `service.py` 的三处调用（不自己拼逻辑，避免两套口径漂移）：

        classify_pattern(points, min_points, volume_vs_yesterday,
                         prev_limit_up_streak, falling_min_ratio)
        jump_gap(points, match_price, start, end)
        rush_labels(jump, volume_vs_yesterday, volume_ratio_pct,
                    prev_limit_up_streak, open_gap_pct, ...)

    ⚠️ **`vs_yesterday`（今昨竞比）不能省** —— 它是「急剧下坠型」的成立条件之一
    （`streak == 1` 或 `今昨竞比 > 2.0`，见 `features.classify_pattern`）。
    传 `None` 会把豪尔赛那种"首板/连板 + 尾盘放量跳水"的票判成「台阶型」，
    于是 bit6 不命中 —— 实测踩过（002963 20260827：传 None → 台阶型、
    传真实值 4.407 → 急速下坠型）。
    """
    from src.auction_select import config as auction_config
    from src.auction_select import features as F

    cfg = auction_config.load_config()
    ser = series_from_stored_tick(day)
    if not len(ser):
        return pd.DataFrame()
    # ⚠️ 必须**只取 09:15:00~09:25:00** 这一段喂给生产函数。
    #    存档的 tick 覆盖 09:15~09:31（含 9:25 之后的连续竞价 09:30/0931 bar），
    #    如果拿"序列最后一点"当 9:25 撮合价，会拿到**连续竞价的价**，
    #    跳空值随之失真 —— 实测 002963 20260827：截到 9:25 → 跳空 0.9435（命中硬否决线）；
    #    不截 → 0.9618（刚好躲过 0.97），结论从"该拦"变成"不该拦"。
    #    生产的 `snapshot.series` 本来就只到 09:24:5x，这里对齐同一口径。
    auction_end = F.parse_time_seconds(cfg.universe.auction_end_time) or 33900
    ser = ser[ser["time_seconds"] <= auction_end]
    if not len(ser):
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for code, g in ser.groupby("code", sort=False):
        points = g[["time", "time_seconds", "price", "matched",
                    "unmatched"]].to_dict("records")
        # 9:25 撮合价 = 竞价段最后一个有效价（= 当日开盘价，已与录像带逐位核对）
        match_price = float(g["price"].iloc[-1])
        gap = gap_pct.get(code)
        vs_yest = vs_yesterday.get(code)
        ratio_pct = volume_ratio_pct.get(code)
        streak_now = streaks.get(code)

        pattern, pmeta = F.classify_pattern(
            points, min_points=cfg.features.min_pattern_points,
            volume_vs_yesterday=vs_yest, prev_limit_up_streak=streak_now,
            falling_min_ratio=cfg.veto.falling_min_volume_vs_yesterday)
        jump, jmeta = F.jump_gap(points, match_price=match_price,
                                 start=cfg.features.jump_window_start,
                                 end=cfg.features.jump_window_end)
        tag, labels, note = F.rush_labels(
            jump=jump, volume_vs_yesterday=vs_yest, volume_ratio_pct=ratio_pct,
            prev_limit_up_streak=streak_now, open_gap_pct=gap,
            up=cfg.features.rush_jump_up, down=cfg.features.rush_jump_down,
            min_ratio=cfg.features.rush_min_volume_vs_yesterday,
            heavy_pct=cfg.features.rush_heavy_volume_pct,
            out_pct=cfg.features.rush_out_volume_pct,
            hard_down=cfg.features.rush_out_hard_jump,
            exempt_max_streak=cfg.features.rush_out_exempt_max_streak,
            exempt_min_vs_yesterday=cfg.features.rush_out_exempt_min_vs_yesterday,
            exempt_min_gap_pct=cfg.features.rush_out_exempt_min_gap_pct)
        # 抢筹优先于「急剧下坠型」（service.py 的同一条覆盖，规则 1）
        shape = ""
        if pattern == F.PATTERN_FALLING and (set(labels) & set(F.RUSH_POSITIVE_TAGS)):
            shape = F.PATTERN_FALLING
            pattern = F.PATTERN_UNKNOWN
        rows.append({
            "trade_date": day, "code": code,
            "jump_value": None if jump is None else round(float(jump), 6),
            "jump_points": int(jmeta.get("points") or 0),
            "jump_fallback": bool(jmeta.get("fallback") or False),
            "jump_window_mean": jmeta.get("window_mean"),
            "pattern": pattern,
            "pattern_shape": shape or pmeta.get("shape") or "",
            "pattern_amplitude": pmeta.get("amplitude"),
            "pattern_points": int(pmeta.get("points") or 0),
            "rush_tag": tag,
            "rush_labels": "|".join(labels),
            "rush_note": note[:200],
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 主表


def raw_partition_days() -> list[str]:
    """tushare 分区文件里有的交易日（这些天即使仓库没入库，也是**原始价**可用）。"""
    d = ROOT / "data" / "quant" / "tushare" / "a_share" / "daily"
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


def _partition_fill(days: list[str]) -> pd.DataFrame:
    """从 tushare 分区读**原始价**日线（补仓库还没入库的交易日）。

    ⚠️ 为什么不用 `data/backtest/qmt_daily_*.parquet`：那份 QMT 缓存里
    20260918 之后是**复权价**（实测 `600051` 缓存昨收 6.10，而录像带记的当天
    9:25 竞价价是 7.17，比值 0.85），与仓库原始价不可比，拿它算涨停价会全错。
    分区文件是 tushare 原始价，schema 与仓库一致。
    """
    base = ROOT / "data" / "quant" / "tushare" / "a_share"
    frames = []
    for ds in ("daily", "daily_basic", "stk_limit"):
        for day in days:
            p = base / ds / f"{day}.parquet"
            if not p.exists():
                continue
            d = pd.read_parquet(p)
            d["trade_date"] = d["trade_date"].astype(str)
            d["code"] = d["code"].astype(str).str.zfill(6)
            if ds == "daily" and "volume_lot" in d.columns:
                d = d.rename(columns={"volume_lot": "volume"})
            frames.append((ds, d))
    if not frames:
        return pd.DataFrame()
    # ⚠️ `daily` 必须**把所有分区分天读进来再 concat**。曾经写成"找到第一个 daily
    #    就 break" → 只读了一天（实测：请求 2 天只回 5,209 行 = 1 天），
    #    而且因为行数看着正常、不报错，极难发现。
    daily = pd.concat([d for ds, d in frames if ds == "daily"], ignore_index=True)
    out = daily
    for ds in ("daily_basic", "stk_limit"):
        parts = [d for d2, d in frames if d2 == ds]
        if not parts:
            continue
        d = pd.concat(parts, ignore_index=True)
        cols = [c for c in ("trade_date", "code", "up_limit", "down_limit",
                            "circ_mv", "float_share", "turnover_rate", "total_mv")
                if c in d.columns]
        out = out.merge(d[cols], on=["trade_date", "code"], how="left")
    # 与仓库列对齐（仓库用 volume_lot / amount）
    if "volume" in out.columns and "volume_lot" not in out.columns:
        out = out.rename(columns={"volume": "volume_lot"})
    logger.warning("分区补 %s：%d 行 / %d 只", sorted(set(days)), len(out),
                   out["code"].nunique())
    return out


def _qmt_daily_days(start: str, end: str) -> list[str]:
    """QMT 日线缓存里落在 [start, end] 的交易日（升序）。"""
    p = ROOT / "data" / "backtest" / "qmt_daily_20260918_20260922.parquet"
    if not p.exists():
        return []
    q = pd.read_parquet(p, columns=["trade_date", "volume"])
    td = q["trade_date"].astype(str)
    keep = (td >= start) & (td <= end) & (pd.to_numeric(q["volume"], errors="coerce") > 0)
    return sorted(set(td[keep]))


def _qmt_daily_fill(days: list[str]) -> pd.DataFrame:
    """仓库没覆盖到的交易日，用落过的 QMT 日线缓存补（含涨停价推算）。

    ⚠️ 为什么需要它：仓库 `daily`/`stk_limit` 最新只到 **20260917**，
    而竞价过程（tick）已经采到 **20260922** —— 不补的话最近 3 天进不了数据集，
    回测窗口就缺了用户最关心的那一段（20260824 以来）。

    QMT 缓存的 `close`/`open` 与仓库逐位一致（实测 17.6 万行 100% 相同），
    但它的 `up_limit` 是占位值，所以这里按**官方口径几何推算**：
    主板 10%、创业板/科创板 20%，四舍五入到分。
    """
    p = ROOT / "data" / "backtest" / "qmt_daily_20260918_20260922.parquet"
    if not p.exists():
        return pd.DataFrame()
    q = pd.read_parquet(p)
    q["trade_date"] = q["trade_date"].astype(str)
    q["code"] = q["code"].astype(str).str.zfill(6)
    q = q[q["trade_date"].isin(set(days))]
    if not len(q):
        return pd.DataFrame()
    q = q[q["volume"].fillna(0) > 0]
    pc = pd.to_numeric(q["pre_close"], errors="coerce")
    rate = np.where(q["code"].str.startswith(("300", "301", "688", "689")), 0.20, 0.10)
    q["up_limit"] = (pc * (1 + rate)).round(2)
    # 流通市值：按"最近一个交易日的 circ_mv / 当日收盘"倒推流通股本，再乘当日昨收
    # （QMT 只有当前流通股本，直接乘会在早期严重失真；这里用相邻市值比例更稳）
    q["circ_mv"] = np.nan
    for c in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
        q[c] = pd.to_numeric(q[c], errors="coerce")
    logger.warning("QMT 补 %s：%d 行 / %d 只", sorted(set(days)), len(q),
                   q["code"].nunique())
    return q[["trade_date", "code", "open", "high", "low", "close", "pre_close",
              "volume", "amount", "up_limit", "circ_mv"]]


def build(start: str, end: str, *, refresh: bool = False) -> dict[str, Any]:
    out_main = OUT / "auction_dataset.parquet"
    out_series = OUT / "auction_series.parquet"
    if out_main.exists() and not refresh:
        logger.warning("已存在 %s（--refresh 可重建）", out_main.name)

    days = trading_days(start, end)
    if not days:
        return {"note": f"{start}~{end} 没有交易日"}
    print(f"[1/6] 交易日 {len(days)} 天：{days[0]} ~ {days[-1]}")

    conn = _connect()
    try:
        # ---- 日K基础（只取区间内，避免拉 1400 万行）----
        print("[2/6] 读日K基础（区间内）…")
        q = """
        SELECT d.trade_date, d.code, d.open, d.high, d.low, d.close, d.pre_close,
               d.volume_lot, d.amount, d.pct_chg,
               b.circ_mv, b.float_share, b.turnover_rate, b.total_mv,
               l.up_limit, l.down_limit
        FROM quant_daily d
        LEFT JOIN quant_daily_basic b ON b.code=d.code AND b.trade_date=d.trade_date
        LEFT JOIN quant_stk_limit l   ON l.code=d.code AND l.trade_date=d.trade_date
        WHERE d.trade_date BETWEEN ? AND ?
        """
        daily = pd.read_sql(q, conn, params=[days[0], days[-1]])
    finally:
        conn.close()
    daily["trade_date"] = daily["trade_date"].astype(str)
    daily["code"] = daily["code"].astype(str).str.zfill(6)
    # 仓库没覆盖的交易日：**优先用 tushare 原始分区**（当前只到 0918）。
    # ⚠️ 不要用 `data/backtest/qmt_daily_*.parquet`：那份 0918 之后是**复权价**
    #    （600051 缓存昨收 6.10，而录像带记的当天 9:25 竞价价是 7.17，比值 0.85），
    #    与仓库原始价不可比，算涨停价会全错。
    have = set(daily["trade_date"].astype(str))
    # ⚠️ 判"仓库有没有这一天"不能只看 `daily` 有没有行。实测 20260918：
    #    `quant_daily` 有 5209 行，但 `daily_basic`(circ_mv) 与 `stk_limit`(up_limit)
    #    **一行都没入库** → 算不出涨停、市值也缺，候选会被整片剔掉（且不报错）。
    #    所以按"这一天在 daily 里到底有多少行**能用于候选判定**"来判。
    usable = (daily.assign(_ok=daily["up_limit"].notna()
                           & daily["circ_mv"].notna())
                    .groupby("trade_date")["_ok"].sum())
    thin = {str(d) for d, n in usable.items() if n < 100}
    avail = [d for d in raw_partition_days() if start <= d <= end]
    pdays = [d for d in avail if d in thin or d not in have]
    print(f"[1b/6] 日线 {len(have)} 天；可用行不足的交易日 {sorted(thin)}；"
          f"分区可用 {avail}；**将补 {pdays}**", flush=True)
    if pdays:
        pf = _partition_fill(pdays)
        if len(pf):
            # ⚠️ 两张表的 trade_date 可能一个 str 一个 int（分区读回来是 int），
            #    直接 isin 会配不上 → 替换静默失效（实测：补后行数一行没变、
            #    最新日期还停在旧的，而日志却写着"将补 X 天"）。
            #    两边都 `.astype(str)` 再比。
            drop = set(map(str, pdays))
            keep_mask = ~daily["trade_date"].astype(str).isin(drop)
            daily = pd.concat([daily[keep_mask], pf], ignore_index=True)
            daily["trade_date"] = daily["trade_date"].astype(str)
            print(f"      分区替换 {sorted(drop)} 后：{len(daily):,} 行，最新 "
                  f"{daily['trade_date'].max()}", flush=True)
    days = sorted(set(days) | set(pdays))
    for c in ("open", "high", "low", "close", "pre_close", "volume_lot", "amount",
              "pct_chg", "circ_mv", "float_share", "turnover_rate", "total_mv",
              "up_limit", "down_limit"):
        if c in daily.columns:
            daily[c] = pd.to_numeric(daily[c], errors="coerce")
    print(f"      日K {len(daily):,} 行 / {daily['code'].nunique():,} 只")

    daily["is_st"] = _st_from_limit(daily["pre_close"], daily["up_limit"])
    daily["board"] = daily["code"].map(_board)
    daily["涨停"] = (daily["close"] - daily["up_limit"]).abs() < 0.005

    # ---- 候选：**上一交易日**涨停（生产 `require_yesterday_limit_up`）+ 用户口径 ----
    #
    # ⚠️ 这里必须是"**昨日**涨停"而不是"当日涨停"。当日涨停正是要**预测**的结果，
    #    用它当入选条件就是循环定义 —— 实测踩过：那样建出来的池子
    #    "收盘涨停率"恒等于 100%、"日内实体涨幅"恒为正，看上去策略完美，
    #    其实什么都没测。
    daily = daily.sort_values(["code", "trade_date"])
    daily["prev_sealed"] = daily.groupby("code", sort=False)["涨停"].shift(1)
    daily["prev_date"] = daily.groupby("code", sort=False)["trade_date"].shift(1)
    cand = daily[daily["prev_sealed"] == True].copy()          # noqa: E712
    cand = cand[cand["prev_date"].notna()
                & (~cand["is_st"])
                # 口径（用户 2026-09-22）：主板 + 创业板之外的都剔；创业板**与市值同处过滤**
                & cand["board"].isin(["沪主板", "深主板"])
                & cand["circ_mv"].between(MIN_MV, MAX_MV, inclusive="neither")
                & (cand["pre_close"] < MAX_PRICE)].copy()
    print(f"[3/6] 候选（上一交易日涨停 + 用户口径）{len(cand):,} 股日 / "
          f"{cand['code'].nunique():,} 只")

    # ---- 前 N 个交易日涨停次数（用户要求，窗口截至上一交易日）----
    print(f"[4/6] 算前 {LU_WINDOWS} 个交易日涨停次数…")
    hist_days = trading_days("20250101", days[-1])
    conn = _connect()
    try:
        q2 = """
        SELECT d.trade_date, d.code, d.close, l.up_limit
        FROM quant_daily d
        JOIN quant_stk_limit l ON l.code=d.code AND l.trade_date=d.trade_date
        WHERE d.trade_date >= ?
        """
        hd = pd.read_sql(q2, conn, params=[hist_days[0]])
    finally:
        conn.close()
    hd["trade_date"] = hd["trade_date"].astype(str)
    hd["code"] = hd["code"].astype(str).str.zfill(6)
    hd = hd.sort_values(["code", "trade_date"])
    hd["_sealed"] = ((pd.to_numeric(hd["close"], errors="coerce")
                      - pd.to_numeric(hd["up_limit"], errors="coerce")).abs()
                     < 0.005).astype("int8")
    prev_sealed = hd.groupby("code", sort=False)["_sealed"].shift(1)
    # 昨日连板高度 = **截至昨日的连续涨停天数**（遇非涨停归零）。
    #
    # ⚠️ 千万别用 `cumsum(sealed).shift(1)` —— 那是"**累计涨停总次数**"，
    #    不是"连续高度"。后果极其隐蔽且严重：实测 000856 被标成 14 板，
    #    而它区间内**一次涨停都没有**；候选里 59.3% 被打成"≥7板"。
    #    正确的 run-length 递推是 `h_i = (h_{i-1} + 1) × sealed_i`，
    #    即"非涨停日必须归零"。这一列是 bit11/12/13 与所有连板分档的判据，
    #    算错会让"昨日首板"这类规则整片失效。
    def _run_length(s: pd.Series) -> pd.Series:
        out = []
        run = 0
        for v in s.to_numpy():
            run = (run + 1) if v else 0
            out.append(run)
        return pd.Series(out, index=s.index, dtype="float64")

    hd["_streak_today"] = hd.groupby("code", sort=False)["_sealed"].transform(_run_length)
    hd["prev_streak"] = hd.groupby("code", sort=False)["_streak_today"].shift(1).fillna(0)
    _ps = pd.to_numeric(hd["prev_streak"], errors="coerce").fillna(0)
    print(f"      prev_streak 非零 {int((_ps > 0).sum()):,} / {len(hd):,}，"
          f"最大 {_ps.max():.0f}", flush=True)
    g = hd.groupby("code", sort=False)
    had = g.cumcount()
    count_cols = []
    for w in LU_WINDOWS:
        col = f"limit_up_days_{w}"
        roll = prev_sealed.groupby(hd["code"], sort=False).transform(
            lambda s, w=w: s.rolling(w, min_periods=w).sum())
        hd[col] = roll.where(had >= w).astype("Float64")
        count_cols.append(col)
    counts = hd[["trade_date", "code", "prev_streak"] + count_cols].copy()
    # ⚠️ merge 前把键统一成 str 并**显式重置索引** —— `hd` 是 sort_values 过的、
    #    索引带洞，两个表键类型/索引不一致时 merge 会配不上，
    #    结果 prev_streak 全是 NaN（实测：非零 0 行）。
    counts["trade_date"] = counts["trade_date"].astype(str)
    counts["code"] = counts["code"].astype(str).str.zfill(6)
    cand["trade_date"] = cand["trade_date"].astype(str)
    cand["code"] = cand["code"].astype(str).str.zfill(6)
    print(f"      键类型 daily={cand['trade_date'].dtype}/"
          f"counts={counts['trade_date'].dtype}，"
          f"样例 {counts['trade_date'].iloc[0]!r} vs {cand['trade_date'].iloc[0]!r}",
          flush=True)
    _before = len(cand)
    cand = cand.merge(counts, on=["trade_date", "code"], how="left",
                      suffixes=("", "_hd"))
    print(f"      merge 后行数 {_before} → {len(cand)}（应相等）", flush=True)
    _s = pd.to_numeric(cand["prev_streak"], errors="coerce")
    print(f"      合并后 prev_streak：非空 {int(_s.notna().sum()):,}、"
          f"非零 {int((_s > 0).sum()):,}、最大 {(_s.max() if _s.notna().any() else 0)}",
          flush=True)
    print("      " + "、".join(f"{c} 非空 {int(cand[c].notna().sum()):,}"
                              for c in count_cols))

    # ---- 竞价摘要 + 量能比值 ----
    print("[5/6] 接竞价摘要与量能比值…")
    summ = auction_summary()
    cand = cand.merge(summ, on=["trade_date", "code"], how="left")
    # 昨日全天量与昨日竞价量
    prev_vol = daily[["trade_date", "code", "volume_lot"]].rename(
        columns={"trade_date": "prev_date", "volume_lot": "prev_day_volume"})
    cand = cand.merge(prev_vol, on=["prev_date", "code"], how="left")
    prev_au = summ.rename(columns={"trade_date": "prev_date",
                                   "auction_volume_hand": "prev_auction_volume"})
    cand = cand.merge(prev_au[["prev_date", "code", "prev_auction_volume"]],
                      on=["prev_date", "code"], how="left")
    cand["auction_volume_ratio"] = (
        cand["auction_volume_hand"] / cand["prev_day_volume"] * 100).round(4)
    cand["auction_vs_yesterday"] = (
        cand["auction_volume_hand"] / cand["prev_auction_volume"]).round(4)
    cand["open_gap_pct"] = ((cand["auction_price"] / cand["pre_close"] - 1) * 100
                            ).round(4)

    # ---- 竞价过程特征（仅 tick 覆盖日；无覆盖整列留空）----
    print(f"[6/6] 复算竞价过程特征（tick 覆盖 {PROCESS_START} 起）…")
    proc_days = [d for d in days if (OUT / f"tick_auction_{d}.parquet").exists()]
    print(f"      有 tick 的交易日 {len(proc_days)} 天")
    frames = []
    for i, d in enumerate(proc_days, 1):
        sub = cand[cand["trade_date"] == d]
        if not len(sub):
            continue
        feat = process_features_for_day(
            d,
            streaks=dict(zip(sub["code"], sub["prev_streak"])),
            gap_pct=dict(zip(sub["code"], sub["open_gap_pct"])),
            vs_yesterday=dict(zip(sub["code"], sub["auction_vs_yesterday"])),
            volume_ratio_pct=dict(zip(sub["code"], sub["auction_volume_ratio"])))
        if len(feat):
            frames.append(feat)
        if i % 10 == 0:
            print(f"      … {i}/{len(proc_days)}", flush=True)
    proc = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if len(proc):
        proc = proc.drop(columns=["series_points", "match_price"], errors="ignore")
        cand = cand.merge(proc, on=["trade_date", "code"], how="left")
    cand["process_available"] = cand.get("pattern", pd.Series(index=cand.index)).notna()
    cand["data_layers"] = np.where(
        cand["process_available"], "daily+minute+tick",
        np.where(cand["auction_price"].notna(), "daily+minute", "daily"))

    # ---- 序列长表 ----
    ser_frames = []
    for d in proc_days:
        s = series_from_stored_tick(d)
        if len(s):
            ser_frames.append(s)
    series = pd.concat(ser_frames, ignore_index=True) if ser_frames else pd.DataFrame()

    cols = ["trade_date", "prev_date", "code", "board", "is_st", "prev_streak",
            "open", "high", "low", "close", "pre_close", "volume_lot", "amount",
            "pct_chg", "up_limit", "down_limit", "circ_mv", "float_share",
            "turnover_rate", "total_mv",
            "auction_price", "auction_volume_hand", "auction_amount",
            "auction_volume_ratio", "auction_vs_yesterday", "prev_day_volume",
            "prev_auction_volume", "open_gap_pct"] + count_cols + [
            "jump_value", "jump_points", "jump_fallback", "jump_window_mean",
            "pattern", "pattern_shape", "pattern_amplitude", "pattern_points",
            "rush_tag", "rush_labels", "rush_note",
            "process_available", "data_layers"]
    cols = [c for c in cols if c in cand.columns]
    main = cand[cols].sort_values(["trade_date", "code"]).reset_index(drop=True)
    main.to_parquet(out_main, index=False)
    if len(series):
        series.to_parquet(out_series, index=False)
    return {"main": main, "series": series, "days": len(days), "proc_days": proc_days}


def main() -> int:
    ap = argparse.ArgumentParser(description="构建竞价选股统一数据集（2 张表）")
    ap.add_argument("--from", dest="frm", default="20250919")
    ap.add_argument("--to", dest="to", default="")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    conn = _connect()
    try:
        end = args.to or str(conn.execute("SELECT MAX(trade_date) FROM quant_daily")
                             .fetchone()[0])
    finally:
        conn.close()

    res = build(args.frm, end, refresh=args.refresh)
    if res.get("note"):
        print(res["note"])
        return 0
    main_df, series = res["main"], res["series"]
    print()
    print("─" * 96)
    print(f"主表 auction_dataset.parquet：{len(main_df):,} 行 "
          f"（{main_df['trade_date'].min()} ~ {main_df['trade_date'].max()}）")
    print(f"序列 auction_series.parquet：{len(series):,} 行"
          + (f"（{series['code'].nunique():,} 只 × {series['trade_date'].nunique()} 天）"
             if len(series) else "（无）"))
    print()
    print("分层覆盖：")
    print(main_df["data_layers"].value_counts().to_string())
    print()
    print(f"竞价过程可用 {int(main_df['process_available'].sum()):,} 行"
          f"（占 {main_df['process_available'].mean() * 100:.1f}%）")
    if "pattern" in main_df.columns:
        print()
        print("形态分布（有过程数据的部分）：")
        print(main_df.loc[main_df["process_available"], "pattern"]
              .value_counts().to_string())
    print(f"\n已写出：{OUT / 'auction_dataset.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
