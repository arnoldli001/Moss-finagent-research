"""列出 akshare 里可能的"舆情/热榜/股吧/社区"接口名（离线，不发请求）。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import akshare as ak  # noqa: E402

names = [n for n in dir(ak) if not n.startswith("_")]
KEYS = ("guba", "xueqiu", "jiuyan", "hot", "rank", "sentiment", "news",
        "info_global", "tgb", "comment", "attention", "tfp", "emotion")
for kw in KEYS:
    hit = [n for n in names if kw in n.lower()]
    if hit:
        print(f"--- {kw} ({len(hit)}) ---")
        for n in hit:
            print("   ", n)
