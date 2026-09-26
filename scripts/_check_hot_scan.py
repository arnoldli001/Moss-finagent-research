"""验证 hot_scan：在真实的平台快讯上做确定性匹配，看能扫出什么。"""
import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.domain.intel import hot_job, hot_scan  # noqa: E402
from src.domain.intel.service import build_feed  # noqa: E402

# 先做几组人工断言（含已知的误报陷阱）
CASES = [
    ("财联社9月25日电，江波龙（301308）公告，拟回购股份。", ["江波龙"]),
    ("农产品价格持续上涨，带动种植板块走强。", []),          # 3字名，无代码，应拒绝
    ("农产品（000061）今日涨停。", ["农产品"]),               # 3字名 + 代码，应接受
    ("金融街附近的写字楼空置率上升。", []),                   # 3字名日常词
    ("贵州茅台(600519)发布三季报。", ["贵州茅台"]),
    ("中信证券指出，半导体设备景气度回升。", []),             # 券商名不该被当股票
    ("中信证券(600030)发布半年报，自营收入增长。", ["中信证券"]),  # 带代码就是个股新闻
    ("梅卡曼德机器人亏损收窄，机器人行业景气度回升。", []),      # 行业词，不是"机器人"这只票
    ("比亚迪股份与辽阳市政府签署战略合作协议", ["比亚迪"]),      # 3字白名单
    ("太阳能光伏装机量创新高", []),                            # 行业词
    ("今日无相关个股信息。", []),
]


def main() -> int:
    print("=== 规则断言 ===")
    bad = 0
    for text, want in CASES:
        got = hot_scan.find_names(text)
        ok = sorted(got) == sorted(want)
        if not ok:
            bad += 1
        print(f"  {'OK ' if ok else 'BAD'} {text[:34]:<36} -> {got} (期望 {want})")
    print(f"  断言失败 {bad} 条")

    feed = asyncio.run(build_feed(limit=500, group_undetermined=False))
    pool = hot_job.recent_pool([dict(x) for x in feed.items])
    print(f"\n=== 扫描 {len(pool)} 条平台快讯 ===")
    hits = hot_scan.scan_items(pool)
    print(f"命中 {len(hits)} 只 A 股\n")
    for h in hits:
        print(f"  {h.name:<8} {h.code}  提及{h.mention_count:<3} "
              f"{'/'.join(h.platforms) or '-'}")
        for e in h.evidence[:2]:
            print(f"        · {e[:58]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
