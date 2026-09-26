"""跑一轮真实的平台热议聚合，看能不能出股票/事件（不改调度、只落 hot_topics.json）。"""
import asyncio
import json
import logging
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.disable(logging.WARNING)

from src.core.config import get_settings  # noqa: E402
from src.domain.intel import hot_job  # noqa: E402
from src.domain.intel.service import build_feed  # noqa: E402
from src.infrastructure.llm import LLMGateway  # noqa: E402


async def main() -> int:
    feed = await build_feed(limit=500, group_undetermined=False)
    pool = hot_job.recent_pool([dict(x) for x in feed.items])
    from collections import Counter
    print("池子", len(pool), Counter(x.get("kind") for x in pool))
    print("样例:")
    for x in pool[:5]:
        print("   ", str(x.get("published_at"))[:19], "|",
              str(x.get("title"))[:50])

    gw = LLMGateway(settings=get_settings())
    stats = await hot_job.run_once(gateway=gw, items=[dict(x) for x in feed.items],
                                   max_notes=80)
    print("\n=== 聚合统计 ===")
    print(json.dumps(stats, ensure_ascii=False))
    data = hot_job.load(force=True)
    print("\n=== 热门个股 ===")
    for s in (data.get("stocks") or [])[:12]:
        print(f"   {s['name']:<8} {s.get('code',''):<7} "
              f"提及{s['mention_count']} {s['sentiment']}  {s['summary'][:34]}")
    print("\n=== 热议事件 ===")
    for t in (data.get("topics") or [])[:8]:
        print(f"   [{t.get('sentiment','')}] {t['title'][:40]}")
        print(f"       {t.get('detail','')[:60]} | 关联 {t.get('related')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
