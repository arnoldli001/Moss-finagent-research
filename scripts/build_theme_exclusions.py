"""生成「主线题材剔除清单」—— 按历史回测的 **20 日胜率**剔掉判得准的差题材。

## 它做什么

    1. 读完整题材池（`ml_board` 里的概念板块，**包含上次已剔除的**）
    2. 用与「回测收益展示」面板同一处口径，算每个题材的 20 日胜率
    3. 判据：`胜率 ≤ threshold` **且** `已走满窗口 ≥ min_samples` → 剔除
    4. 写成 `configs/mainline_theme_exclusions.yaml`（池级剔除，`boards()` 现读）

判据本身在 `src/mainline/theme_gate.py` 里（纯函数，有单测），这里只负责
取数、落盘与打印。

## 用法

    .venv\\Scripts\\python.exe scripts/build_theme_exclusions.py            # 干跑，只打印
    .venv\\Scripts\\python.exe scripts/build_theme_exclusions.py --write    # 真的写文件
    .venv\\Scripts\\python.exe scripts/build_theme_exclusions.py --threshold 0.45 --min-samples 5
    .venv\\Scripts\\python.exe scripts/build_theme_exclusions.py --keep 886015.TI --write

⚠️ 默认**干跑**（不写文件）：这份清单直接决定哪些题材从系统里消失，
落盘必须是一次显式动作。

## 为什么池子要用 `include_excluded=True` 读

被剔除的题材不再进打分池，但它们的**历史告警还在库里**。如果按默认（已过滤的）
池子读，上次剔掉的题材这次根本不在名单里 → 生成时它们既不在剔除清单也不在观察
名单 → 文件里没有它们 → 下一轮它们又回到池子里。来回振荡，而且每次看起来都
"生成成功"。所以这里显式要完整目录，让"已被剔除"变成一个**显式记录**。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings                        # noqa: E402
from src.mainline import alert_returns, theme_gate              # noqa: E402
from src.mainline.config import PROJECT_ROOT, load_config       # noqa: E402
from src.mainline.datastore import MainlineDataStore            # noqa: E402
from src.mainline.models import BoardKind                       # noqa: E402
from src.mainline.storage import build_mainline_repository      # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threshold", type=float,
                        default=theme_gate.DEFAULT_THRESHOLD,
                        help=f"20 日胜率门槛，**严格大于**它才保留（默认 "
                             f"{theme_gate.DEFAULT_THRESHOLD:.0%}）")
    parser.add_argument("--min-samples", type=int,
                        default=theme_gate.DEFAULT_MIN_SAMPLES,
                        help="至少几个已走满的 20 日窗口才有资格被剔除（默认 "
                             f"{theme_gate.DEFAULT_MIN_SAMPLES}）")
    parser.add_argument("--levels", default=",".join(theme_gate.DEFAULT_LEVELS),
                        help="统计哪些档位的信号（默认全部档位，见 theme_gate 的注释）")
    parser.add_argument("--split", default=alert_returns.DEFAULT_SPLIT,
                        help="历史 / 近期分界日，只影响 segment 标记")
    parser.add_argument("--keep", action="append", default=[],
                        metavar="CODE",
                        help="强制保留的题材代码，可重复（人工白名单，优先级最高）")
    parser.add_argument("--output", default="",
                        help="输出文件（默认 configs/<theme_exclude_file>）")
    parser.add_argument("--write", action="store_true",
                        help="真的写文件。**不加就是干跑**（只打印结论）")
    return parser.parse_args(argv)


def _pool(store: MainlineDataStore) -> list:
    """完整题材池（含上次已剔除的），只取概念板块。"""
    return [item for item in store.boards(include_excluded=True)
            if item.kind is BoardKind.CONCEPT]


def _win_rates(store: MainlineDataStore, repo, levels: list[str], split: str
               ) -> tuple[dict[str, dict], str]:
    """算每个题材的 20 日胜率（口径与面板同一处：`alert_returns`）。"""
    alerts = asyncio.run(repo.load_alerts(level=",".join(levels), start="",
                                          end="", limit=200000))
    calendar = store.calendar("", "")
    rows, _gaps = alert_returns.build_rows(
        alerts, store, calendar=calendar, horizons=(7, 20, 60),
        dedup_days=alert_returns.DEFAULT_DEDUP_DAYS, split=split)
    as_of = max((row.latest_date for row in rows if row.latest_date),
                default=calendar[-1] if calendar else "")
    return {item["board_code"]: item
            for item in alert_returns.summarize_boards(rows)}, as_of


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    levels = [item.strip() for item in str(args.levels).split(",") if item.strip()]
    levels = levels or list(theme_gate.DEFAULT_LEVELS)

    config = load_config()
    store = MainlineDataStore(config=config)
    repo = build_mainline_repository(get_settings())
    pool = _pool(store)
    rates, as_of = _win_rates(store, repo, levels, args.split)
    pool_codes = [item.code for item in pool]
    names = {item.code: item.name for item in pool}

    # 只看池内题材：`summarize_boards` 会带回池外的板块（历史告警残留），
    # 它们本来就不参与主线打分，写进这份清单只会让人误以为"被剔了"。
    known = [rates[code] for code in pool_codes if code in rates]
    excluded, watching = theme_gate.select(
        known, threshold=args.threshold, min_samples=args.min_samples)

    # 池内**一条告警都没有**的题材：连胜率都没有，进观察名单（不剔除）
    quiet = [{"board_code": code, "board_name": names.get(code, ""),
              "win_rate_20d": None, "done_20d": 0, "signals": 0,
              "alert_total": 0, "avg_ret_20d": None}
             for code in pool_codes if code not in rates]
    watching = quiet + watching

    forced = {str(code).strip() for code in args.keep if str(code).strip()}
    if forced:
        keepers = [item for item in excluded
                   if str(item.get("board_code")) in forced]
        excluded = [item for item in excluded
                    if str(item.get("board_code")) not in forced]
        watching = keepers + watching

    print(f"题材池（含上次已剔除）：{len(pool)} 个 · 数据截至 {as_of}")
    print(f"统计档位：{'/'.join(levels)} · 门槛 > {args.threshold:.0%}"
          f" · 最少样本 {args.min_samples} 个窗口")
    print(f"→ 剔除 {len(excluded)} 个 · 保留 {len(pool) - len(excluded)} 个"
          f"（其中观察名单 {len(watching)} 个）")
    if forced:
        print(f"  人工保留 {sorted(forced)}")

    print(f"\n剔除清单（{len(excluded)} 个，按胜率升序）：")
    print(f"  {'题材':<18}{'胜率':>7}{'已走满':>7}{'周期':>6}{'告警':>6}"
          f"{'20日均实际收益':>14}")
    for item in excluded:
        print(f"  {str(item.get('board_name'))[:16]:<18}"
              f"{(item.get('win_rate_20d') or 0) * 100:>6.0f}%"
              f"{int(item.get('done_20d') or 0):>7}"
              f"{int(item.get('signals') or 0):>6}"
              f"{int(item.get('alert_total') or 0):>6}"
              f"{str(item.get('avg_ret_20d')):>14}")

    print(f"\n观察名单（{len(watching)} 个，**仍在池内**）："
          f"没记录 {len(quiet)} 个 + 样本不足 {len(watching) - len(quiet)} 个")
    for item in watching[:20]:
        print(f"  {str(item.get('board_name'))[:16]:<18}"
              f"{theme_gate.watch_reason(item, args.min_samples)}")
    if len(watching) > 20:
        print(f"  …另有 {len(watching) - 20} 个（完整名单见生成文件）")

    text = theme_gate.render(excluded, watching, threshold=args.threshold,
                             min_samples=args.min_samples, pool_size=len(pool),
                             as_of=as_of, levels=levels)
    out = Path(args.output) if args.output else (
        PROJECT_ROOT / "configs" / str(config.universe.theme_exclude_file))
    if not args.write:
        print(f"\n（干跑，未写文件。确认无误后加 --write 写入 {out}）")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\n✅ 已写入 {out}")
    print("   池级剔除立刻生效（`boards()` 按 mtime 现读），**不需要重启**；"
          "下一轮打分与告警就不再包含这些题材。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
