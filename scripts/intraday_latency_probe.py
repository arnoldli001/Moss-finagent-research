"""做T数据源测速与优先级重排（逐源直测，实测驱动）。

用法：

    uv run python scripts/intraday_latency_probe.py            # 每项 20 次采样
    uv run python scripts/intraday_latency_probe.py --samples 50

什么时候该跑它：**QMT mini 被关闭/重开、换了网络、或换了券商终端之后**。
QMT 的延迟完全取决于本机终端是否在跑 —— 终端没开时样本是"连接失败"，
开了之后是 0.2~16 ms，两者不能混在一张排序表里（这是实际踩过的坑：
在 QMT 关闭期间测出来的排序会把 QMT 判成不可用）。

为什么逐源直测，而不是走 provider 的回退链：
回退链只返回"第一个成功的源"，而它通常就是排序第一的源 —— 拿它采样等于
永远只测冠军，测不出亚军的真实速度，排序也就无从校正。

测完把样本原样喂给 `SourceHealthTracker`，用**它自己的 rank()** 出结论，
而不是脚本里另写一套判断（否则验证的和线上跑的就不是同一件事）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from src.intraday.config import load_intraday_config  # noqa: E402
from src.intraday.sources import IntradayDataProvider  # noqa: E402

SAMPLES = 20
CODES = ["600519", "000001", "300750"]


async def sample(call, rounds: int = SAMPLES) -> dict:
    """重复调用并统计延迟分布（毫秒）；失败单独计数，不混进延迟。"""
    latencies: list[float] = []
    errors: list[str] = []
    for index in range(rounds):
        code = CODES[index % len(CODES)]
        started = time.perf_counter()
        try:
            result = await call(code)
            elapsed = (time.perf_counter() - started) * 1000
            empty = result is None or (
                hasattr(result, "__len__") and len(result) == 0)
            if empty:
                errors.append("空结果")
                continue
            latencies.append(elapsed)
        except Exception as exc:  # noqa: BLE001 测速要把失败如实记下来
            errors.append(f"{type(exc).__name__}: {str(exc)[:70]}")
    if not latencies:
        return {"ok": 0, "fail": len(errors), "error": errors[:2]}
    ordered = sorted(latencies)
    return {
        "ok": len(latencies),
        "fail": len(errors),
        "median_ms": round(statistics.median(ordered), 1),
        "min_ms": round(ordered[0], 1),
        "p90_ms": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1),
        "max_ms": round(ordered[-1], 1),
        "samples_ms": [round(value, 1) for value in ordered],
        "errors": errors[:2],
    }


async def main() -> None:
    config = load_intraday_config()
    print(f"配置：period={config.data.intraday_period} "
          f"timeout={config.data.request_timeout}s "
          f"冷切={config.data.source_cooldown_seconds}s")
    client = httpx.AsyncClient(timeout=config.data.request_timeout,
                               follow_redirects=True)
    provider = IntradayDataProvider(config, client)
    period = config.data.intraday_period
    qmt, tencent, sina, eastmoney = (provider._qmt, provider._tencent,  # noqa: SLF001
                                     provider._sina, provider._eastmoney)

    plan = [
        ("quote", "qmt", lambda code: asyncio.to_thread(
            provider._qmt_quote_sync, code)),  # noqa: SLF001
        ("quote", "tencent", tencent.fetch_quote),
        ("bars", "qmt", lambda code: qmt.fetch_bars(code, period, 5)),
        ("bars", "tencent", lambda code: tencent.fetch_bars(code, period, 5)),
        ("bars", "sina", lambda code: sina.fetch_bars(code, period, 5)),
        ("bars", "eastmoney", lambda code: eastmoney.fetch_bars(code, period, 5)),
        ("trend", "qmt", qmt.fetch_trend),
        ("trend", "tencent", tencent.fetch_trend),
        ("trend", "sina", sina.fetch_trend),
    ]

    results: dict[str, dict] = {}
    print(f"\n每项采样 {SAMPLES} 次（标的轮换 {CODES}）\n")
    header = (f"{'用途':<7}{'数据源':<11}{'成功':>5}{'失败':>5}"
              f"{'中位':>9}{'p90':>9}{'最小':>9}{'最大':>9}  错误")
    print(header)
    print("-" * len(header))
    for method, source, call in plan:
        stats = await sample(call)
        results[f"{method}:{source}"] = stats
        median = stats.get("median_ms")
        cells = ("—" if median is None else f"{median:.1f}",
                 "—" if median is None else f"{stats['p90_ms']:.1f}",
                 "—" if median is None else f"{stats['min_ms']:.1f}",
                 "—" if median is None else f"{stats['max_ms']:.1f}")
        detail = (stats.get("error") or stats.get("errors") or [""])[0]
        print(f"{method:<7}{source:<11}{stats['ok']:>5}{stats['fail']:>5}"
              f"{cells[0]:>9}{cells[1]:>9}{cells[2]:>9}{cells[3]:>9}  "
              f"{detail[:46]}")

    # 把样本原样喂给线上那套排序器 → 用它自己的 rank() 出结论
    for key, stats in results.items():
        method, source = key.split(":", 1)
        for latency in stats.get("samples_ms", []):
            provider.health.record_success(source, method, latency / 1000.0)
        for _ in range(stats.get("fail", 0)):
            provider.health.record_failure(source, method,
                                          (stats.get("error")
                                           or stats.get("errors") or ["失败"])[0])

    # `snapshot()` 返回 {"sources": [...], "ranking": {...}}，不是裸列表
    snapshot = provider.health.snapshot(["qmt", "tencent", "sina", "eastmoney"])
    rows = {(row["source"], row["method"]): row for row in snapshot["sources"]}

    print("\n=== 排序器给出的回退链（按实测中位数，未测量的源按先验排后）===")
    for method in ("quote", "bars", "trend"):
        prior = (["qmt", "tencent"] if method == "quote"
                 else ["qmt", "tencent", "sina"])
        ranked = provider.health.rank(prior, method, prior=prior)
        chain = provider.health.explore_refresh(ranked, method)
        labels = []
        for source in chain:
            row = rows.get((source, method))
            median = None if row is None else row["median_ms"]
            labels.append(f"{source}({'无样本' if median is None else f'{median:.0f}ms'})")
        print(f"  {method:<6} → " + "  ▸  ".join(labels))

    print("\n=== 健康表（真实调用记录）===")
    for row in snapshot["sources"]:
        print(f"  {row['source']:<10}{row['method']:<7}调用{row['calls']:>3} "
              f"失败{row['failures']:>3} 成功{_pct(row['success_rate'])} "
              f"中位{_ms(row['median_ms'])} EWMA{_ms(row['ewma_ms'])} "
              f"样本{row['samples']:>3} {row['note']}")

    await provider.aclose()
    print("\n=== 采样明细 ===")
    print(json.dumps(results, ensure_ascii=False, indent=1)[:4000])


def _ms(value) -> str:
    return "—" if value is None else f"{value:.1f}ms"


def _pct(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="做T数据源延迟实测与优先级重排")
    parser.add_argument("--samples", type=int, default=SAMPLES,
                        help=f"每项采样次数（默认 {SAMPLES}）")
    parser.add_argument("--codes", default=",".join(CODES),
                        help="轮换的标的，逗号分隔")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    SAMPLES = max(3, args.samples)      # 样本不足 3 次排序器不给结论
    CODES = [item.strip() for item in args.codes.split(",") if item.strip()]
    asyncio.run(main())
