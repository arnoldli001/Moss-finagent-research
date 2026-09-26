"""竞价过程（09:15~09:25 每 3 秒）增量采集 —— **防过期**流水线。

## 为什么必须每天跑

竞价过程只有三个来源，**全都过了当天就没了**：

| 源 | 能力 | 结论 |
|---|---|---|
| QMT tick | 服务器只保留约 **1 个月** | 今天不下，一个月后就没 |
| eltdx 实时推 | 只有盘中 9:15~9:25 那一刻 | 收盘即失 |
| 生产落盘录像带 | 只在服务跑着的时候才有 | 服务不开就没有 |

项目源码里已经写明这个边界：`features.py`「QMT 没有 tick 历史」、
`sources.py` 能力表 `QMT/xtquant ❌ 无 tick 历史`、
`service.py`「eltdx 拿不到历史竞价」。

所以本模块做成**幂等 + 可补课**的每日任务：不管今天是不是第一次跑、
中间漏了几天，每次运行都会把"最近 N 个交易日里还缺的"补齐。

## 采集谁

两个池子分开存，互不覆盖：

    limit_up   当日涨停股（沪深主板/创业板/科创板/北证，剔 ST）
    hot_pool   当日涨停 **且** 流通市值 (20,110) 亿 **且** 昨收 < 45 元
               **且** 非科创板(688/689) **且** 非北证 —— 用户口径

默认两个池子都采（并集去重）。`hot_pool` 是回测直接用的；
`limit_up` 留着，将来改口径不用重下。

## 采集哪一天

对每个"待补交易日" D，采 **D 当天** 的竞价过程。注意语义：
某只票在 D 涨停 → 它的**次日**（D+1）竞价才是"接力买点"，
而 D+1 的竞价是 D+1 当天采的 —— 所以只要**每个交易日都跑一次**，
D 和 D+1 的竞价都会各自被采到，不需要在 D 那天就去抓 D+1。

## 落盘

    data/auction_hist/tick_auction_<YYYYMMDD>.parquet   逐日竞价过程（09:15~09:31）
    data/auction_hist/auction_candidates_<YYYYMMDD>.parquet  该日候选名单（审计用）
    data/auction_hist/_progress.json                    断点/审计

## 用法

    # 日常：补齐最近 5 个交易日缺的
    .venv\\Scripts\\python.exe scripts/daily_auction_tick.py

    # 回补：指定区间
    .venv\\Scripts\\python.exe scripts/daily_auction_tick.py --from 20260824 --to 20260922

    # 全市场（不限于候选票，慢但全）
    .venv\\Scripts\\python.exe scripts/daily_auction_tick.py --scope all
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("daily_auction_tick")

#: QMT 数据接口。**放在模块级**是为了让单测能 monkeypatch 它
#: （放在函数里 import 的话，patch `sys.modules` 也打不到 —— 实测踩过）。
try:
    from xtquant import xtdata  # type: ignore[import-not-found]
except Exception:                                              # noqa: BLE001
    xtdata = None                                             # type: ignore[assignment]

OUT = ROOT / "data" / "auction_hist"
DB = ROOT / "data" / "quant" / "warehouse.db"
PROGRESS = OUT / "_progress.json"

#: 竞价时段（含 9:25 撮合与随后两根，便于对齐）
AM_START = "091500"
AM_END = "093100"
#: 默认回补窗口（交易日）
LOOKBACK_DAYS = 5
#: 用户口径
HOT_MIN_MV = 20e8
HOT_MAX_MV = 110e8
HOT_MAX_PRICE = 45.0


# ------------------------------------------------------------------ 基础


def _load_progress() -> dict[str, Any]:
    if PROGRESS.exists():
        try:
            # ⚠️ 用 utf-8-sig：Windows 上 PowerShell/编辑器写回的文件常带 BOM，
            #    用 utf-8 读会直接抛 JSONDecodeError（实测踩过），
            #    而这里一旦抛异常就会把整天的采集记录当成"没跑过"。
            return json.loads(PROGRESS.read_text(encoding="utf-8-sig"))
        except Exception:                                      # noqa: BLE001
            return {}
    return {}


def _save_progress(data: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 不带 BOM 写（utf-8），读的时候兼容 BOM（utf-8-sig）—— 两端不对称会咬人
    PROGRESS.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def trade_days(start: str, end: str) -> list[str]:
    """仓库里有日线的交易日（升序）。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM quant_daily "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start, end)).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def latest_trade_day(before_or_on: str | None = None) -> str | None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        if before_or_on:
            r = conn.execute("SELECT MAX(trade_date) FROM quant_daily "
                             "WHERE trade_date <= ?", (before_or_on,)).fetchone()
        else:
            r = conn.execute("SELECT MAX(trade_date) FROM quant_daily").fetchone()
    finally:
        conn.close()
    return str(r[0]) if r and r[0] else None


