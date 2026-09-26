"""主线挖掘：只为**分析池内**的板块回补历史行情与资金流。

## 为什么必须限定范围

`sync_board_bars` 默认同步 `self.boards()` 里的**全部**板块。本地
`ml_board_bar` 已有 2240 个板块的痕迹（早期自建名录留下的），全量补 2023-2024
等于 2240 × 3.7 年，绝大多数是黑名单/已剔除的板块，补了也没人读。

用户口径：**只补那 387 个（当前分析池）**。
所以这里显式传 `codes=`，范围就是 `ml_board` 的现有内容。

## 为什么不用 `sync_all`

`sync_all(datasets=["board_bar"])` 不接受代码范围 —— 它是调度策略层的编排，
按"数据集"而不是"标的"组织。范围限定属于本脚本的职责。

## 断点续跑（`--resume`）

每轮先算出**区间覆盖不完整**的板块，只把那些传进 `codes`，已补齐的直接跳过。

判据是**区间两端都要覆盖**，而不是只看"有没有行"：

    区间内最早一条 <= start + 容忍  且  区间内最晚一条 >= end - 容忍

- 只看"有行"会把"只补到一半"的板块误判成已完成 —— 而这恰恰是最危险的情况：
  它看起来有数据，回测照跑，只是结果基于一段缺失的行情，**不会报错**。
- 上限与下限都要，是因为 `sync_board_bars` 每次拉的是**整段**，
  所以"补了一半"只会来自上一轮中断，两端检查正好能识别它。

容忍天数默认 10 个自然日（含周末与短假）。

## 用法

    python scripts/backfill_pool_history.py --start 20240101 --resume
    python scripts/backfill_pool_history.py --start 20240101 --only bars
"""

from __future__ import annotations

import argparse
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402

#: 分析池规模上限（防呆护栏）。
#:
#: 基线演进：拥挤度口径 387 → 用户加入 633 个概念/行业板块后 **748**。
#: 超过这个数说明 `ml_board` 被换成了自建全量名录（2232 个）。
#: 取 1200 是给"未来继续新增板块"留足余量，同时远低于全量。
#: 触发时先跑 `import_crowding_pool()` 把池子恢复成拥挤度口径。
POOL_GUARD = 1200


def _pool_codes(store: MainlineDataStore) -> list[str]:
    """当前分析池的板块代码（`ml_board` 就是池子本身）。"""
    return sorted(item.code for item in store.boards() if item.code)


def _call_with_codes(fn, *, codes: list[str], start: str, end: str):
    """调用同步方法，**仅传它签名里真的有的参数**。

    这样脚本不会因为某个同步方法的签名不一样就崩在半路 ——
    补数通常要跑很久，崩在中途的代价是重跑。

    `progress` 尤其重要：概念板块是**逐板块**调 `ths_daily` 的，
    没有回调就只能盲等一小时，无法区分"在推进"和"卡死了"。
    """
    params = inspect.signature(fn).parameters
    kwargs: dict = {"start": start, "end": end}
    if "codes" in params:
        kwargs["codes"] = codes
    else:
        print(f"  ⚠️ {fn.__name__} 不接受 codes，将同步全部板块")
    if "progress" in params:
        kwargs["progress"] = _progress
    else:
        print(f"  ⚠️ {fn.__name__} 不接受 progress，本轮无进度输出")
    return fn(**kwargs)


def _progress(text: str) -> None:
    """逐条进度：原地刷新一行，避免 380 行把终端刷爆。

    `\\r` 而不是 `\\n`：这个循环每秒都在报，换行会让真正重要的
    汇总信息被冲到屏幕外。末尾补空格是为了盖掉上一次的长残留。
    """
    sys.stdout.write(f"\r    {text[:96]:<96}")
    sys.stdout.flush()


def _covered(store: MainlineDataStore, codes: list[str], start: str, end: str,
             slack_days: int) -> set[str]:
    """区间**两端都已覆盖**的板块代码集合（`--resume` 的判据）。

    两端都要：只查"有没有行"会把"只补到一半"当成已完成，而那种板块
    看起来有数据、回测照跑、结果基于缺失行情却**不报错**。
    """
    if not codes:
        return set()
    floor = _shift(start, int(slack_days))
    ceil = _shift(end, -int(slack_days))
    out: set[str] = set()
    for chunk in range(0, len(codes), 400):
        part = codes[chunk:chunk + 400]
        marks = ",".join("?" for _ in part)
        rows = store._read(  # noqa: SLF001 只读探测
            "SELECT board_code, MIN(trade_date) mn, MAX(trade_date) mx"
            " FROM ml_board_bar WHERE board_code IN (" + marks + ")"
            " AND trade_date BETWEEN ? AND ? GROUP BY board_code",
            (*part, start, end))
        for row in rows:
            if str(row["mn"] or "") <= floor and str(row["mx"] or "") >= ceil:
                out.add(str(row["board_code"]))
    return out


