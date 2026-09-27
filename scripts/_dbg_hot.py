"""看热度聚合的**原始模型输出**，判断是模型不行还是校验过严。"""
import asyncio
import logging
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.disable(logging.WARNING)

from src.core.config import get_settings  # noqa: E402
from src.domain.intel.hot_topics import (  # noqa: E402
    OUTPUT_SCHEMA, SYSTEM_PROMPT, build_prompt, parse_output,
)
from src.domain.intel.service import build_feed  # noqa: E402
from src.infrastructure.llm import LLMGateway  # noqa: E402


async def main() -> int:
    feed = await build_feed(limit=500, group_undetermined=False)
    pool = [x for x in feed.items
            if x["kind"] in ("research_note", "broker_report")]
    pool = pool[:40]
    src = "\n".join(f"{c.get('title') or ''}\n{c.get('summary') or ''}"
                    for c in pool)
    gw = LLMGateway(settings=get_settings())
    # ★ 探针不花钱：`light` 层的 fallback 是**付费的** deepseek-flash，
    #   不钉住的话"本地输出不对→想看看原始输出"反而会去调云端（而这脚本
    #   存在的意义正是**排查本地模型**，云端答案对它没有价值）。
    resp = await gw.complete("light", SYSTEM_PROMPT, build_prompt(pool),
                             agent_id="dbg", json_mode=True,
                             json_schema=OUTPUT_SCHEMA, max_tokens=1200,
                             use_cache=False, local_only=True)
    raw = str(getattr(resp, "content", "") or "")
    print("=== 模型:", getattr(resp, "model_used", "?"),
          "| 输出长度:", len(raw),
          "| tokens_out:", getattr(resp, "tokens_out", 0))
    print(raw[:1600])
    print()
    stocks, topics, st = parse_output(raw, src)
    print("=== 校验后: stocks=%d topics=%d rejected=%s"
          % (len(stocks), len(topics), st))
    for s in stocks[:6]:
        print("   ", s.name, s.sector, s.sentiment)
    # 材料里有没有股票名可用
    print()
    print("=== 材料样例（看有没有可抽的股票名）===")
    for c in pool[:3]:
        print("   ", str(c.get("title"))[:44])
        print("      ", str(c.get("summary"))[:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
