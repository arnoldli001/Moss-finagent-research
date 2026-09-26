"""诊断情报流：各类型条数、时间跨度、以及"刷屏"的是哪一类。"""
import asyncio
import sys
from collections import Counter

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.domain.intel.service import build_feed  # noqa: E402
from src.infrastructure.connectors.intel_sources import sort_key  # noqa: E402


async def main() -> int:
    feed = await build_feed(limit=500, group_undetermined=False)
    print("counts:", feed.counts)
    print("返回条数:", len(feed.items))
    by_kind = Counter(x["kind"] for x in feed.items)
    print("按类型:", dict(by_kind))

    # 去重前/后的池子时间跨度
    times = sorted(sort_key(x["published_at"]) for x in feed.items)
    print("最早:", times[0][:19] if times else "-",
          "| 最新:", times[-1][:19] if times else "-")

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).astimezone()
    for days in (1, 3, 7, 14, 30):
        cut = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        n = sum(1 for t in times if t >= cut)
        print(f"  最近 {days:>2} 天: {n}")

    print("\n=== 各类型的时间范围（看谁在回填旧数据）===")
    for kind in sorted(by_kind):
        ts = sorted(sort_key(x["published_at"])
                    for x in feed.items if x["kind"] == kind)
        print(f"  {kind:<14} n={len(ts):<4} {ts[0][:16] if ts else '-'}"
              f" → {ts[-1][:16] if ts else '-'}")

    print("\n=== 最旧的 10 条 ===")
    for x in sorted(feed.items, key=lambda y: sort_key(y["published_at"]))[:10]:
        print("   ", str(x["published_at"])[:16], x["kind"], "|",
              str(x["title"])[:46])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