def _shift(day: str, days: int) -> str:
    """`YYYYMMDD` 平移若干自然日（解析失败时原样返回）。"""
    from datetime import datetime, timedelta

    text = str(day or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (moment + timedelta(days=int(days))).strftime("%Y%m%d")
    return text


def _report(name: str, result) -> None:
    print()          # 结束原地刷新那一行，让汇总从行首开始
    print(f"  {name}: {result.status} | 写入 {result.rows} 行 | "
          f"{result.seconds:.1f}s | {result.message}")
    if getattr(result, "missing", None):
        print(f"    未取到 {len(result.missing)} 项：{result.missing[:8]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只为分析池内的板块回补历史数据")
    parser.add_argument("--start", default="20240101", help="起点（含）")
    parser.add_argument("--end", default="", help="终点（含）；留空取最新交易日")
    parser.add_argument("--only", default="", choices=["", "bars", "flows"],
                        help="只补其中一个数据集")
    parser.add_argument("--resume", action="store_true",
                        help="跳过区间两端都已覆盖的板块（中断后接着跑用）")
    parser.add_argument("--slack-days", type=int, default=10,
                        help="两端覆盖判定的容忍自然日数（默认 10）")
    args = parser.parse_args(argv)

    config = load_config()
    store = MainlineDataStore(config=config)
    codes = _pool_codes(store)
    if not codes:
        print("❌ 分析池为空（先跑 board_crowding 同步）")
        return 2
    # 护栏：`ml_board` 被别的东西换回全量名录时，池子会从 387 涨到两千多，
    # 而**下载量会跟着涨 6 倍且不会报错** —— 只是从"几分钟"变成"几小时"，
    # 看起来像网络慢。用户明确要求只补分析池，所以这里直接拒绝而不是提醒。
    # 触发时先跑 `import_crowding_pool()` 把池子恢复成拥挤度口径。
    if len(codes) > POOL_GUARD:
        print(f"❌ 分析池有 {len(codes)} 个板块（上限 {POOL_GUARD}）——"
              "`ml_board` 很可能被换成了全量名录。\n"
              "   先执行：python -c \"from src.mainline.config import load_config;"
              "from src.mainline.datastore import MainlineDataStore;"
              "print(MainlineDataStore(config=load_config())"
              ".import_crowding_pool().message)\"")
        return 2
    end = args.end
    if not end:
        rows = store._read("SELECT MAX(trade_date) AS d FROM ml_etf")  # noqa: SLF001
        end = str(rows[0]["d"] or "") if rows else ""
    if not end:
        print("❌ 无法确定终点")
        return 2

    print(f"分析池 {len(codes)} 个板块，区间 {args.start} ~ {end}")
    if args.resume and args.only != "flows":
        done = _covered(store, codes, args.start, end, args.slack_days)
        todo = [code for code in codes if code not in done]
        print(f"--resume：已覆盖 {len(done)} 个，待补 {len(todo)} 个")
        codes = todo
        if not codes:
            print("✅ 无需补数")
            return 0
    print(f"（全量会是 2240 个，本次限定为池内 {len(codes)} 个）\n")
    begun = time.monotonic()

    if args.only in ("", "bars"):
        print("[1/2] 板块指数日线（ths_daily 逐板块全区间）")
        try:
            _report("board_bar", _call_with_codes(
                store.sync_board_bars, codes=codes, start=args.start, end=end))
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ board_bar 失败：{type(exc).__name__}: {exc}")

    if args.only in ("", "flows"):
        print("\n[2/2] 板块资金流（按成分股聚合）")
        try:
            _report("board_flow", _call_with_codes(
                store.sync_board_flows, codes=codes, start=args.start, end=end))
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ board_flow 失败：{type(exc).__name__}: {exc}")

    print(f"\n耗时 {(time.monotonic() - begun) / 60:.1f} 分")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
