"""审计：**主线挖掘「刷新」要的数据，分别来自哪个源、现在能不能拿到**。

## 用户的问题

> 「检查下主线挖掘 点击刷新 需要的数据是否从其他数据源能否获取完整，
>   当前我 QMT 没有权限获取数据了」

所以要回答两件事：
1. **刷新到底依赖哪些源**（是不是依赖 QMT）；
2. 每个数据集**现在能不能拿到**，QMT 掉权限之后**有没有替代源**。

## 已知的架构事实（来自 `src/mainline/datastore.py` / `sources.py` 的模块文档）

    warehouse.db（15 GiB，只读） 个股日线 / 资金流 / 流通市值 / 财务 → Tushare 口径
    mainline_cache.db            板块指数 / 板块资金流 / 两融 / 北向 / 龙虎榜 /
                                 宏观 / 期货 —— "Tushare 才有的东西"
    三个源：TushareSource / WarehouseSource / EastmoneySource（降级通道）
    **QMT（xtquant）只出现在行情路由与竞价链路，主线一个字段都不用。**

本脚本把这三件事**实测**出来，而不是照抄文档：
1. `SYNC_ORDER` 里每个数据集 → 调哪个方法 → 读哪个源；
2. 每个源**现在**是否可用（token / 库 / 目录 / 文件时间）；
3. 本地缓存各表的**末端日期**，指出"哪个数据集卡在哪一天"。

只读，不写库。用法：
    .venv\\Scripts\\python.exe scripts/audit_mainline_sources.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CACHE_DB = ROOT / "data" / "mainline_cache.db"
WAREHOUSE = ROOT / "data" / "quant" / "warehouse.db"
TUSHARE_DIR = ROOT / "data" / "quant" / "tushare"

#: 手工维护的「数据集 → 数据源」映射，**依据是代码里真正调用的东西**
#: （`sources.TushareSource` / `WarehouseSource` / `EastmoneySource`），
#: 不是照抄文档。每一项都在注释里写清"凭什么这么归"。
DATASET_SOURCE: dict[str, tuple[str, str]] = {
    "calendar": ("Tushare", "`trade_cal`（落库缓存到 ml_calendar）"),
    "board_crowding": ("拥挤度库（本地）", "`sector_crowding_list`，不联网"),
    "member_crowding": ("拥挤度库（本地）", "`sector_member`，不联网"),
    "member": ("Tushare", "`ths_member` 按个股反查概念（比遍历板块快两个数量级）"),
    "stock_meta": ("本地行情仓", "`quant_daily` 取个股上市日期"),
    "board_bar": ("Tushare", "`ths_daily` 板块指数（source 实测 tushare:ths_daily）"),
    "board_flow": ("本地行情仓", "成分股 `quant_moneyflow` 聚合（**回测唯一可行口径**）"),
    "margin": ("Tushare", "`margin_detail`"),
    "northbound": ("Tushare", "`hk_hold`；2024-08 起交易所停止个股级披露 → 如实留缺口"),
    "holder": ("Tushare", "`stk_holdernumber` 股东户数"),
    "seat": ("Tushare", "`top_list` / `top_inst` 龙虎榜"),
    "macro": ("Tushare", "宏观接口"),
    "future_meta": ("Tushare", "期货品种名录"),
    "future": ("Tushare", "`fut_daily`"),
    "etf_meta": ("Tushare", "ETF 名录"),
    "etf": ("Tushare", "ETF 日线"),
    "index": ("Tushare", "指数日线"),
}


def main() -> int:
    print("=" * 78)
    print("一、刷新依赖哪些数据集、分别来自哪个源")
    print("=" * 78)
    from src.mainline.datastore import SYNC_ORDER
    qmt = []
    for name in SYNC_ORDER:
        source, why = DATASET_SOURCE.get(name, ("（未登记）", ""))
        if "QMT" in source or "xtquant" in source:
            qmt.append(name)
        print(f"  {name:16s} ← {source:16s} {why}")
    print()
    print(f"  ⚠️ 其中依赖 QMT 的数据集：{qmt or '**一个都没有**'}")

    print()
    print("=" * 78)
    print("二、QMT 的影子在哪（说明它影响的是另一条线）")
    print("=" * 78)
    hits = []
    for path in (ROOT / "src").rglob("*.py"):
        if "mainline" in path.parts or "sector_crowding" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"xtquant|xtdata|XtMiniQmt", text):
            hits.append(path.relative_to(ROOT).as_posix())
    print(f"  非主线模块里出现 xtquant 的文件（{len(hits)} 个）：")
    for item in sorted(hits):
        print(f"    {item}")
    ml_hits = [p.relative_to(ROOT).as_posix()
               for p in (ROOT / "src" / "mainline").rglob("*.py")
               if re.search(r"xtquant|xtdata|XtMiniQmt", p.read_text(
                   encoding="utf-8", errors="ignore"))]
    print(f"  **src/mainline 下出现 xtquant 的文件：{ml_hits or '一个都没有 ✅'}**")

    print()
    print("=" * 78)
    print("三、各源**现在**是否可用")
    print("=" * 78)
    try:
        from src.core.config import get_settings
        settings = get_settings()
        token = getattr(settings, "tushare_token", "") or ""
        shown = f"有值 len={len(token)}" if token else "**空**"
        print(f"  Tushare token : {shown}")
        print(f"  本地CSV目录    : {getattr(settings, 'local_quote_dir', '') or '（空）'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读 settings 失败：{type(exc).__name__}: {exc}")
    print(f"  环境变量 TUSHARE_TOKEN: {'有' if os.environ.get('TUSHARE_TOKEN') else '空'}")
    import importlib.util as util
    for mod in ("tushare", "akshare", "xtquant"):
        print(f"  库 {mod:9s}: {'已安装' if util.find_spec(mod) else '**未安装**'}")
    size = (f"存在 {WAREHOUSE.stat().st_size / 2**30:.1f} GiB"
            if WAREHOUSE.exists() else "**不存在**")
    print(f"  warehouse.db  : {size}")
    parts = sorted(p.name for p in TUSHARE_DIR.rglob("*") if p.is_dir()
                   and not any(c.isdigit() for c in p.name))
    parts_note = (f"存在（{len(parts)} 个数据集：{'、'.join(parts[:6])}）"
                  if TUSHARE_DIR.exists() else "**不存在**")
    print(f"  Tushare 分区  : {parts_note}")

    print()
    print("=" * 78)
    print("四、本地缓存各表末端日期（哪个数据集卡住了）")
    print("=" * 78)
    if CACHE_DB.exists():
        conn = sqlite3.connect(f"file:{CACHE_DB.as_posix()}?mode=ro", uri=True)
        for table in ("ml_calendar", "ml_board_bar", "ml_board_flow", "ml_margin",
                      "ml_northbound", "ml_seat", "ml_macro", "ml_future",
                      "ml_etf", "ml_index", "ml_stock_meta"):
            try:
                cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
                col = next((c for c in ("trade_date", "updated_at") if c in cols), None)
                if not col:
                    continue
                row = conn.execute(
                    f"SELECT MAX({col}), COUNT(*) FROM {table}").fetchone()
                print(f"  {table:16s} max({col})={str(row[0]):<12} 行数={row[1]}")
            except sqlite3.Error as exc:
                print(f"  {table:16s} 读取失败：{exc}")
        conn.close()
    if WAREHOUSE.exists():
        w = sqlite3.connect(f"file:{WAREHOUSE.as_posix()}?mode=ro", uri=True)
        print()
        print("  行情仓（个股级，board_flow 的原料）：")
        for table in ("quant_daily", "quant_daily_basic", "quant_moneyflow",
                      "quant_adj_factor", "quant_index_daily", "quant_stk_limit"):
            try:
                row = w.execute(
                    f"SELECT MAX(trade_date), COUNT(*) FROM {table}").fetchone()
                print(f"    {table:20s} max={str(row[0]):<12} 行数={row[1]}")
            except sqlite3.Error as exc:
                print(f"    {table:20s} 读取失败：{exc}")
        w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
