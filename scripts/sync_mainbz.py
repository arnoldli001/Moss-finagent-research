"""同步主营业务构成（tushare `fina_mainbz`，分产品，含营收）。

## 为什么需要它

`ml_company_business` 存的是**一段自由文本**（`stock_company` 的
main_business + business_scope + introduction，平均 747 字）。它有两个问题：

1. **长且杂**：认证资质、子公司、荣誉都在里面，而决定"属于哪个题材"的
   往往只是其中一句 —— 我们为此把 prompt 里的描述放到 2400 字（截断会
   丢判断依据，实测把 `001314 亿道信息` 的 XR 业务切掉过）。
2. **不可核对**：LLM 判"高相关"时，没有任何独立的业务数据来验证。

`fina_mainbz(type='P')` 给的是**分产品营收构成**：`bz_item`（业务名）+
`bz_sales`（营收）。据此可以算**营收占比前 3 的业务**，用途：

- **交叉验证** LLM 判定：说某股与"存储芯片"高度相关，但营收前 3 里
  没有存储相关业务 → 可疑，值得复核
- 将来可把"营收前 3 业务名"作为结构化输入补进 prompt，比 2400 字自由文本精准

⚠️ **它不会直接给出概念映射**：`bz_item` 是"半导体存储器"这类业务名，
映射到"存储芯片"这种概念名仍是语义任务，不是查表能解决的。

## 为什么逐股票调用

`fina_mainbz` 的批量口径（按 `period` 一次取全市场）有行数上限，
且我们只需要**已上市股票的最近一期**，逐股票最稳、也最容易断点续跑。

## 用法

    python scripts/sync_mainbz.py --limit 50     # 先试 50 只
    python scripts/sync_mainbz.py                # 全部（可反复运行，自动跳过已同步）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.errors import BRIEF_TIGHT, brief  # noqa: E402

CACHE_DB = "data/mainline_cache.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_company_segment (
    code TEXT NOT NULL          -- 6 位 A 股代码（与其余 ml_* 表口径一致）
        , ts_code TEXT NOT NULL DEFAULT ''
    , end_date TEXT NOT NULL DEFAULT ''   -- 报告期
    , item TEXT NOT NULL DEFAULT ''       -- 业务名（bz_item）
    , sales REAL                          -- 营收（元）
    , profit REAL
    , cost REAL
    , curr_type TEXT NOT NULL DEFAULT ''
    , updated_at TEXT NOT NULL DEFAULT ''
    , PRIMARY KEY (code, end_date, item)
);
CREATE INDEX IF NOT EXISTS idx_seg_code ON ml_company_segment(code);
CREATE INDEX IF NOT EXISTS idx_seg_item ON ml_company_segment(item);
"""


def to_ts_code(code: str) -> str:
    """6 位代码 → tushare 的 ts_code（按号段推断交易所）。"""
    text = str(code or "").strip()
    if "." in text:
        return text
    if text.startswith(("60", "68", "9")):
        return f"{text}.SH"
    if text.startswith(("00", "30", "20")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return f"{text}.SH"


def targets(conn: sqlite3.Connection, limit: int) -> list[str]:
    """已同步过的不再取（断点续跑）；按代码排序保证顺序稳定。"""
    done = {str(r[0]) for r in conn.execute(
        "SELECT DISTINCT code FROM ml_company_segment")}
    codes = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT code FROM ml_company_business"
        " WHERE business <> '' ORDER BY code")]
    todo = [c for c in codes if c not in done]
    return todo[:limit] if limit else todo


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass
    parser = argparse.ArgumentParser(description="同步主营业务构成 fina_mainbz")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 只（试跑）")
    parser.add_argument("--type", default="P", help="P=按产品（默认）/ D=按地区")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="每次调用后的额外间隔（秒），限频时用")
    args = parser.parse_args(argv)

    from src.quant.tushare_source import TushareClient, resolve_token

    conn = sqlite3.connect(CACHE_DB)
    conn.executescript(SCHEMA)
    conn.commit()
    todo = targets(conn, args.limit)
    if not todo:
        print("✅ 已全部同步")
        return 0
    print(f"待同步 {len(todo)} 只（已同步的自动跳过）")

    client = TushareClient(token=resolve_token())
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    begun = time.monotonic()
    ok = empty = failed = 0
    rows_written = 0
    for index, code in enumerate(todo, 1):
        ts_code = to_ts_code(code)
        try:
            frame = client.call("fina_mainbz", ts_code=ts_code, type=args.type)
        except Exception as exc:  # noqa: BLE001 单只失败不中断整批
            failed += 1
            if failed <= 10:
                print(f"  {code} 失败：{brief(exc, BRIEF_TIGHT)}")
            continue
        if frame is None or len(frame) == 0:
            empty += 1
            continue
        payload = []
        for row in frame.to_dict("records"):
            item = str(row.get("bz_item") or "").strip()
            if not item:
                continue
            payload.append((
                code, str(row.get("ts_code") or ts_code),
                str(row.get("end_date") or ""), item,
                row.get("bz_sales"), row.get("bz_profit"), row.get("bz_cost"),
                str(row.get("curr_type") or ""), now))
        if payload:
            conn.executemany(
                "INSERT INTO ml_company_segment(code, ts_code, end_date, item,"
                " sales, profit, cost, curr_type, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(code, end_date, item) DO UPDATE SET"
                " sales=excluded.sales, profit=excluded.profit,"
                " cost=excluded.cost, curr_type=excluded.curr_type,"
                " updated_at=excluded.updated_at", payload)
            rows_written += len(payload)
            ok += 1
        else:
            empty += 1
        if index % 200 == 0:
            conn.commit()
            rate = (time.monotonic() - begun) / index
            print(f"  {index}/{len(todo)}  已入库 {rows_written} 行  "
                  f"有数据 {ok} / 空 {empty} / 失败 {failed}  "
                  f"| 已用 {rate * index / 60:.1f} 分，"
                  f"预计还需 {rate * (len(todo) - index) / 60:.1f} 分",
                  flush=True)
        if args.sleep:
            time.sleep(args.sleep)
    conn.commit()
    print(f"\n完成：有数据 {ok} / 空 {empty} / 失败 {failed}，"
          f"写入 {rows_written} 行，耗时 {(time.monotonic() - begun) / 60:.1f} 分")
    total = conn.execute("SELECT COUNT(*) FROM ml_company_segment").fetchone()[0]
    codes = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM ml_company_segment").fetchone()[0]
    print(f"ml_company_segment 累计 {total} 行 / {codes} 只股票")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
