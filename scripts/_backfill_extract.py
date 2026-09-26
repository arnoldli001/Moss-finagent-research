"""补齐全量抽取（摘要 + 倾向）。分批跑，每批打印进度。"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.disable(logging.WARNING)

from src.core.config import get_settings  # noqa: E402
from src.domain.intel import tone_job, tone_store  # noqa: E402
from src.domain.intel.service import build_feed  # noqa: E402
from src.infrastructure.llm import LLMGateway  # noqa: E402


async def main() -> int:
    feed = await build_feed(limit=500, group_undetermined=False)
    gw = LLMGateway(settings=get_settings())
    total = 0
    for i in range(10):
        st = await tone_job.run_once(gateway=gw, items=feed.items,
                                    max_items=60)
        total += st["extracted"]
        print(f"批{i + 1}: 抽取={st['extracted']:3d} "
              f"已抽过={st['skipped_already_done']:3d} "
              f"低可信={st['skipped_low_credibility']:2d}", flush=True)
        if st["extracted"] == 0:
            break
    rows = list(tone_store.load().values())
    with_sum = sum(1 for r in rows if r.get("summary"))
    print(f"合计抽取 {total} | 落库 {len(rows)} | 带摘要 {with_sum}")
    tones: dict[str, int] = {}
    for r in rows:
        tones[r.get("tone") or "?"] = tones.get(r.get("tone") or "?", 0) + 1
    print("倾向分布:", tones)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
