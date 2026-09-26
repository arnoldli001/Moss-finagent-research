"""真实数据验证：第六轮两条修复（全文落库 + 规则层方向）。

跑的是**本机 dev 环境 + 真实数据源**（不是 mock），验证四件事：

  1. 一次 `build_feed()` 之后 `data/intel/item_bodies.jsonl` 真的存在且有行；
  2. `GET /api/v1/intel/item/{hash}`（TestClient，不走 pilot 的登录门槛）
     对它里面的一个真指纹返回**全文**；
  3. "增加一倍 / 签订订单" 这类真实笔记拿到方向标记，且至少 3 条真实条目
     被**正确地不标**（过度标注可见）；
  4. 请求路径上两件新事的**成本**（全文落库 / 规则层倾向）+ 幂等性
     （第二次调用不再写文件）。

用法：
    python scripts/_verify_intel_round6.py

⚠️ 它会**真的写** `data/intel/item_bodies.jsonl`（不写临时目录）——
那正是要验证的东西。除此之外不改任何数据：
知识星球游标会被 `save_watermark` 推进（与正常打开一次情报流等价）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from src.api.routes import intel as INTEL_ROUTE                # noqa: E402
from src.domain.intel import body_store                        # noqa: E402
from src.domain.intel import service as S                      # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, text: str, extra: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {text}")
    if extra:
        print(f"       {extra}")
    (PASSED if ok else FAILED).append(text)


async def _run_feed(limit: int = 60) -> tuple[object, float]:
    t0 = time.monotonic()
    feed = await S.build_feed(limit=limit, group_undetermined=False)
    return feed, (time.monotonic() - t0) * 1000.0


def main() -> int:
    # 让 `build_feed` 的成本日志打出来（它按 DEBUG 级开关）。
    # ⚠️ 只放行本项目自己的 logger：`urllib3` / `httpcore` 在 DEBUG 级会把
    # 每一次 HTTP 连接都打出来，几万行噪声会把真正要看的那两行淹掉。
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("src.domain.intel.service").setLevel(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    store = body_store.store_path()
    print(f"全文存储：{store}")
    before = store.stat().st_size if store.exists() else 0
    print(f"跑之前：{'不存在' if not store.exists() else f'{before} 字节'}")

    # ── 1) 真实一次 build_feed（冷路径：有可能是第一次落库）──
    print("\n=== 1) build_feed（真实数据源）===")
    feed, ms = asyncio.run(_run_feed())
    print(f"  返回 {len(feed.items)} 条，耗时 {ms:.0f}ms，"
          f"counts={feed.counts}")
    check(bool(feed.items), "build_feed 拿到了真实条目")
    check(store.exists(), f"item_bodies.jsonl 存在（{store}）")
    if not store.exists():
        print("\n存储不存在，后续检查无从谈起")
        return 1
    lines = [ln for ln in store.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = [json.loads(ln) for ln in lines]
    after = store.stat().st_size
    check(len(rows) > 0, f"存储里有 {len(rows)} 行（{after} 字节，"
                         f"跑之前 {before} 字节）")

    # ── 2) 幂等：**同一批**再存一次，不许再写行 ──
    #
    # ⚠️ 不能用"再跑一次 build_feed"来验幂等：两次调用会各自**重新联网取数**，
    # 新出现的快讯本来就是新的（content_hash 不同），文件当然会长。
    # 那测的是"上游又发了新闻"，不是"短路有没有生效"。
    # 幂等的判据必须是**同一批输入**：把刚才那一批原样再 persist 一次。
    print("\n=== 2) 幂等性（同一批 item 再存一次）===")
    again = body_store.persist(feed.items)
    lines2 = [ln for ln in store.read_text(encoding="utf-8").splitlines()
              if ln.strip()]
    check(again["written"] == 0,
          f"同一批再存一次**写入 0 行**（written={again['written']}）",
          f"文件行数 {len(lines)} → {len(lines2)}")
    check(len(lines2) == len(lines), f"文件没有变大（{len(lines2)} 行）")

    # 单独量一次"全是已存过的"那趟落库成本（这是请求路径上的常见情形）
    t_dup = time.monotonic()
    body_store.persist(feed.items)
    ms_dup = (time.monotonic() - t_dup) * 1000.0

    # ── 3) 端到端：TestClient 打 /item/{hash} ──
    print("\n=== 3) GET /api/v1/intel/item/{hash}（TestClient，非 mock）===")

    async def _fake_require_feature(request: object, feature: str):
        return ("verify-bot", "pro")

    INTEL_ROUTE.require_feature = _fake_require_feature      # type: ignore[assignment]
    app = FastAPI()
    app.include_router(INTEL_ROUTE.router)
    client = TestClient(app)

    hashes = [it.get("content_hash") for it in feed.items
              if it.get("content_hash")]
    check(bool(hashes), f"本页有 {len(hashes)} 个可点开的指纹")
    hit_hash = ""
    for h in hashes:
        r = client.get(f"/api/v1/intel/item/{h}")
        if r.status_code == 200 and (r.json().get("text") or "").strip():
            hit_hash = h
            body = r.json()
            break
    check(bool(hit_hash), "至少一个指纹能取到全文",
          f"命中 {hit_hash}")
    if hit_hash:
        print(f"       键：{sorted(body)}")
        print(f"       标题：{body['title'][:60]}")
        print(f"       全文 {len(body['text'])} 字，前 80 字：{body['text'][:80]}")
        check(set(body) == {"content_hash", "title", "text", "published_at",
                            "kind_label", "tone"},
              "响应键仍是最小六键（无渠道身份）")
        check("source_alias" not in json.dumps(body, ensure_ascii=False),
              "响应里没有 source_alias")
    # 过期/不存在的成因要能分开（D 项）
    r404 = client.get("/api/v1/intel/item/不存在的指纹" + "x" * 40)
    r404b = client.get("/api/v1/intel/item/" + "z" * 200)
    check(r404.status_code == 404 and
          r404.json()["detail"]["code"] == "item_not_retained",
          "没留存 → item_not_retained",
          json.dumps(r404.json(), ensure_ascii=False)[:120])
    check(r404b.status_code == 404 and
          r404b.json()["detail"]["code"] == "item_bad_hash",
          "垃圾编号 → item_bad_hash",
          json.dumps(r404b.json(), ensure_ascii=False)[:120])

    # ── 4) 方向：加了规则层之后，真实数据的标注情况 ──
    print("\n=== 4) 真实数据的方向判定（规则层兜底）===")
    tagged = [it for it in feed.items
              if (it.get("tone") or {}).get("has_tone")
              and (it.get("tone") or {}).get("tone") in ("偏多", "偏空")]
    rule_tagged = [it for it in tagged
                   if (it.get("tone") or {}).get("source") == "rules"]
    print(f"  本页 {len(feed.items)} 条：有方向 {len(tagged)} 条"
          f"（其中规则层兜底 {len(rule_tagged)} 条）")
    for it in tagged[:8]:
        t = it["tone"]
        print(f"    [{t['tone']}·{t.get('source')}] "
              f"{str(it.get('title') or '')[:40]} | "
              f"依据={t.get('phrases')[:3]}")

    keywords = ("增加", "签订", "订单", "中标", "增长", "放量", "涨价", "上调",
                "超预期", "突破", "创新高", "扩产", "扭亏")
    target = [it for it in tagged
              if any(k in (str(it.get("title") or "") +
                           str(it.get("summary") or "") +
                           str(it.get("extract_text") or ""))
                     for k in keywords)]
    check(bool(target),
          "含「增加/签订/订单」类关键词的真实条目拿到了方向标记",
          f"命中 {len(target)} 条")
    for it in target[:5]:
        t = it["tone"]
        print(f"    ★ [{t['tone']}·{t.get('source')}] "
              f"{str(it.get('title') or '')[:50]}")
        print(f"       依据词：{t.get('phrases')}")
        print(f"       说明  ：{t.get('explain')}")

    # ── 5) 正确**不**标注的真实条目（过度标注可见）──
    print("\n=== 5) 真实条目里**没有**被标方向的（防过度标注）===")
    untagged = [it for it in feed.items
                if not (it.get("tone") or {}).get("has_tone")]
    print(f"  没有方向标记的：{len(untagged)} 条 / 共 {len(feed.items)} 条")
    # 说明：有 `tone` 字段但 `has_tone=False`（判定为中性）与**根本没有
    # `tone` 字段**（规则层没给方向）在读取侧都表现为"不显示标记"，
    # 但成因不同 —— 下面分开数，排障时能看出是哪一种占多数。
    neutral = sum(1 for it in untagged if it.get("tone"))
    print(f"    其中「判定为中性」{neutral} 条，"
          f"「规则层没给出方向」{len(untagged) - neutral} 条")
    for it in untagged[:6]:
        text = str((it.get("summary_text") or {}).get("text")
                   or it.get("summary") or "")[:70]
        print(f"    · {str(it.get('title') or '')[:34]} | {text}")
    check(len(untagged) >= 3, f"至少 3 条真实条目没有被标方向（{len(untagged)} 条）")

    # ── 6) 展示摘要：单行 + ≤200 字 + 摘录标记 ──
    print("\n=== 6) 展示摘要（单行 / 200 字上限 / 摘录标记）===")
    shown = 0
    for it in feed.items:
        st = it.get("summary_text") or {}
        if not st.get("text"):
            continue
        bad_nl = "\n" in st["text"] or "\r" in st["text"]
        bad_ws = "  " in st["text"] or "\u3000" in st["text"]
        over = len(st["text"]) > S.DISPLAY_SUMMARY_MAX_CHARS
        if bad_nl or bad_ws or over:
            check(False, f"摘要不合格：{st}")
        shown += 1
        if shown <= 5:
            print(f"    [{st.get('kind')}{'/摘录' if st.get('truncated') else ''}]"
                  f" {len(st['text'])} 字：{st['text'][:100]}")
    check(shown > 0, f"有 {shown} 条带展示摘要")
    check(all("\n" not in (it.get("summary_text") or {}).get("text", "")
              for it in feed.items), "没有任何摘要在换行")

    # 模型摘要 vs 原文摘录的分布（决定"用户看到的是摘要还是摘录"）
    kinds: dict[str, int] = {}
    for it in feed.items:
        k = (it.get("summary_text") or {}).get("kind") or "(空)"
        kinds[k] = kinds.get(k, 0) + 1
    print(f"  kind 分布：{kinds}")
    print(f"  tone_store 覆盖：有模型判定的 "
          f"{sum(1 for it in feed.items if (it.get('tone') or {}).get('source') != 'rules' and (it.get('tone') or {}).get('has_tone'))} 条")

    # ── 7) 成本 ──
    print("\n=== 7) 请求路径成本 ===")
    print(f"  build_feed（含六源真实聚合）：{ms:.0f}ms")
    print(f"  同一批**已存过**再落库一次：{ms_dup:.2f}ms（这就是常见情形）")
    print("  分项成本见上面 `build_feed 成本：…` 那行 DEBUG 日志")

    print(f"\n=== 结果：{len(PASSED)} 项通过，{len(FAILED)} 项失败 ===")
    for f in FAILED:
        print(f"  FAIL {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
