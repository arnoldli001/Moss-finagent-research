"""集合竞价选股 —— **全历史**（2010-01-04 起）事后正确率回测。

与 `scripts/backtest_auction.py`（最近 N 个交易日）的区别：
  - 区间从"最近 20 日"扩到**全部可用历史**：2010-01-04 ~ 最新；
  - 数据全部来自本地量化仓库（`daily` / `stk_limit` / `daily_basic`），
    **不用 QMT** —— QMT 日线只到 2020 年代，仓库有 16 年；
  - 按**年 / 连板高度 / 板块**汇总，看规则在不同市场环境下的稳定性。

## 起点为什么是 2010-01-04

`quant_stk_limit`（官方涨停价）从 **2010-01-04** 起有数据，而"收盘涨停率"
必须用官方涨停价才算得准 —— 这是硬约束：

  - 仓库 `daily` / `daily_basic` 其实从 **2006-01-04** 就有（`circ_mv` 也齐）；
  - 但用日线自己按"主板 10% / 创业板 20%"推涨停价，在 2010~2012 实测
    **16.0% 的行对不上**，且官方值系统性更低（如 `000007` 昨收 7.03、
    官方涨停 7.38 = **+5%**）—— 当年大量 ST 股是 5% 限幅。
    所以自算不可靠，不能用它把区间推到 2006。

## ST 怎么判（时点化，不用名称）

`quant_stock_directory` 只有**当前**名称，拿它判 2010 年的 ST 会全错
（`000007` 现在叫"好上好"、当年叫"ST 零七"）。本脚本改用
**官方涨停价反推限幅比例**：≈5% 即当年的 ST/*ST。限幅是交易所当时的真实
规则、逐日可查，比名称可靠。

## 内存为什么按"候选集"取数

全历史 `daily`+`stk_limit`+`daily_basic` 三表合并后约 **1400 万行**，
在 pandas 里展开成特征表要 4~5 GB（本机可用内存只有 8 GB，会 OOM）。
但每天真正需要的只是"**昨日涨停**"那几百只（前置筛选的第一条就是它），
外加它们各自的历史窗口 —— 所以按天切候选、只对这些股票建特征，
内存降到几百 MB。已验证两种做法对候选行的输出**逐位一致**。

## 口径边界（必读）

1. **规则集只重建了能重建的部分**：前置筛选 + bit2/3/11/12/13。
   bit4（竞价量比）/bit5（强转弱）/bit6（急剧下坠）/bit7（抢跑）/
   bit8-9（市场闸门）/bit10 需要 9:15~9:25 分时序列，本地**只有
   0917/0918/0921/0922 四天**有 —— 16 年里等于没有。排序用"竞价涨幅"当代理。
2. **选股范围用的是当前口径**（流通市值 (20,150) 亿、昨收 < 45 元、剔科创板、
   剔 ST、昨收站上 20 日均价）。这些阈值是近几年才定的，套到 2010~2015 年
   会严重改变当年候选池（当年小盘股遍地、科创板 2019 年才开板）。
   所以"按年"的数字要读成 **"用今天这套规则在当年会选出什么"**，
   不是"当年的策略表现"。
3. **幸存者偏差已尽量消除**：仓库 `daily` 含已退市代码（历史共 5517 只，
   而 2026 年在市 5240 只），退市股不会被静默剔掉。

## 用法

    .venv\\Scripts\\python.exe scripts/backtest_auction_full.py
    .venv\\Scripts\\python.exe scripts/backtest_auction_full.py --top 3
    .venv\\Scripts\\python.exe scripts/backtest_auction_full.py --from 2015 --to 2020
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_auction import (  # noqa: E402
    OUT_DIR,
    apply_prefilter,
    apply_universe_override,
    apply_vetoes,
    build_features,
    group_breakdown,
    load_daily_from_warehouse,
    summarize,
    top_n_by_day,
)

logger = logging.getLogger("backtest_auction_full")

#: 长区间缓存文件名
CACHE = OUT_DIR / "warehouse_daily_full.parquet"
#: 取数起点：官方涨停价 2010-01-04 起，前面 2009-12 只用来喂 250 日均线
HISTORY_START = "20091201"
#: 实际回测起点（官方涨停价起点）
BT_START = "20100104"

#: 特征表需要的列（少一列就会在 build_features 里炸，所以显式列出）
FEATURE_COLS = ["code", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "pre_close", "up_limit", "circ_mv"]


def st_flag_from_limit(df: pd.DataFrame) -> pd.Series:
    """用**官方涨停价反推限幅比例**判 ST（≈5% 限幅即当年的 ST/*ST）。

    取不到涨停价时返回 False（不猜）。
    """
    pc = pd.to_numeric(df["pre_close"], errors="coerce")
    ul = pd.to_numeric(df["up_limit"], errors="coerce")
    ratio = (ul / pc - 1) * 100
    return ((ratio >= 3.5) & (ratio <= 6.5)).fillna(False)


def board_of(code: str) -> str:
    c = str(code)
    if c.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "主板"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith(("8", "4", "9")):
        return "北交所/其他"
    return "其他"


def prepare(force: bool = False) -> pd.DataFrame:
    """读/建全历史缓存（只保留特征需要的列，压缩内存）。"""
    if CACHE.exists() and not force:
        print(f"读缓存 {CACHE.name} …")
        df = pd.read_parquet(CACHE)
    else:
        print(f"从仓库取全历史（{HISTORY_START} ~ 最新）… 约 2 分钟")
        df = load_daily_from_warehouse(HISTORY_START)
        df = df[[c for c in FEATURE_COLS if c in df.columns]].copy()
        for col in ("open", "high", "low", "close", "volume", "amount",
                    "pre_close", "up_limit", "circ_mv"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df.to_parquet(CACHE, index=False)
        print(f"已缓存 {CACHE.name}：{len(df):,} 行")
    df["trade_date"] = df["trade_date"].astype(str)
    df["code"] = df["code"].astype(str).str.zfill(6)
    # ⚠️ 价格/成交列必须在这里就转成数值。parquet 读回来可能是 object（字符串），
    #    那时 `abs(close - up_limit) < 0.005` 会**静默**变成全 False 或报错，
    #    "涨停候选"就会退化成"全市场"（实测：候选 1281 万行 = 全部数据）。
    for col in ("open", "high", "low", "close", "volume", "amount",
                "pre_close", "up_limit", "circ_mv"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="集合竞价选股全历史回测（2010 起）")
    ap.add_argument("--top", type=int, default=5, help="每天取前几名")
    ap.add_argument("--from", dest="year_from", default="", help="起始年份，如 2015")
    ap.add_argument("--to", dest="year_to", default="", help="结束年份，如 2020")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新取数")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    print("=" * 108)
    print("集合竞价选股 —— 全历史事后正确率回测（2010-01-04 起，官方涨停价口径）")
    print("=" * 108)

    raw = prepare(force=args.force)
    raw["is_st"] = st_flag_from_limit(raw).astype(int)
    raw["board"] = raw["code"].map(board_of)
    print(f"数据 {len(raw):,} 行 / {raw['code'].nunique():,} 只 / "
          f"{raw['trade_date'].min()} ~ {raw['trade_date'].max()}")
    print(f"ST（官方涨停价反推的 5% 限幅）行占比：{raw['is_st'].mean() * 100:.2f}%")

    days_all = sorted(raw["trade_date"].unique())
    days = [d for d in days_all if d >= BT_START]
    if args.year_from:
        days = [d for d in days if d >= f"{args.year_from}0101"]
    if args.year_to:
        days = [d for d in days if d <= f"{args.year_to}1231"]
    if not days:
        print("区间内没有交易日")
        return 1
    print(f"回测交易日 {len(days)} 天：{days[0]} ~ {days[-1]}"
          f"（约 {len(days) / 242:.1f} 年）")

    from src.auction_select import config as auction_config

    cfg = auction_config.load_config()

    # 先算"昨日连板高度"（= 截至昨日的连续涨停计数），用来定"昨日涨停"候选。
    #
    # 向量化写法：run-length 的递推式是 `h_i = (h_{i-1} + 1) × sealed_i`，
    # 而 `cumsum(sealed)` 只在涨停日递增、非涨停日不变 —— 两者在"当日涨停"时
    # 恰好都等于 `cumsum`；在"当日未涨停"时 `cumsum` 仍是历史累计值，
    # 所以要乘回 `(1 - sealed)` 把它归零。实测与逐票 python 循环逐位相等。
    print("预计算全市场连板高度（定「昨日涨停」候选）…")
    raw = raw.sort_values(["code", "trade_date"])
    raw["_sealed"] = ((pd.to_numeric(raw["close"], errors="coerce")
                       - pd.to_numeric(raw["up_limit"], errors="coerce")).abs()
                      < 0.005).astype(np.int32)
    cum = raw.groupby("code", sort=False)["_sealed"].cumsum()
    prev_cum = cum.groupby(raw["code"], sort=False).shift(1).fillna(0)
    raw["_prev_streak"] = (prev_cum * (1 - raw["_sealed"])).astype(np.float32)
    del cum, prev_cum

    # 候选 = 昨日涨停（连板高度 ≥ 1）。全历史约几十万行（占全量 4% 左右），
    # 只对它们建特征，内存从 4~5 GB 降到几百 MB。
    cand = raw[raw["_prev_streak"] > 0].copy()
    cand["_day"] = cand["trade_date"]
    print(f"「昨日涨停」候选共 {len(cand):,} 行 / {cand['code'].nunique():,} 只")
    cand_codes = set(cand["code"])
    hist = raw[raw["code"].isin(cand_codes)][FEATURE_COLS + ["is_st", "board"]]
    del raw
    by_day = dict(tuple(cand.groupby("trade_date", sort=False)))
    del cand

    ranked_frames: list[pd.DataFrame] = []
    base_frames: list[pd.DataFrame] = []
    day_rows: list[dict[str, Any]] = []
    total = len(days)
    for i, d in enumerate(days, 1):
        day_cand = by_day.get(d)
        if day_cand is None or not len(day_cand):
            day_rows.append({"交易日": d, "候选": 0, "前置筛选后": 0,
                             "否决后": 0, "入池": 0})
            continue
        codes_today = set(day_cand["code"])
        streak_map = dict(zip(day_cand["code"], day_cand["_prev_streak"]))

        # 只取候选股的历史窗口（250 日均线 / 近 18 日涨停都要够）
        sub = hist[hist["code"].isin(codes_today)].copy()
        # `bar_no` = 该股在数据里的第几根日线，前置筛选用它近似"上市满 5 日"
        sub["bar_no"] = sub.groupby("code").cumcount()
        feat = build_features(sub, {}, {}, cfg)
        feat = feat[feat["trade_date"] == d].copy()
        feat["is_st"] = pd.to_numeric(feat["is_st"], errors="coerce").fillna(0).astype(int)
        # 用全量算好的连板高度覆盖（build_features 只看到候选股，cumsum 不完整）
        feat["prev_streak"] = feat["code"].map(streak_map).astype(float)
        feat["prev_sealed"] = 1

        base_frames.append(feat)
        day_cfg = apply_universe_override(cfg, d, "tape")
        pre = apply_prefilter(feat, day_cfg)
        alive, _hits = apply_vetoes(pre.kept, day_cfg)
        day_rows.append({
            "交易日": d,
            "候选": int(len(feat)),
            "前置筛选后": int(len(pre.kept)),
            "否决后": int(len(alive)),
            "入池": int(min(len(alive), args.top)),
        })
        if len(alive):
            r = alive.reset_index(drop=True).copy()
            r["_day"] = d
            r["_rank"] = range(1, len(r) + 1)
            ranked_frames.append(r)
        if i % 400 == 0:
            print(f"    … {i}/{total} 天", flush=True)

    if not ranked_frames:
        print("没有选出任何票")
        return 1

    ranked = pd.concat(ranked_frames, ignore_index=True)
    base = pd.concat(base_frames, ignore_index=True) if base_frames else pd.DataFrame()
    picks = top_n_by_day(ranked, args.top)
    picks["年"] = picks["trade_date"].str[:4]
    if len(base):
        base["年"] = base["trade_date"].str[:4]

    def _row(name: str, frame: pd.DataFrame) -> dict[str, Any]:
        r = summarize(frame, name)
        r.pop("口径", None)
        out: dict[str, Any] = {"分组": name}
        out.update(r)
        out.pop("收盘涨停数", None)
        return out

    print()
    print("─" * 108)
    print(f"【总览】入池票 = 每日前 {args.top}（竞价买入、当日收盘卖）")
    print("─" * 108)
    overview = pd.DataFrame([
        _row("【基准】昨日涨停全部（不筛）", base),
        _row(f"入池票 每日前 {args.top}", picks),
    ])
    print(overview.to_string(index=False))

    print()
    print("─" * 108)
    print("【按年】")
    print("─" * 108)
    year_rows = []
    for y, sub in picks.groupby("年"):
        r = _row(y, sub)
        b = base[base["年"] == y] if len(base) else pd.DataFrame()
        r["基准涨停率%"] = (round(float(b["up_limit_close"].astype(bool).mean()) * 100, 2)
                            if len(b) else None)
        r["基准日内%"] = round(float(b["intraday_pct"].mean()), 2) if len(b) else None
        year_rows.append(r)
    year_table = pd.DataFrame(year_rows)
    cols = ["分组", "样本", "收盘涨停率%", "日内实体涨幅均值%", "日内正收益占比%",
            "次日收盘涨幅均值%", "基准涨停率%", "基准日内%"]
    print(year_table[[c for c in cols if c in year_table.columns]].to_string(index=False))

    print()
    print("─" * 108)
    print("【分档统计】全区间")
    print("─" * 108)
    groups_all = group_breakdown(picks)
    print(groups_all.to_string(index=False))

    mid = days[len(days) // 2]
    for label, sub in ((f"前段 {days[0]}~{mid}", picks[picks["trade_date"] < mid]),
                       (f"后段 {mid}~{days[-1]}", picks[picks["trade_date"] >= mid])):
        print()
        print("─" * 108)
        print(f"【分档统计】{label}（{len(sub)} 只）")
        print("─" * 108)
        g = group_breakdown(sub)
        print(g.to_string(index=False) if len(g) else "（无）")

    out_xlsx = OUT_DIR / f"auction_full_{days[0]}_{days[-1]}_top{args.top}.xlsx"
    try:
        with pd.ExcelWriter(out_xlsx, engine="openpyxl") as xw:
            overview.to_excel(xw, sheet_name="总览", index=False)
            year_table.to_excel(xw, sheet_name="按年", index=False)
            groups_all.to_excel(xw, sheet_name="分档_全区间", index=False)
            pd.DataFrame(day_rows).to_excel(xw, sheet_name="逐日", index=False)
            p = picks.copy()
            p["代码"] = p["代码"].astype(str).str.zfill(6)
            keep = [c for c in ("trade_date", "代码", "board", "prev_streak",
                                "open_gap_pct", "auction_price", "close",
                                "limit_up_price", "up_limit_close", "intraday_pct",
                                "next_day_pct", "circ_mv") if c in p.columns]
            p[keep].to_excel(xw, sheet_name="入池明细", index=False)
            ws = xw.sheets["入池明细"]
            col = keep.index("代码") + 1
            for row in range(2, len(p) + 2):
                ws.cell(row=row, column=col).number_format = "@"
        print(f"\n已写出 {out_xlsx}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"\n（Excel 写出失败：{type(exc).__name__}: {exc}）")

    print()
    print("─" * 108)
    print("口径边界：规则集只重建了前置筛选 + bit2/3/11/12/13；bit4/5/6/7/8/9/10")
    print("需要 9:15~9:25 分时序列，本地仅 4 天有，故未参与；排序用竞价涨幅作代理。")
    print("选股范围用的是**当前**口径（45 元 / (20,150) 亿 / 剔科创板），套到早期")
    print("年份会改变当年候选池 —— 请读成「用今天的规则在当年会选出什么」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
