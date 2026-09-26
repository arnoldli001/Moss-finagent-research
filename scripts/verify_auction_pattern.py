"""用 QMT tick 重建**竞价分时序列**，并喂给生产函数复算形态判定。

## 为什么需要它

`auction_snapshot.series`（竞价分时序列）是生产里 `pattern`/`rush_tag` 的唯一输入，
而它只在生产跑过的那几天被落盘（本地只有 0917/0918/0921/0922 四天）。
好消息：**QMT 的 3 秒 tick 里就有这条序列**，且覆盖约 1 个月（20260824 起）。

## 怎么从 tick 还原"竞价撮合价"

竞价时段（09:15~09:25）没有成交，`lastPrice` 恒为 0 —— 真正代表当时撮合意向的
是 **`bidPrice[0]` 与 `askPrice[0]` 相等时的那个价**（集合竞价的参考撮合价）。
实测 002963 在 2026-08-27：09:15:00 起 `bid1 == ask1 == 24.59`，
到 09:24:57 变成 `23.20`，09:25:00 以 23.20 成交 —— 这就是"最后 3 秒跳水"。

产出的序列格式与 `auction_snapshot.series` 一致（`time` / `time_seconds` / `price` /
`matched` / `unmatched`），所以可以直接喂 `features.jump_gap` / `features.classify_pattern`，
**不用重写任何判定逻辑**，避免两套口径漂移。

## 用法

    .venv\\Scripts\\python.exe scripts/verify_auction_pattern.py --code 002963 --date 20260827
    .venv\\Scripts\\python.exe scripts/verify_auction_pattern.py --code 002963 --dates 20260825,20260826,20260827
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("verify_auction_pattern")


def _seconds(stamp: str) -> int | None:
    """'09:24:57' -> 33897"""
    try:
        h, m, s = (int(x) for x in stamp.split(":"))
    except Exception:                                          # noqa: BLE001
        return None
    return h * 3600 + m * 60 + s


def series_from_tick(full_code: str, day: str) -> list[dict[str, Any]]:
    """从 QMT tick 还原竞价分时序列（09:15:00 ~ 09:25:00）。"""
    from xtquant import xtdata

    xtdata.connect()
    xtdata.download_history_data(full_code, "tick", start_time=day, end_time=day)
    data = xtdata.get_market_data_ex([], [full_code], period="tick",
                                     start_time=day, end_time=day, count=-1)
    df = data.get(full_code)
    if df is None or not len(df):
        return []

    out: list[dict[str, Any]] = []
    for stamp, row in df.iterrows():
        hhmmss = str(stamp)[8:14]
        if not ("091500" <= hhmmss <= "092500"):
            continue
        sec = _seconds(f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}")
        if sec is None:
            continue
        bid = row.get("bidPrice")
        ask = row.get("askPrice")
        bvol = row.get("bidVol")
        avol = row.get("askVol")
        last = row.get("lastPrice") or 0.0
        price = None
        if last and float(last) > 0:
            price = float(last)                       # 9:25 那笔是真实成交价
        elif bid and ask and float(bid[0]) > 0 and abs(float(bid[0]) - float(ask[0])) < 1e-9:
            price = float(bid[0])                     # 撮合价 = bid1 == ask1
        if price is None or price <= 0:
            continue
        out.append({
            "time": f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}",
            "time_seconds": sec,
            "price": price,
            "matched": float(bvol[0]) if bvol is not None and len(bvol) else 0.0,
            "unmatched": float(avol[0]) if avol is not None and len(avol) else 0.0,
        })
    return out


def judge(full_code: str, day: str, *, match_price: float | None,
          volume_ratio_pct: float | None, vs_yesterday: float | None,
          streak: float | None) -> None:
    from src.auction_select import config as auction_config
    from src.auction_select import features as F

    cfg = auction_config.load_config()
    series = series_from_tick(full_code, day)
    print(f"\n=== {full_code} {day}：竞价序列 {len(series)} 个点 ===")
    if not series:
        print("  （无竞价序列，tick 未覆盖该日或该股）")
        return
    first, last = series[0], series[-1]
    print(f"  首个点 {first['time']} 价 {first['price']}；"
          f"末个点 {last['time']} 价 {last['price']}")
    window = F.slice_auction_series(series, start="09:19:20", end="09:25:00")
    if window:
        lo = min(p["price"] for p in window)
        hi = max(p["price"] for p in window)
        print(f"  09:19:20~09:25 窗口：{len(window)} 点，区间 {lo} ~ {hi}"
              f"（振幅 {(hi - lo) / lo * 100:.2f}%）")

    jump, jump_meta = F.jump_gap(series, match_price=match_price)
    print(f"  跳空值 = {jump}（{jump_meta.get('points')} 个窗口点"
          f"{'，回退' if jump_meta.get('fallback') else ''}）")
    if jump is not None:
        hard = float(getattr(cfg.features, "rush_out_hard_jump", F.RUSH_OUT_HARD_JUMP))
        down = float(getattr(cfg.features, "rush_jump_down", F.RUSH_JUMP_DOWN))
        print(f"    硬否决线 {hard} → {'⛔ 命中（不看量比、不受豁免）' if jump < hard else '未命中'}")
        print(f"    常规抢跑线 {down} → {'命中（还需量比）' if jump < down else '未命中'}")

    pattern, meta = F.classify_pattern(
        series, volume_vs_yesterday=vs_yesterday, prev_limit_up_streak=streak)
    print(f"  形态判定 = 「{pattern}」")
    for k in ("amplitude", "first", "last", "low", "high", "falling_first_board",
              "falling_ratio", "falling_blocked", "falling_streak"):
        if k in meta:
            print(f"    meta.{k} = {meta[k]}")

    ratio, notes = F.auction_volume_ratio(
        open_volume_hand=None, prev_day_volume_hand=None)
    print(f"  （量比由调用方提供：竞价量比 {volume_ratio_pct}%、"
          f"今昨竞比 {vs_yesterday}、昨日连板 {streak}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="用 tick 复算竞价形态判定")
    ap.add_argument("--code", required=True, help="6 位代码，如 002963")
    ap.add_argument("--date", default="", help="单个交易日 YYYYMMDD")
    ap.add_argument("--dates", default="", help="多个交易日，逗号分隔")
    ap.add_argument("--match-price", type=float, default=None, help="9:25 撮合价")
    ap.add_argument("--volume-ratio", type=float, default=None, help="竞价量比%")
    ap.add_argument("--vs-yesterday", type=float, default=None, help="今昨竞比")
    ap.add_argument("--streak", type=float, default=None, help="昨日连板高度")
    args = ap.parse_args()

    logging.basicConfig(level=logging.ERROR)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    code = args.code
    full = code + (".SH" if code.startswith(("6", "9")) else ".SZ")
    days = [d.strip() for d in (args.dates or args.date).split(",") if d.strip()]
    for day in days:
        judge(full, day, match_price=args.match_price,
              volume_ratio_pct=args.volume_ratio,
              vs_yesterday=args.vs_yesterday, streak=args.streak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
