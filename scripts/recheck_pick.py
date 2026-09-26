"""逐只复核：「这只回测入池票，生产口径到底该不该拦」。

把四件事一次性打出来：

1. 它在回测里是**第几名、哪天入池**的；
2. 用 tick 重建的竞价序列 + **生产函数**复算：形态、跳空值、竞价量比、今昨竞比；
3. 逐条列出**回测没重建的规则**（bit4~bit10）各自会不会命中；
4. 结论：**该拦但没拦**（附原因），或**确实该入池**。

用法：
    .venv\\Scripts\\python.exe scripts/recheck_pick.py --code 002963 --date 20260827
    .venv\\Scripts\\python.exe scripts/recheck_pick.py --pairs 002963:20260827,603270:20260904
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from verify_auction_pattern import series_from_tick  # noqa: E402

logger = logging.getLogger("recheck_pick")
OUT_DIR = ROOT / "data" / "backtest"


def _pick_row(code: str, day: str) -> dict | None:
    """从回测输出里找这只票的行（前5 口径优先）。"""
    cands = sorted(glob.glob(str(OUT_DIR / "auction_backtest_*_top5*.xlsx")), key=os.path.getmtime)
    if not cands:
        return None
    df = pd.read_excel(cands[-1], sheet_name="前5_入池明细",
                       dtype={"代码": str, "交易日": str})
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df["交易日"] = df["交易日"].astype(str)
    hit = df[(df["代码"] == code) & (df["交易日"] == day)]
    if not len(hit):
        return None
    rec = hit.to_dict("records")[0]
    rec["_source"] = Path(cands[-1]).name
    rec["_rank_in_day"] = int(df[df["交易日"] == day].reset_index(drop=True)
                              .index[df[df["交易日"] == day].reset_index(drop=True)["代码"]
                                     == code][0]) + 1
    return rec


def _auction_volume_lot(full: str, day: str) -> float | None:
    from xtquant import xtdata

    xtdata.connect()
    try:
        xtdata.download_history_data(full, "1m", start_time=day, end_time=day)
        d = xtdata.get_market_data_ex([], [full], period="1m",
                                      start_time=day, end_time=day, count=-1)
    except Exception:                                          # noqa: BLE001
        return None
    df = d.get(full)
    if df is None or not len(df):
        return None
    idx = [i for i, x in enumerate(df.index) if str(x)[8:12] == "0930"]
    return float(df.iloc[idx[0]]["volume"]) if idx else None


def _prev_day_volume(code: str, day: str) -> float | None:
    """上一交易日的**全天成交量**（手）。"""
    from src.quant.warehouse import load_dataset

    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    dd, _ = load_dataset("daily", start=start, codes=[code],
                         columns=["code", "trade_date", "volume_lot"])
    if dd is None or not len(dd):
        return None
    dd.columns = [str(c).strip().strip('"') for c in dd.columns]
    dd["trade_date"] = dd["trade_date"].astype(str)
    dd["volume_lot"] = pd.to_numeric(dd["volume_lot"], errors="coerce")
    dd = dd[dd["trade_date"] < day].sort_values("trade_date")
    return float(dd["volume_lot"].iloc[-1]) if len(dd) else None


def recheck(code: str, day: str) -> None:
    from src.auction_select import config as auction_config
    from src.auction_select import features as F

    cfg = auction_config.load_config()
    full = code + (".SH" if code.startswith(("6", "9")) else ".SZ")
    print("=" * 96)
    print(f"【{code} {day}】")
    print("=" * 96)

    rec = _pick_row(code, day)
    if rec is None:
        print("  回测入池明细里没有这只票（可能不是前 5，或不在回测窗口）")
    else:
        print(f"  回测里：第 {rec['_rank_in_day']} 名入池"
              f"（来源 {rec['_source']}）")
        print(f"    昨收 {rec.get('昨收')} → 竞价 {rec.get('竞价价')}"
              f"（{rec.get('竞价涨幅%')}%）→ 收盘 {rec.get('收盘')}"
              f"（日内实体 {rec.get('日内实体涨幅%')}%，收盘涨停 {rec.get('收盘涨停')}）")
        print(f"    昨日连板 {rec.get('昨日连板')}、近18日涨停 {rec.get('近18日涨停')}、"
              f"流通市值 {rec.get('流通市值亿')} 亿")

    streak = None
    if rec:
        try:
            streak = float(rec.get("昨日连板"))
        except (TypeError, ValueError):
            streak = None

    # ---- 1. tick 重建序列 ----
    series = series_from_tick(full, day)
    if not series:
        print("  ⚠ 无 tick 数据，形态类判不了")
        return
    first, last = series[0], series[-1]
    win = F.slice_auction_series(series, start="09:19:20", end="09:25:00")
    lo = min(p["price"] for p in win) if win else None
    hi = max(p["price"] for p in win) if win else None
    print(f"\n  ── 竞价分时（tick 重建，{len(series)} 点）──")
    print(f"    首点 {first['time']} = {first['price']}；"
          f"末点 {last['time']} = {last['price']}")
    if lo:
        print(f"    09:19:20~09:25 窗口区间 {lo} ~ {hi}"
              f"（振幅 {(hi - lo) / lo * 100:.2f}%）")
    print(f"    序列前 6 点: {[p['price'] for p in series[:6]]}")
    print(f"    序列后 6 点: {[p['price'] for p in series[-6:]]}")

    match_price = rec.get("竞价价") if rec else last["price"]
    match_price = float(match_price) if match_price else float(last["price"])

    # ---- 2. 量 ----
    vol_today = _auction_volume_lot(full, day)
    vol_prev = _prev_day_volume(code, day)
    ratio = (vol_today / vol_prev * 100) if (vol_today and vol_prev) else None
    prev_vol_prev = _auction_volume_lot(full, _prev_date(code, day)) \
        if _prev_date(code, day) else None
    vs_yest = (vol_today / prev_vol_prev) if (vol_today and prev_vol_prev) else None
    print("\n  ── 量与比 ──")
    print(f"    今日竞价量 {vol_today} 手；昨日全天量 {vol_prev} 手")
    print(f"    昨日竞价量 {prev_vol_prev} 手")
    print(f"    竞价量比（今竞价 ÷ 昨全天）= "
          f"{round(ratio, 3) if ratio else '—'}%")
    print(f"    今昨竞比（今竞价 ÷ 昨竞价）= "
          f"{round(vs_yest, 3) if vs_yest else '—'}")

    # ---- 3. 生产函数复算 ----
    jump, jmeta = F.jump_gap(series, match_price=match_price)
    pattern, pmeta = F.classify_pattern(series, volume_vs_yesterday=vs_yest,
                                        prev_limit_up_streak=streak)
    print("\n  ── 生产函数复算 ──")
    print(f"    跳空值 = {round(jump, 4) if jump else '—'}"
          f"（窗口 {jmeta.get('points')} 点）")
    print(f"    形态   = 「{pattern}」"
          f"（falling_first_board={pmeta.get('falling_first_board')}, "
          f"falling_ratio={pmeta.get('falling_ratio')}, "
          f"falling_blocked={pmeta.get('falling_blocked')}）")

    # ---- 4. 逐条判"回测没重建的规则" ----
    hard = float(getattr(cfg.features, "rush_out_hard_jump", F.RUSH_OUT_HARD_JUMP))
    down = float(getattr(cfg.features, "rush_jump_down", F.RUSH_JUMP_DOWN))
    vol_pct = float(getattr(cfg.features, "rush_out_volume_pct", F.RUSH_OUT_VOLUME_PCT))
    max_ratio = float(cfg.veto.max_auction_volume_ratio)
    exempt_streak = float(getattr(cfg.veto, "main_board_exempt_min_streak", 2))
    heavy_pct = float(getattr(cfg.features, "rush_heavy_volume_pct",
                              F.RUSH_HEAVY_VOLUME_PCT))
    gap_pct = float(rec.get("竞价涨幅%")) if rec and rec.get("竞价涨幅%") is not None else None
    exempt = streak is not None and streak > exempt_streak
    heavy = (pattern == F.PATTERN_FALLING and ratio is not None and ratio > heavy_pct)

    print("\n  ── 回测**没有**重建的规则，逐条判 ──")
    bit4 = ratio is not None and ratio > max_ratio and not heavy
    print(f"    bit4 竞价量比 > {max_ratio}% ? "
          f"{'命中' if ratio and ratio > max_ratio else '未命中'}"
          f"（{round(ratio, 2) if ratio else '—'}%）"
          f"{'，但「大量抢筹」可豁免' if heavy else ''}"
          f" → {'⛔ 否决' if bit4 else '通过'}")
    b7_hard = jump is not None and jump < hard
    b7_soft = jump is not None and jump < down and ratio is not None and ratio > vol_pct
    print(f"    bit7 抢跑（硬线 跳空<{hard}）? "
          f"{'⛔ 命中：不看量比、不受豁免' if b7_hard else '未命中'}"
          f"（跳空 {round(jump, 4) if jump else '—'}）")
    print(f"    bit7 抢跑（常规档 跳空<{down} 且 竞价量比>{vol_pct}%）? "
          f"{'命中' if b7_soft else '未命中'}")
    bit6 = pattern == F.PATTERN_FALLING
    print(f"    bit6 形态「{F.PATTERN_FALLING}」? "
          f"{'⛔ 命中' if bit6 else '未命中'}"
          f"{'（形状像但因量不够被 blocked）' if pmeta.get('falling_blocked') else ''}")
    print(f"    bit3 主板 >{cfg.veto.main_board_max_open_gap_pct:g}%"
          f"（昨日连板>{exempt_streak:g} 豁免）? "
          f"{'豁免生效' if exempt else '不豁免'}"
          f"（竞价涨幅 {gap_pct}%、昨日连板 {streak}）")

    verdict = b7_hard or b7_soft or bit6 or bit4
    print("\n  ── 结论 ──")
    if verdict:
        reasons = []
        if b7_hard:
            reasons.append(f"bit7 硬否决（跳空 {round(jump,4)} < {hard}）")
        if b7_soft:
            reasons.append("bit7 常规抢跑")
        if bit6:
            reasons.append("bit6 急速下坠型")
        if bit4:
            reasons.append("bit4 竞价量比超限")
        print(f"    ⛔ **该被拦截**：{'；'.join(reasons)}")
        print(f"    被回测放行的原因：这条规则需要 9:15~9:25 分时序列，"
              f"回测只重建了 前置筛选+bit2/3/11/12/13，bit6/bit7/bit4 **未参与**。")
    else:
        print("    ✅ 确实该入池：形态类与量能类规则都没命中")


def _prev_date(code: str, day: str) -> str | None:
    from src.quant.warehouse import load_dataset

    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    dd, _ = load_dataset("daily", start=start, codes=[code],
                         columns=["code", "trade_date"])
    if dd is None or not len(dd):
        return None
    dd.columns = [str(c).strip().strip('"') for c in dd.columns]
    dd["trade_date"] = dd["trade_date"].astype(str)
    dd = dd[dd["trade_date"] < day].sort_values("trade_date")
    return str(dd["trade_date"].iloc[-1]) if len(dd) else None


def main() -> int:
    ap = argparse.ArgumentParser(description="逐只复核回测入池票是否该被拦")
    ap.add_argument("--code", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--pairs", default="", help="code:date,code:date")
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    pairs: list[tuple[str, str]] = []
    if args.pairs:
        for item in args.pairs.split(","):
            if ":" in item:
                c, d = item.split(":", 1)
                pairs.append((c.strip().zfill(6), d.strip()))
    elif args.code and args.date:
        pairs.append((args.code.zfill(6), args.date))
    if not pairs:
        print("请给 --code/--date 或 --pairs")
        return 1
    for code, day in pairs:
        recheck(code, day)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
