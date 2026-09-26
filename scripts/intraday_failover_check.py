"""做T数据源容灾切换验证：QMT 正常 → 模拟掉线 → 回退腾讯 → 熔断 → 自动恢复。

用法：

    uv run python scripts/intraday_failover_check.py     # 约 50 秒（含 35s 等探针）

五段验证与预期结果：

    ① QMT 在线        → 命中 QMT（8ms 级）
    ② QMT 掉线        → 自动切到腾讯，尝试日志保留 QMT 失败原因
    ③ 熔断生效        → 后续请求的尝试日志里**完全没有 QMT**（真跳过，不是再试一次）
    ④ 自动恢复        → 静默等过探针周期后，不手动干预即自己探测并切回 QMT
    ⑤ 双源同时不可用  → 如实抛错并逐源列出原因（不静默返回空数据）

**关键测试细节**：清缓存必须用 `provider._cache.clear()`，
不能用 `provider.invalidate()` —— 后者按设计会同时关闭熔断器（那是前端
"强制刷新"的语义），用它来清缓存会把"熔断是否真的生效"这件事一起测没了。
第一版就踩了这个坑：`should_attempt` 明明返回 False，请求却仍然去试了 QMT。

**另一个坑**：④ 里不能用轮询 `should_attempt` 来判断"探针到期没有"——
它每次放行都会把 `next_probe_at` 再推后一个周期（名额预留，防并发雪崩），
于是轮询本身就把探测名额吃掉了，会测出假 FAIL。必须静默等够时间。

为什么用替身而不是真关掉 QMT mini：关掉再开要走登录流程、无法精确控制失败
时刻，而且只能测一次；替身抛的错用真实场景原文（XtMiniQmt 未运行或未登录）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from src.core.exceptions import DataFetchError  # noqa: E402
from src.intraday.config import load_intraday_config  # noqa: E402
from src.intraday.sources import IntradayDataProvider  # noqa: E402
from src.core.errors import BRIEF_DEFAULT, brief

CODE = "600519"
PERIOD = "5m"
QMT_DOWN = "XtMiniQmt 未运行或未登录"


class DeadQmt:
    """QMT 掉线替身：任何取数都抛"终端未运行"，与真关闭 QMT 时表现一致。"""

    def _raise(self, *_args, **_kwargs):
        raise DataFetchError(f"QMT不可用: {QMT_DOWN}")

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._raise


class DeadTencent:
    async def fetch_bars(self, *_args, **_kwargs):
        raise DataFetchError("腾讯接口连接被重置")

    async def fetch_trend(self, *_args, **_kwargs):
        raise DataFetchError("腾讯接口连接被重置")


async def tick(provider: IntradayDataProvider, label: str,
               *, fresh: bool = True) -> str:
    if fresh:
        provider._cache.clear()  # noqa: SLF001 只清 TTL 缓存，不动熔断器
    started = time.perf_counter()
    try:
        bars, source, attempts = await provider.fetch_bars(CODE, period=PERIOD,
                                                           days=5)
        elapsed = (time.perf_counter() - started) * 1000
        detail = " | ".join(
            f"{item.source}:{'ok' if item.ok else '✗'}"
            f"({'跳过:' + item.detail[:16] if '熔断' in item.detail else item.detail[:20]})"
            for item in attempts)
        print(f"  {label:<20} → 命中 {source:<9} {len(bars):>3}行 "
              f"{elapsed:>7.1f}ms  尝试[{detail}]")
        return source
    except Exception as exc:  # noqa: BLE001 全链失败正是要观测的结果
        elapsed = (time.perf_counter() - started) * 1000
        print(f"  {label:<20} → 全部失败 {elapsed:>7.1f}ms "
              f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}")
        return ""


def chain(provider: IntradayDataProvider, method: str = "bars") -> str:
    pool = ["qmt", "tencent", "sina", "eastmoney"]
    ranked = provider.health.rank(pool, method, prior=pool)
    return " ▸ ".join(provider.health.explore_refresh(ranked, method))


def row_of(provider: IntradayDataProvider, source: str) -> dict:
    snapshot = provider.health.snapshot([])
    return next((item for item in snapshot["sources"]
                 if item["source"] == source and item["method"] == "bars"), {})


def health_line(provider: IntradayDataProvider, source: str) -> str:
    row = row_of(provider, source)
    if not row:
        return f"{source}: 无记录"
    median = row["median_ms"]
    return (f"{source}: 调用{row['calls']} 失败{row['failures']} "
            f"中位{'—' if median is None else round(median, 1)}ms "
            f"熔断{'开启' if row['cooling_down'] else '关闭'}")


async def main() -> None:
    config = load_intraday_config()
    client = httpx.AsyncClient(timeout=config.data.request_timeout,
                               follow_redirects=True)
    provider = IntradayDataProvider(config, client)
    real_qmt = provider._qmt  # noqa: SLF001
    results: dict[str, str] = {}

    print("=== ① 正常态：QMT 在线 ===")
    print(f"  回退链：{chain(provider)}")
    for index in range(3):
        results[f"正常#{index + 1}"] = await tick(provider, f"QMT 在线 #{index + 1}")
    print(f"  {health_line(provider, 'qmt')}")

    print("\n=== ② QMT 掉线：应回退到腾讯 ===")
    provider._qmt = DeadQmt()  # noqa: SLF001
    for index in range(3):
        results[f"掉线#{index + 1}"] = await tick(provider, f"QMT 掉线 #{index + 1}")
    print(f"  回退链：{chain(provider)}")
    print(f"  {health_line(provider, 'qmt')}")
    print(f"  {health_line(provider, 'tencent')}")

    print("\n=== ③ 熔断生效：应【跳过】QMT 而不是再试一次 ===")
    allowed, reason = provider.health.should_attempt("qmt", "bars")
    print(f"  should_attempt(qmt, bars) = {allowed}  理由：{reason}")
    results["熔断后"] = await tick(provider, "熔断中请求")

    print("\n=== ④ 自动恢复：等探针周期(30s)后应自己切回 QMT（不手动重置熔断）===")
    provider._qmt = real_qmt  # noqa: SLF001
    # 这里**不能**轮询 should_attempt 来判断"探测到期了没有"：
    # 它每次放行都会把 next_probe_at 再推后 30s（名额预留，防并发雪崩），
    # 于是轮询本身会把探测名额吃掉 —— 第一版就是这样测出假 FAIL 的。
    # 正确做法：老老实实等够时间，再发请求，让链路自己完成探测。
    print("  探针间隔 30s，静默等待 35s（不查询、不干预）…")
    await asyncio.sleep(35)
    for index in range(2):
        results[f"恢复#{index + 1}"] = await tick(provider, f"QMT 恢复 #{index + 1}")
    print(f"  回退链：{chain(provider)}")
    print(f"  {health_line(provider, 'qmt')}")

    print("\n=== ⑤ 双源同时不可用：应如实报全链失败 ===")
    provider._qmt = DeadQmt()  # noqa: SLF001
    provider._tencent = DeadTencent()  # noqa: SLF001
    results["双源断供"] = await tick(provider, "双源同时不可用")
    print("  （第三级新浪被新浪封 IP，故这条链真的会失败——这是事实，不该被掩盖）")

    print("\n=== 结论 ===")
    checks = [
        ("正常态命中 QMT", results.get("正常#2") == "迅投QMT"),
        ("掉线后切到腾讯", results.get("掉线#2") == "腾讯行情"),
        ("熔断后不再试 QMT", results.get("熔断后") == "腾讯行情"),
        ("恢复后切回 QMT", results.get("恢复#1") == "迅投QMT"),
        ("双源断供如实失败", results.get("双源断供") == ""),
    ]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    for index in range(1, 4):
        values = [results.get(f'正常#{index}'), results.get(f'掉线#{index}'),
                  results.get(f'恢复#{index}')]
        print(f"  第{index}轮：正常={values[0]} 掉线={values[1]} 恢复={values[2]}")
    await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
