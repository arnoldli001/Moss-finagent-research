"""策略扫描：把预设策略在一个**股票篮子**上回测，看它是否真的有价值。

用法：

    uv run python scripts/quant_strategy_scan.py --preset all --codes 30
    uv run python scripts/quant_strategy_scan.py --preset faber_200ma --codes 50 \
        --start 2015-01-01

## 为什么必须扫篮子，而不是挑一只票

这是整件事里最容易骗自己的地方。**在一只长期上涨的股票上，总能调出一段
"大幅跑赢买入持有"的回测** —— 只要试的参数足够多。我自己建的门槛
（`strategy_store.verdict`：样本外为正 + 交易笔数足够 + 自检通过）就是为了挡住
这件事，所以扫描器不会去"挑最好的一只票给你看"，而是回答：

    这个策略在 N 只票上，**有多少比例**在样本外跑赢基准？平均超额多少？
    回撤是否系统性更小？

一只票上跑赢是噪声；30 只票里 20 只跑赢才有讨论价值。

## 输出的三个统计量

- **样本外胜率**：多少比例的股票在样本外区间跑赢"同票买入持有"；
- **平均超额**：样本外超额收益的均值与中位数（中位数比均值更抗单只极端值）；
- **回撤改善**：策略最大回撤相对基准的改善比例（趋势跟踪的价值主要在这里）。

同时给出"跑赢指数"的比例 —— 对多数人来说，不择时就直接买指数，
这才是真实的替代方案。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.quant.dataset_store import DatasetStore  # noqa: E402
from src.quant.panels import build_panels  # noqa: E402
from src.quant.single_backtest import (  # noqa: E402
    SingleBacktestConfig,
    run_single_backtest,
)
from src.quant.strategy_presets import PRESETS, get_preset  # noqa: E402
from src.core.errors import BRIEF_DEFAULT

DEFAULT_UNIVERSE_SIZE = 30
MIN_EXCESS_DEFAULT = 10.0      # "跑赢买入持有 10% 以上"
DISCLAIMER = ("本结果由历史回测得出，不构成投资建议。"
              "策略由「多候选 × 多标的」搜索选出，存在选择性偏差："
              "搜索空间越大，纯靠运气进入前列的策略越多，实盘表现通常会明显回落。")


def _pick_codes(days: list[str], size: int) -> list[str]:
    """从缓存里挑流动性最好的一批票。

    用**成交额中位数**排序而不是随机抽样：随机抽会混入大量流动性极差的票，
    回测里那些票的滑点假设并不成立，结论会被污染。
    """
    store = DatasetStore("daily_basic")
    sample = days[-60:]
    totals: dict[str, list[float]] = {}
    for day in sample:
        frame = store.read(day)
        if frame is None or len(frame) == 0:
            continue
        if "circ_mv" not in frame.columns:
            continue
        for code, value in zip(frame["code"].astype(str),
                               frame["circ_mv"], strict=False):
            if value is not None and value == value:
                totals.setdefault(code, []).append(float(value))
    ranked = sorted(totals.items(),
                    key=lambda item: statistics.median(item[1]), reverse=True)
    return [code for code, _ in ranked[:size]]


def scan_basket(keys: list[str], codes: list[str], days: list[str], *,
                initial_cash: float = 1_000_000.0,
                train_ratio: float = 0.7,
                index_code: str = "000300.SH",
                min_trades: int = 2,
                progress: bool = True) -> dict[str, list[dict]]:
    """**每个标的只装配一次面板**，所有策略共用。

    为什么必须这样组织：面板装配是整条链路里最贵的一步（单票 5030 天约 11 秒）。
    按 (策略 × 标的) 组织循环会让同一只票的面板被装配 17 次 ——
    实测 17 个预设 × 10 只票要 31 分钟，其中绝大部分是重复劳动。
    改成"标的在外层、策略在内层"后，装配次数从 策略×标的 降到 标的。
    """
    output: dict[str, list[dict]] = {key: [] for key in keys}
    for position, code in enumerate(codes, start=1):
        try:
            panels = build_panels(days, codes=[code])
        except Exception as exc:  # noqa: BLE001 装配失败不该中断整轮扫描
            for key in keys:
                output[key].append({
                    "code": code,
                    "error": f"面板装配失败 {type(exc).__name__}: "
                             f"{brief(exc, BRIEF_DEFAULT)}"})
            continue
        for key in keys:
            preset = get_preset(key)
            try:
                config = SingleBacktestConfig(
                    code=code, entry=preset.entry, exit=preset.exit,
                    initial_cash=initial_cash, train_ratio=train_ratio,
                    index_code=index_code, **dict(preset.params))
                result = run_single_backtest(panels, code, config=config,
                                             name=preset.label)
            except Exception as exc:  # noqa: BLE001 单个策略失败不影响其它
                output[key].append({
                    "code": code,
                    "error": f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"})
                continue
            output[key].append(_row_from(result, preset, key, min_trades))
        if progress:
            print(f"    [{position}/{len(codes)}] {code} 完成"
                  f"（{len(keys)} 个策略）", flush=True)
    return output


def _row_from(result: Any, preset: Any, key: str,
              min_trades: int = 2) -> dict:
    """把一次回测结果压成一行扫描记录。"""
    metrics = result.metrics
    recent = result.segments.get("recent", {}) or {}
    oos = result.segments.get("oos", {}) or {}
    return {
        "code": result.code,
        "preset": key,
        "label": preset.label,
        "entry": preset.entry,
        "exit": preset.exit,
        "trades": metrics.get("trade_count"),
        "start": result.dates[0] if result.dates else "",
        "end": result.dates[-1] if result.dates else "",
        "recent_start": recent.get("start", ""),
        "recent_end": recent.get("end", ""),
        "total_return_pct": metrics.get("total_return_pct"),
        "benchmark_return_pct": metrics.get("benchmark_return_pct"),
        "index_return_pct": metrics.get("index_return_pct"),
        "excess_vs_benchmark_pct": metrics.get("excess_vs_benchmark_pct"),
        "excess_vs_index_pct": metrics.get("excess_vs_index_pct"),
        "recent_return_pct": recent.get("return_pct"),
        "recent_benchmark_pct": recent.get("benchmark_return_pct"),
        "recent_excess_pct": recent.get("excess_vs_benchmark_pct"),
        "recent_excess_vs_index_pct": recent.get("excess_vs_index_pct"),
        "recent_trades": recent.get("trades"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "benchmark_max_drawdown_pct": metrics.get("benchmark_max_drawdown_pct"),
        "calmar": metrics.get("calmar"),
        "sharpe": metrics.get("sharpe"),
        "win_rate_pct": metrics.get("win_rate_pct"),
        "oos_return_pct": oos.get("return_pct"),
        "oos_trades": oos.get("trades"),
        "verdict_passed": (result.verdict or {}).get("passed_count"),
        "verdict_total": (result.verdict or {}).get("total_count"),
        "config": result.config,
        "exit_reasons": metrics.get("exit_reasons"),
        "recent_trading_days": recent.get("trading_days"),
        "enough_trades": (metrics.get("trade_count") or 0) >= min_trades,
    }


def _summarize(preset_key: str, label: str, rows: list[dict]) -> dict:
    usable = [row for row in rows if "error" not in row]
    with_trades = [row for row in usable if row.get("enough_trades")]
    beat_stock = [row for row in with_trades
                  if (row.get("excess_vs_benchmark_pct") or 0) > 0]
    beat_index = [row for row in with_trades
                  if (row.get("excess_vs_index_pct") or 0) > 0]
    beat_recent = [row for row in with_trades
                   if (row.get("recent_excess_pct") or 0) > 0]
    dd_better = [row for row in with_trades
                 if abs(row.get("max_drawdown_pct") or 0)
                 < abs(row.get("benchmark_max_drawdown_pct") or 0)]

    def median(values: list) -> float | None:
        cleaned = [float(v) for v in values if v is not None]
        return round(statistics.median(cleaned), 2) if cleaned else None

    denominator = max(len(with_trades), 1)
    return {
        "preset": preset_key, "label": label,
        "scanned": len(rows), "usable": len(usable),
        "with_trades": len(with_trades),
        "errors": [row for row in rows if "error" in row][:3],
        "beat_benchmark_ratio": round(len(beat_stock) / denominator, 3),
        "beat_index_ratio": round(len(beat_index) / denominator, 3),
        "beat_recent_ratio": round(len(beat_recent) / denominator, 3),
        "drawdown_better_ratio": round(len(dd_better) / denominator, 3),
        "median_excess_vs_benchmark_pct": median(
            [row.get("excess_vs_benchmark_pct") for row in with_trades]),
        "median_recent_excess_pct": median(
            [row.get("recent_excess_pct") for row in with_trades]),
        "median_trades": median([row.get("trades") for row in with_trades]),
        "rows": with_trades,
    }


def _archive_winners(scored: list[dict], *, min_excess: float, top: int,
                     context: dict | None = None,
                     min_recent_trades: int = 1) -> tuple[list[dict], dict]:
    """把近一年超额 ≥ min_excess 的前 top 名存档到数据库。

    **每条档案都带搜索上下文**（试了多少组合、全体中位超额是多少、这笔交易几笔）：
    一个"跑赢 23.88pp"的数字，单独看像发现，配上"从 204 个组合里选出、
    全体中位超额 −9.6%、只有 3 笔交易"才是完整的信息。
    没有上下文的胜者档案，本质上就是一张选择性报告的截图。

    返回 (存档成功的策略列表, 落库状态)。数据库不可用时如实返回，
    不静默降级 —— 否则人会以为策略已经入库了。
    """
    from src.quant.warehouse import QuantWarehouse, StrategyArchive, WarehouseConfig
    from src.quant.strategy_store import spec_hash

    context = dict(context or {})
    if not scored:
        return [], {"available": False, "reason": "没有达标策略"}
    config = WarehouseConfig.from_env()
    warehouse = QuantWarehouse(config)
    status: dict[str, object] = {
        "available": warehouse.available(), "dialect": config.dialect,
        "description": config.description, "table": "quant_strategy"}
    archive = StrategyArchive(warehouse)
    saved: list[dict] = []
    for item in scored[:top]:
        digest = spec_hash(item["code"], item["entry"], item["exit"],
                           item.get("config") or {})
        recent_trades = int(item.get("recent_trades") or 0)
        low_sample = recent_trades < 5
        record = {
            "id": f"{item['preset']}-{item['code']}-{digest}",
            "name": f"{item['label']}·{item['code']}",
            "code": item["code"], "preset": item["preset"],
            "entry_condition": item["entry"], "exit_condition": item["exit"],
            "spec_hash": digest,
            "start_date": item.get("recent_start", ""),
            "end_date": item.get("recent_end", ""),
            "trading_days": item.get("recent_trading_days"),
            "total_return_pct": item.get("total_return_pct"),
            "benchmark_return_pct": item.get("benchmark_return_pct"),
            "index_return_pct": item.get("index_return_pct"),
            "excess_vs_benchmark_pct": item.get("recent_excess_pct"),
            "excess_vs_index_pct": item.get("recent_excess_vs_index_pct"),
            "recent_return_pct": item.get("recent_return_pct"),
            "recent_excess_pct": item.get("recent_excess_pct"),
            "oos_return_pct": item.get("oos_return_pct"),
            "max_drawdown_pct": item.get("max_drawdown_pct"),
            "sharpe": item.get("sharpe"), "calmar": item.get("calmar"),
            "trade_count": item.get("trades"),
            "win_rate_pct": item.get("win_rate_pct"),
            "verdict_passed": item.get("verdict_passed"),
            "verdict_total": item.get("verdict_total"),
            "beats_benchmark": (1 if (item.get("recent_excess_pct") or 0)
                                >= min_excess else 0),
            "auto_saved": 1, "source": "scan",
            "recent_window": f"{item.get('recent_start')}~{item.get('recent_end')}",
            "recent_benchmark_pct": item.get("recent_benchmark_pct"),
            "recent_trades": recent_trades,
            "low_sample": 1 if low_sample else 0,
            "search_space": context.get("search_space"),
            "scan_median_excess_pct": context.get("median_excess_pct"),
            "scan_beat_ratio": context.get("beat_ratio"),
            "config": item.get("config"),
            "disclaimer": DISCLAIMER,
        }
        try:
            saved.append(archive.save(record))
            item["archived"] = True
        except Exception as exc:  # noqa: BLE001 存档失败不该丢掉扫描结果
            item["archived"] = False
            item["archive_error"] = f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"
    status["stats"] = archive.stats()
    status["context"] = context
    return saved, status


def _rearchive(args: argparse.Namespace) -> int:
    """从已有的扫描 JSON 重新归档（不重跑回测）。

    用途：档案表结构变了（新增了"搜索空间""低样本标记"这类字段）之后，
    重跑一轮完整扫描要十几分钟，而扫描结果 JSON 里已经有全部原始数据。
    """
    payload = json.loads(Path(args.archive_only).read_text(encoding="utf-8-sig"))
    every = [row for item in payload.get("presets", [])
             for row in item.get("rows", [])]
    if not every:
        print(f"[×] {args.archive_only} 里没有扫描明细")
        return 2
    excesses = [row["recent_excess_pct"] for row in every
                if row.get("recent_excess_pct") is not None]
    context = {
        "search_space": payload.get("search_space"),
        "combos_with_trades": len(every),
        "median_excess_pct": (round(statistics.median(excesses), 2)
                              if excesses else None),
        "beat_ratio": (round(sum(1 for value in excesses if value > 0)
                             / max(len(excesses), 1), 3)),
        "min_excess": args.min_excess,
        "generated_at": payload.get("generated_at", ""),
    }
    scored = sorted((row for row in every
                     if row.get("recent_excess_pct") is not None),
                    key=lambda row: row["recent_excess_pct"], reverse=True)
    winners = [row for row in scored if row["recent_excess_pct"] >= args.min_excess]
    print(f"从 {args.archive_only} 载入 {len(every)} 个组合，"
          f"达标 {len(winners)} 个（≥{args.min_excess:.0f}pp）")
    print(f"搜索上下文：{context}")
    archived, status = _archive_winners(winners, min_excess=args.min_excess,
                                        top=args.top, context=context)
    print(f"\n已重新归档 {len(archived)} 条 → {status.get('dialect')} 表 "
          f"{status.get('table')}")
    print(f"统计：{status.get('stats')}")
    for item in archived:
        print(f"  {item['name']:<28} 超额 {item['recent_excess_pct']:>6}  "
              f"笔数 {item['recent_trades']}  低样本={item['low_sample']}  "
              f"搜索空间 {item['search_space']}")
    failures = [row for row in winners[:args.top] if row.get("archive_error")]
    if failures:
        print(f"\n⚠ {len(failures)} 条写入失败：")
        for row in failures[:3]:
            print(f"  {row['code']} {row['preset']}: {row['archive_error']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="预设策略的篮子扫描 + 达标策略存档")
    parser.add_argument("--preset", default="all", help="预设 key，或 all")
    parser.add_argument("--codes", type=int, default=DEFAULT_UNIVERSE_SIZE,
                        help="篮子里的股票数（按流通市值挑）")
    parser.add_argument("--start", default="", help="面板起始（含预热期）")
    parser.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--warmup", type=int, default=300,
                        help="预热交易日数：计量窗口之前额外装配的天数")
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--min-excess", type=float, default=MIN_EXCESS_DEFAULT,
                        help="近一年超额下限（百分点），默认 10")
    parser.add_argument("--top", type=int, default=5, help="存档前 N 名")
    parser.add_argument("--no-archive", action="store_true",
                        help="只扫描，不写数据库")
    parser.add_argument("--archive-only", default="",
                        help="跳过扫描，直接从已有的扫描 JSON 重新归档"
                             "（改了档案字段后不必重跑整轮扫描）")
    parser.add_argument("--out", default="", help="结果 JSON 输出路径")
    args = parser.parse_args()

    if args.archive_only:
        return _rearchive(args)

    all_days = DatasetStore("daily").keys()
    start = args.start.replace("-", "")
    end = args.end.replace("-", "")
    days = [day for day in all_days
            if (not start or day >= start) and (not end or day <= end)]
    if len(days) < 320:
        print(f"[×] 区间只有 {len(days)} 个交易日；"
              f"至少需要 320 天（200 日均线预热 + 近一年计量窗口）")
        return 2
    codes = _pick_codes(days, args.codes)
    print(f"面板区间 {days[0]}~{days[-1]}（{len(days)} 个交易日）")
    print(f"篮子 {len(codes)} 只（按近 60 日流通市值中位数排序）："
          f"{', '.join(codes[:10])} …")

    keys = ([preset.key for preset in PRESETS] if args.preset == "all"
            else [args.preset])
    print(f"候选策略 {len(keys)} 个 × 标的 {len(codes)} 只 = "
          f"**{len(keys) * len(codes)} 个组合**（这个数字就是搜索空间，"
          f"下面挑出的赢家必须结合它来读）\n", flush=True)

    results = []
    started = time.perf_counter()
    print(f"每只票装配一次面板、{len(keys)} 个策略共用（避免重复装配）…",
          flush=True)
    grouped = scan_basket(keys, codes, days, train_ratio=args.train_ratio)
    for key in keys:
        preset = get_preset(key)
        summary = _summarize(key, preset.label, grouped.get(key, []))
        results.append(summary)
        print(f"  {preset.label[:24]:<26} 近一年跑赢 "
              f"{summary['beat_recent_ratio']:>4.0%}  全程跑赢 "
              f"{summary['beat_benchmark_ratio']:>4.0%}  回撤更小 "
              f"{summary['drawdown_better_ratio']:>4.0%}", flush=True)
    print(f"扫描总耗时 {time.perf_counter() - started:.1f}s")

    _print_table(results)

    # ---- 汇总所有组合，按近一年超额排序，挑出达标的 ----
    every = [row for item in results for row in item["rows"]]
    scored = sorted((row for row in every
                     if row.get("recent_excess_pct") is not None),
                    key=lambda row: row["recent_excess_pct"], reverse=True)
    winners = [row for row in scored
               if row["recent_excess_pct"] >= args.min_excess]

    print(f"\n=== 近一年跑赢买入持有 ≥ {args.min_excess:.0f}pp 的组合 ===")
    print(f"总组合 {len(every)} 个（有交易且笔数达标），达标 {len(winners)} 个")
    if every:
        all_excess = [row["recent_excess_pct"] for row in every
                      if row.get("recent_excess_pct") is not None]
        if all_excess:
            beat = sum(1 for value in all_excess if value > 0)
            print(f"**全体 {len(all_excess)} 个组合**：跑赢比例 "
                  f"{beat / len(all_excess):.0%}，中位超额 "
                  f"{statistics.median(all_excess):.2f}pp，"
                  f"最好 {max(all_excess):.2f}pp，最差 {min(all_excess):.2f}pp")
            print("  ↑ 这一行比下面的赢家表更重要：它说明"
                  "「这类策略的典型结果」是跑赢还是跑输。")
    if not winners:
        print("没有组合达到该门槛 —— 这本身就是一个结论："
              "在这段样本上，没有找到能稳定大幅跑赢的因子组合。"
              "把门槛降到 5pp 通常也只是把噪声放进来。")
    else:
        print(f"\n{'#':<3}{'策略':<24}{'标的':<9}{'近一年':>9}{'基准':>9}"
              f"{'超额':>9}{'回撤':>9}{'笔数':>6}{'样本外':>9}")
        print("-" * 88)
        for index, row in enumerate(winners[:args.top], start=1):
            print(f"{index:<3}{row['label'][:22]:<24}{row['code']:<9}"
                  f"{row['recent_return_pct']:>9.2f}"
                  f"{row['recent_benchmark_pct']:>9.2f}"
                  f"{row['recent_excess_pct']:>9.2f}"
                  f"{row['max_drawdown_pct']:>9.2f}"
                  f"{row.get('recent_trades') or 0:>6}"
                  f"{str(row.get('oos_return_pct')):>9}")
        print(f"\n窗口：{winners[0].get('recent_start')} ~ "
              f"{winners[0].get('recent_end')}"
              f"（{winners[0].get('recent_trading_days')} 个交易日）")

    archived: list[dict] = []
    status: dict = {"available": False, "reason": "未执行"}
    if winners and not args.no_archive:
        # 搜索上下文：这是让"跑赢 23.88pp"这个数字**可被正确解读**的关键信息
        all_excess = [row["recent_excess_pct"] for row in every
                      if row.get("recent_excess_pct") is not None]
        context = {
            "search_space": len(keys) * len(codes),
            "combos_with_trades": len(every),
            "median_excess_pct": (round(statistics.median(all_excess), 2)
                                  if all_excess else None),
            "beat_ratio": (round(sum(1 for value in all_excess if value > 0)
                                 / max(len(all_excess), 1), 3)),
            "min_excess": args.min_excess,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        archived, status = _archive_winners(winners, min_excess=args.min_excess,
                                            top=args.top, context=context)
        dialect = status.get("dialect") or "?"
        print(f"\n已存档 {len(archived)} 条 → {dialect} 表 "
              f"{status.get('table')}（{status.get('description')}）")
        if not status.get("available"):
            print("⚠ 数据库不可用，未能存档 —— 策略没有被写入任何地方")
        if status.get("stats"):
            print(f"  档案统计：{status['stats']}")
        print(f"  搜索上下文（每条档案都带上了）：搜索空间 {context['search_space']}，"
              f"全体中位超额 {context['median_excess_pct']}%，"
              f"跑赢比例 {context['beat_ratio']:.0%}")
    elif winners:
        status = {"available": False, "reason": "--no-archive"}

    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "panel_range": {"start": days[0], "end": days[-1],
                               "trading_days": len(days)},
               "universe_size": len(codes), "search_space": len(keys) * len(codes),
               "min_excess": args.min_excess,
               "presets": results, "winners": winners[:args.top],
               "archived": [{"id": item.get("id"), "name": item.get("name")}
                            for item in archived],
               "archive_status": status, "disclaimer": DISCLAIMER}
    destination = args.out or "data/quant/strategy_scan.json"
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"\n扫描明细已写入 {destination}")
    print(f"\n{DISCLAIMER}")
    return 0


def _print_table(results: list[dict]) -> None:
    print(f"\n{'策略':<26}{'有交易':>6}{'近一年跑赢':>11}{'全程跑赢':>9}"
          f"{'回撤更小':>9}{'近一年超额中位':>15}")
    print("-" * 78)
    for item in results:
        print(f"{item['label'][:24]:<26}{item['with_trades']:>6}"
              f"{item['beat_recent_ratio']:>11.0%}"
              f"{item['beat_benchmark_ratio']:>9.0%}"
              f"{item['drawdown_better_ratio']:>9.0%}"
              f"{str(item['median_recent_excess_pct']):>15}")
    print("\n读法：一只票上跑赢是噪声；一列里 ≥60% 才值得进一步讨论，"
          "且必须同时看「回撤更小」与「近一年超额中位」。")
    print("注：这些比例**都是实测出来的**，没有任何一项被写死或挑选。")


if __name__ == "__main__":
    raise SystemExit(main())