def candidates_for(day: str) -> pd.DataFrame:
    """当日涨停股 + 其中的"热池"（用户口径）。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        q = """
        SELECT d.code, d.close, d.pre_close, b.circ_mv, l.up_limit,
               b.total_mv, b.turnover_rate
        FROM quant_daily d
        JOIN quant_stk_limit l   ON l.code=d.code AND l.trade_date=d.trade_date
        LEFT JOIN quant_daily_basic b ON b.code=d.code AND b.trade_date=d.trade_date
        WHERE d.trade_date = ?
        """
        df = pd.read_sql(q, conn, params=[day])
    finally:
        conn.close()
    if not len(df):
        return df
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["涨停"] = (pd.to_numeric(df["close"], errors="coerce")
                  - pd.to_numeric(df["up_limit"], errors="coerce")).abs() < 0.005
    df = df[df["涨停"]].copy()
    if not len(df):
        return df
    # ST：仓库目录只有当前名称，用「官方涨停价反推限幅 ≈5%」判（时点化）
    pc = pd.to_numeric(df["pre_close"], errors="coerce")
    ul = pd.to_numeric(df["up_limit"], errors="coerce")
    ratio = (ul / pc - 1) * 100
    df["is_st"] = ((ratio >= 3.5) & (ratio <= 6.5)).fillna(False)

    def board(c: str) -> str:
        if c.startswith(("600", "601", "603", "605")): return "沪主板"
        if c.startswith(("000", "001", "002", "003")): return "深主板"
        if c.startswith(("300", "301")): return "创业板"
        if c.startswith(("688", "689")): return "科创板"
        return "北证/其他"
    df["board"] = df["code"].map(board)
    mv = pd.to_numeric(df["circ_mv"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    pc_num = pd.to_numeric(df["pre_close"], errors="coerce")
    df["hot_pool"] = (
        (~df["is_st"])
        & (~df["board"].isin(["科创板", "北证/其他"]))
        & mv.between(HOT_MIN_MV, HOT_MAX_MV, inclusive="neither")
        & (pc_num < HOT_MAX_PRICE)
    )
    return df


def existing_codes(day: str) -> set[str]:
    """该日 tick 文件里已经采到的代码。"""
    p = OUT / f"tick_auction_{day}.parquet"
    if not p.exists():
        return set()
    try:
        df = pd.read_parquet(p, columns=["code"])
    except Exception:                                          # noqa: BLE001
        return set()
    return set(df["code"].astype(str).str.zfill(6))


def collect_day(day: str, codes: list[str], *, merge: bool = True) -> dict[str, Any]:
    """采 `day` 的竞价过程（09:15~09:31）并落盘。返回统计。"""
    if xtdata is None:                                        # pragma: no cover
        return {"day": day, "requested": len(codes), "saved": 0,
                "failed": len(codes), "note": "xtquant 不可用"}
    xtdata.connect()
    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"tick_auction_{day}.parquet"
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    began = time.time()
    for i, code6 in enumerate(codes, 1):
        full = code6 + (".SH" if code6.startswith(("6", "9")) else ".SZ")
        try:
            xtdata.download_history_data(full, "tick",
                                         start_time=f"{day}{AM_START}",
                                         end_time=f"{day}{AM_END}")
            d = xtdata.get_market_data_ex([], [full], period="tick",
                                          start_time=f"{day}{AM_START}",
                                          end_time=f"{day}{AM_END}", count=-1)
        except Exception as exc:                               # noqa: BLE001
            failed.append(code6)
            continue
        df = d.get(full)
        if df is None or not len(df):
            failed.append(code6)
            continue
        keep = df[["time", "lastPrice", "volume", "amount", "askPrice",
                   "bidPrice", "askVol", "bidVol"]].copy().reset_index(drop=True)
        keep["code"] = code6
        keep["trade_date"] = day
        frames.append(keep)
        if i % 300 == 0:
            logger.warning("  %s %d/%d  %.0fs", day, i, len(codes), time.time() - began)

    if not frames:
        return {"day": day, "requested": len(codes), "saved": 0,
                "failed": len(failed), "file": str(out_path)}

    new = pd.concat(frames, ignore_index=True)
    # ⚠️ 合并旧文件时要**先读回来**再拼。曾经在 1 分钟线那支脚本上踩过：
    #    每批 to_parquet 覆盖写、却不读回旧文件 → 前面批次被静默冲掉，
    #    最终 5,224 只只剩 225 只，而进度文件还记着"全部完成"。
    if merge and out_path.exists():
        try:
            old = pd.read_parquet(out_path)
            old = old[~old["code"].astype(str).str.zfill(6).isin(
                set(new["code"].astype(str)))]
            new = pd.concat([old, new], ignore_index=True)
        except Exception as exc:                               # noqa: BLE001
            logger.warning("%s 旧文件读取失败，按覆盖处理：%s", out_path.name, exc)
    new.to_parquet(out_path, index=False)
    return {"day": day, "requested": len(codes), "saved": int(new["code"].nunique()),
            "rows": int(len(new)), "failed": len(failed),
            "elapsed": round(time.time() - began, 1), "file": out_path.name}


# ------------------------------------------------------------------ 主流程


def run(from_day: str = "", to_day: str = "", *, lookback: int = LOOKBACK_DAYS,
        scope: str = "candidates", refresh: bool = False) -> dict[str, Any]:
    """补齐区间内缺的竞价过程。

    Args:
        from_day/to_day: 指定区间；空则用"最近 lookback 个交易日"。
        scope: `candidates`=只采当日涨停股（默认，快）；`all`=全市场（慢、全）。
        refresh: True 则忽略已有文件重采。
    """
    end = to_day or latest_trade_day() or datetime.now().strftime("%Y%m%d")
    if from_day:
        start = from_day
    else:
        all_days = trade_days("20200101", end)
        start = all_days[-max(1, lookback)] if all_days else end
    days = trade_days(start, end)
    if not days:
        return {"days": 0, "note": f"{start}~{end} 没有交易日"}

    prog = _load_progress()
    audit: dict[str, Any] = dict(prog.get("audit") or {})
    results: list[dict[str, Any]] = []
    if scope == "all":
        from xtquant import xtdata
        xtdata.connect()
        universe = [c.split(".")[0] for c in xtdata.get_stock_list_in_sector("沪深A股")
                    if c.split(".")[0][:1] in "036"]
    else:
        universe = []

    for day in days:
        if scope == "all":
            todo = [] if (existing_codes(day) and not refresh) else universe
            info = {"limit_up": len(universe), "hot_pool": 0}
        else:
            cand = candidates_for(day)
            if not len(cand):
                # 仓库还没同步到这一天（当天盘中跑就会这样）——如实跳过，不算失败
                results.append({"day": day, "note": "仓库无当日数据（未同步？）"})
                audit[day] = {"status": "no_data", "at": datetime.now().isoformat(timespec="seconds")}
                continue
            have = existing_codes(day)
            want = set(cand["code"].astype(str))
            todo = sorted(want - have) if not refresh else sorted(want)
            info = {"limit_up": int(len(cand)),
                    "hot_pool": int(cand["hot_pool"].sum()),
                    "already": len(have)}
            # 候选名单留档（口径变了还能回查当天采了谁）
            cand[["code", "board", "close", "pre_close", "circ_mv",
                  "hot_pool", "is_st"]].to_parquet(
                OUT / f"auction_candidates_{day}.parquet", index=False)

        if not todo:
            logger.warning("%s 已有 %d 只，无缺口", day, len(existing_codes(day)))
            results.append({"day": day, **info, "saved": len(existing_codes(day)),
                            "note": "已齐"})
            audit[day] = {"status": "complete", "codes": len(existing_codes(day)),
                          "at": datetime.now().isoformat(timespec="seconds")}
            _save_progress({**prog, "audit": audit})
            continue

        logger.warning("%s 待采 %d 只（候选 %s）", day, len(todo), info)
        res = collect_day(day, todo)
        res.update(info)
        results.append(res)
        audit[day] = {"status": "ok" if res.get("saved") else "empty",
                      "codes": res.get("saved"), "failed": res.get("failed"),
                      "at": datetime.now().isoformat(timespec="seconds")}
        _save_progress({**prog, "audit": audit, "last_run": datetime.now().isoformat(timespec="seconds")})

    summary = {
        "range": [days[0], days[-1]],
        "days": len(days),
        "days_with_gap": sum(1 for r in results if r.get("saved") is not None
                             and r.get("note") != "已齐"),
        "results": results,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_progress({**prog, "audit": audit, "last_run": summary["at"],
                    "last_summary": summary["results"][-8:]})
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="竞价过程增量采集（防过期）")
    ap.add_argument("--from", dest="frm", default="", help="起始交易日 YYYYMMDD")
    ap.add_argument("--to", dest="to", default="", help="结束交易日 YYYYMMDD")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                    help="不指定区间时回补最近几个交易日")
    ap.add_argument("--scope", choices=("candidates", "all"), default="candidates",
                    help="candidates=只采当日涨停股（默认）；all=全市场")
    ap.add_argument("--refresh", action="store_true", help="忽略已采到的代码重新采")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass

    res = run(args.frm, args.to, lookback=args.lookback,
              scope=args.scope, refresh=args.refresh)
    if res.get("note"):
        print(res["note"])
        return 0
    print(f"竞价过程采集：{res['range'][0]} ~ {res['range'][1]}（{res['days']} 个交易日）")
    for r in res["results"]:
        if r.get("note"):
            print(f"  {r['day']}: {r['note']}")
        else:
            print(f"  {r['day']}: 涨停 {r.get('limit_up', '-')} 只、"
                  f"热池 {r.get('hot_pool', '-')} 只、本次采 {r.get('saved', '-')} 只"
                  + (f"、失败 {r['failed']}" if r.get("failed") else ""))
    print(f"落盘目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
