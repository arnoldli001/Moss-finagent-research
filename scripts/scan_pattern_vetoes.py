"""批量复算：回测入池票里，有多少会被**竞价形态类否决**（bit6/bit7）拦掉。

## 背景

长回测（`backtest_auction.py` / `backtest_auction_full.py`）**不含** bit6
（竞价形态「急剧下坠型」）与 bit7（竞价抢跑）—— 这两条需要 9:15~9:25 的分时序列，
而生产只把它落盘在跑过的那几天（本地仅 0917/0918/0921/0922）。

但 **QMT 的 3 秒 tick 里就有这条序列**（覆盖约 1 个月，20260824 起），
且已逐点验证与生产落盘序列 **100% 一致**（见 `verify_auction_pattern.py`）。
所以对 tick 覆盖到的那段回测区间，可以把这两条补上，回答
"回测入池的票里有多少其实是形态上该被否决的"。

## 用法

    .venv\\Scripts\\python.exe scripts/scan_pattern_vetoes.py --top 5
    .venv\\Scripts\\python.exe scripts/scan_pattern_vetoes.py --top 5 --from 20260826 --to 20260918
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from verify_auction_pattern import series_from_tick  # noqa: E402

logger = logging.getLogger("scan_pattern_vetoes")
OUT_DIR = ROOT / "data" / "backtest"


def auction_volume_lot(full_code: str, day: str) -> float | None:
    """取该日竞价成交量（手）—— 1 分钟线的 `0930` bar 就是竞价 bar。

    已与 `auction_snapshot.open_volume_hand` 逐位核对一致。
    """
    from xtquant import xtdata

    xtdata.connect()
    try:
        xtdata.download_history_data(full_code, "1m", start_time=day, end_time=day)
        d = xtdata.get_market_data_ex([], [full_code], period="1m",
                                      start_time=day, end_time=day, count=-1)
    except Exception:                                          # noqa: BLE001
        return None
    df = d.get(full_code)
    if df is None or not len(df):
        return None
    hit = [i for i, x in enumerate(df.index) if str(x)[8:12] == "0930"]
    if not hit:
        return None
    return float(df.iloc[hit[0]]["volume"])


def load_picks(top: int) -> pd.DataFrame:
    """读长回测的入池明细（如果没有就跑一次短回测的输出）。"""
    cands = sorted(OUT_DIR.glob(f"auction_full_*_top{top}.xlsx"))
    if not cands:
        cands = sorted(OUT_DIR.glob(f"auction_backtest_*_top{top}*.xlsx"))
    if not cands:
        raise SystemExit("找不到回测输出，请先跑 backtest_auction.py 或 backtest_auction_full.py")
    path = cands[-1]
    sheet = "入池明细" if "auction_full" in path.name else f"前{top}_入池明细"
    df = pd.read_excel(path, sheet_name=sheet, dtype={"代码": str, "trade_date": str})
    if "trade_date" not in df.columns:
        df = df.rename(columns={"交易日": "trade_date", "代码": "code",
                                "板块": "board", "昨收": "pre_close",
                                "竞价价": "auction_price", "昨日连板": "prev_streak"})
    else:
        df = df.rename(columns={"代码": "code"})
    logger.warning("读入池明细 %s：%d 行", path.name, len(df))
    return df, path


def main() -> int:
    ap = argparse.ArgumentParser(description="批量复算竞价形态类否决")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--from", dest="d0", default="", help="起始日 YYYYMMDD")
    ap.add_argument("--to", dest="d1", default="", help="结束日 YYYYMMDD")
    ap.add_argument("--out", default="", help="结果写到哪里（默认 data/backtest 下）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    from src.auction_select import config as auction_config
    from src.auction_select import features as F

    cfg = auction_config.load_config()
    hard = float(getattr(cfg.features, "rush_out_hard_jump", F.RUSH_OUT_HARD_JUMP))
    down = float(getattr(cfg.features, "rush_jump_down", F.RUSH_JUMP_DOWN))
    vol_pct = float(getattr(cfg.features, "rush_out_volume_pct", F.RUSH_OUT_VOLUME_PCT))

    picks, src_path = load_picks(args.top)
    picks["trade_date"] = picks["trade_date"].astype(str)
    if args.d0:
        picks = picks[picks["trade_date"] >= args.d0]
    if args.d1:
        picks = picks[picks["trade_date"] <= args.d1]
    if not len(picks):
        print("区间内没有入池票")
        return 1
    print(f"待复算入池票 {len(picks)} 只"
          f"（{picks['trade_date'].min()} ~ {picks['trade_date'].max()}）")
    print(f"阈值：硬否决跳空 < {hard}、常规抢跑 跳空 < {down} 且 竞价量比 > {vol_pct}%")

    prev_vol_cache: dict[tuple[str, str], float | None] = {}
    rows: list[dict[str, Any]] = []
    for i, rec in enumerate(picks.to_dict("records"), 1):
        code = str(rec["code"]).zfill(6)
        day = str(rec["trade_date"])
        full = code + (".SH" if code.startswith(("6", "9")) else ".SZ")
        match_price = rec.get("竞价价") or rec.get("auction_price")
        streak = rec.get("昨日连板") if "昨日连板" in rec else rec.get("prev_streak")

        series = series_from_tick(full, day)
        if not series:
            rows.append({"交易日": day, "代码": code, "竞价序列点数": 0,
                         "跳空值": None, "形态": "无tick数据",
                         "bit6 形态否决": "", "bit7 抢跑否决": "", "结论": "无法复算"})
            continue
        jump, jmeta = F.jump_gap(series, match_price=float(match_price)
                                 if match_price else None)
        pattern, pmeta = F.classify_pattern(
            series, volume_vs_yesterday=None, prev_limit_up_streak=float(streak)
            if streak is not None and str(streak) != "nan" else None)

        # 竞价量比 = 今竞价量 ÷ 昨日全天量（手）
        vol_today = auction_volume_lot(full, day)
        key = (code, day)
        if key not in prev_vol_cache:
            prev_vol_cache[key] = None
            try:
                from src.quant.warehouse import load_dataset
                from datetime import datetime, timedelta
                start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
                dd, _ = load_dataset("daily", start=start, codes=[code],
                                     columns=["code", "trade_date", "volume_lot"])
                if dd is not None and len(dd):
                    dd.columns = [str(c).strip().strip('"') for c in dd.columns]
                    dd["trade_date"] = dd["trade_date"].astype(str)
                    dd = dd[dd["trade_date"] < day].sort_values("trade_date")
                    if len(dd):
                        prev_vol_cache[key] = float(pd.to_numeric(
                            dd["volume_lot"], errors="coerce").iloc[-1])
            except Exception:                                  # noqa: BLE001
                pass
        prev_vol = prev_vol_cache[key]
        ratio = (vol_today / prev_vol * 100) if (vol_today and prev_vol) else None

        bit6 = (pattern == F.PATTERN_FALLING)
        bit7_hard = jump is not None and jump < hard
        bit7_soft = (jump is not None and jump < down
                     and ratio is not None and ratio > vol_pct)
        bit7 = bool(bit7_hard or bit7_soft)
        rows.append({
            "交易日": day, "代码": code, "名称": rec.get("名称") or rec.get("name"),
            "名次": rec.get("rank") or rec.get("_rank"),
            "竞价序列点数": len(series),
            "跳空值": round(jump, 4) if jump is not None else None,
            "竞价量比%": round(ratio, 3) if ratio is not None else None,
            "形态": pattern,
            "bit6 形态否决": "是" if bit6 else "",
            "bit7 抢跑否决": ("硬线" if bit7_hard else ("常规" if bit7_soft else "")) or "",
            "结论": "该被否决" if (bit6 or bit7) else "保留",
            "日内实体涨幅%": rec.get("日内实体涨幅%"),
            "收盘涨停": rec.get("收盘涨停"),
        })
        if i % 10 == 0:
            print(f"  … {i}/{len(picks)}")

    out = pd.DataFrame(rows)
    scanned = out[out["竞价序列点数"] > 0]
    vetoed = scanned[scanned["结论"] == "该被否决"]
    print()
    print("─" * 96)
    print(f"可复算 {len(scanned)} / {len(out)} 只（其余无 tick 数据）")
    print(f"其中**应被形态类否决** {len(vetoed)} 只"
          f"（占可复算 {len(vetoed) / max(len(scanned), 1) * 100:.1f}%）")
    if len(vetoed):
        print(f"  bit6 急速下坠 {int((vetoed['bit6 形态否决'] == '是').sum())} 只、"
              f"bit7 抢跑 {int((vetoed['bit7 抢跑否决'] != '').sum())} 只"
              f"（含硬线 {int((vetoed['bit7 抢跑否决'] == '硬线').sum())} 只）")
        print()
        print(vetoed.to_string(index=False))
    print()
    print("【全部入池票的形态分布】")
    print(scanned["形态"].value_counts().to_string())

    out_path = Path(args.out) if args.out else (
        OUT_DIR / f"pattern_veto_scan_top{args.top}.xlsx")
    try:
        with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
            out.to_excel(xw, sheet_name="复算结果", index=False)
            ws = xw.sheets["复算结果"]
            for row in range(2, len(out) + 2):
                ws.cell(row=row, column=2).number_format = "@"
        print(f"\n已写出 {out_path}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"（写出失败：{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
