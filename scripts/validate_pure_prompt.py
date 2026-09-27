"""精简 prompt 的 A/B 验证：新 prompt 得分 vs 旧缓存得分。

## 为什么必须做这一步

`member_pure` 为了省 token 做了三处收紧：主营描述 `900 → 300` 字、
档位说明压成 1 行、输出改成位置化数组。**前两处直接减少喂给模型的判断依据**，
完全可能让"主营相关度"判得更差 —— 而这类退化不会报错，只会让一批本该校准
的分数整体漂移。

## 判据不是"分数接近"，而是**是否跨过阈值**

分数的绝对值有点噪声无所谓，真正决定成分股去留的是
`business_score >= BUSINESS_PASS(70)`。所以核心指标是
**阈值翻转数**：旧分与新分落在 70 两侧的条数。翻转越多，
说明提纯结果被 prompt 改动实质改变了。

## 对照口径

- 样本：旧缓存里**题材名命中当前 335 池**的 (股票, 题材) 对。
  只有这类才可比 —— 旧跑判过大量行业/旧体系题材，那些对当前池没有意义。
- 旧分来自 `ml_stock_theme`（当时的 prompt + `reasoning` 层）。
- 新分用当前代码的 prompt + 指定 tier 现打。

⚠️ **混淆项**：新默认 tier 是 `decision`，旧跑是 `reasoning`。分数差异同时
来自 prompt 与 tier 两个变化。要分离它们就分别跑 `--tier decision` 与
`--tier reasoning` 各一遍对比。

## 用法

    python scripts/validate_pure_prompt.py --stocks 10
    python scripts/validate_pure_prompt.py --stocks 10 --tier reasoning

## ⚠️ 成本：默认**不花钱**（2026-09-26 改）

这是**一次性验证脚本**（前端没有入口）。实测它花了 0.88 元 / 90 次调用
（`agent_id=validate_pure_prompt`，reasoning 层 = 付费 deepseek-flash）。
现在默认 `local_only`：本地模型跑，0 元。要故意用云端复现旧口径必须显式
`--allow-cloud`，并且仍然过 ≤1 元 的成本护栏（跑超即中止）。

⚠️ 注意：本地分与"旧分（当年云端 reasoning 打的）"跨模型比较，
分数差异里混了**模型**这个变量 —— 这正是 `--allow-cloud` 存在的原因。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.budget import (  # noqa: E402
    ScriptCostError,
    ScriptCostGuard,
    estimate_script_cost,
)
from src.core.config import get_settings  # noqa: E402
from src.core.errors import BRIEF_TIGHT, brief  # noqa: E402
from src.mainline.member_pure import (BUSINESS_CHARS, BUSINESS_PASS,
                                      FOCUS_HINT, RUBRIC, SYSTEM_PROMPT,
                                      USER_PROMPT, max_tokens_for,
                                      parse_scores)  # noqa: E402

CACHE_DB = "data/mainline_cache.db"


def pick_samples(conn: sqlite3.Connection, want: int
                 ) -> list[tuple[str, list[tuple[str, float]]]]:
    """挑 `want` 只股票，每只带它的可比 (题材, 旧分)。

    优先挑可比对数多的：对数越多，单只股票上的观察越充分，
    10 只股票能覆盖的"分数对"就越多。
    """
    pool = {str(r[0]) for r in conn.execute("SELECT name FROM ml_board")}
    biz = {str(r[0]) for r in conn.execute(
        "SELECT code FROM ml_company_business WHERE business <> ''")}
    by_stock: dict[str, list[tuple[str, float]]] = {}
    for row in conn.execute(
            "SELECT code, theme, business_score FROM ml_stock_theme"):
        stock, theme = str(row["code"]), str(row["theme"])
        if theme not in pool or stock not in biz:
            continue
        by_stock.setdefault(stock, []).append(
            (theme, float(row["business_score"] or 0)))
    ranked = sorted(by_stock.items(), key=lambda kv: -len(kv[1]))
    return ranked[:want]


async def run(stocks: list[tuple[str, list[tuple[str, float]]]], *,
              tier: str, concurrency: int, allow_cloud: bool = False) -> None:
    from src.infrastructure.llm import LLMGateway

    gateway = LLMGateway(settings=get_settings())
    # ★ 探针默认不花钱：`local_only=True` 把降级链裁到只剩本地模型。
    #   要云端必须显式 --allow-cloud（那才会计费，且过 ≤1 元护栏）。
    guard = ScriptCostGuard("validate_pure_prompt") if allow_cloud else None
    if guard is not None:
        calls = sum(len(v) for _, v in stocks)
        guard.check_entry(estimate_script_cost(calls))
        print(f"{guard.describe()}")
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    names = {str(r["code"]): str(r["name"] or "") for r in conn.execute(
        "SELECT code, name FROM ml_company_business")}
    biz = {str(r["code"]): str(r["business"] or "") for r in conn.execute(
        "SELECT code, business FROM ml_company_business")}
    conn.close()

    pairs: list[tuple[str, str, float, float]] = []   # 股票, 题材, 旧分, 新分
    failed = 0
    for index, (stock, cached) in enumerate(stocks, 1):
        themes = [theme for theme, _ in cached]
        text = (biz.get(stock) or "")[:BUSINESS_CHARS]
        prompt = USER_PROMPT.format(
            name=names.get(stock, stock), business=text,
            candidates="\n".join(f"{i + 1}. {t}"
                                 for i, t in enumerate(themes)),
            rubric=RUBRIC, focus=FOCUS_HINT, n=len(themes))
        try:
            resp = await gateway.complete(
                tier, SYSTEM_PROMPT, prompt, agent_id="validate_pure_prompt",
                json_mode=True, max_tokens=max_tokens_for(tier),
                local_only=not allow_cloud)
            if guard is not None:
                guard.check_running()
            fresh = parse_scores(resp.content or "", themes)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{index}/{len(stocks)}] {stock} 失败：{brief(exc, BRIEF_TIGHT)}")
            failed += 1
            continue
        if not fresh:
            print(f"  [{index}/{len(stocks)}] {stock} 解析失败（{len(themes)} 个候选）")
            failed += 1
            continue
        for theme, old in cached:
            if theme in fresh:
                pairs.append((stock, theme, old, fresh[theme]))
        print(f"  [{index}/{len(stocks)}] {stock} 完成（{len(themes)} 个候选）")

    print()
    print(f"tier={tier}  样本 {len(stocks)} 只股票，{len(pairs)} 个分数对，"
          f"失败 {failed}")
    if not pairs:
        return
    flips = [(s, t, o, n) for s, t, o, n in pairs
             if (o >= BUSINESS_PASS) != (n >= BUSINESS_PASS)]
    diffs = [abs(n - o) for _, _, o, n in pairs]
    print(f"  平均绝对差 {sum(diffs) / len(diffs):.1f} 分，"
          f"最大 {max(diffs):.0f} 分")
    print(f"  **阈值({BUSINESS_PASS})翻转 {len(flips)} / {len(pairs)} 对"
          f"（{len(flips) / len(pairs):.0%}）**")
    print()
    print(f"{'股票':<9}{'题材':<16}{'旧分':>6}{'新分':>6}{'差':>6}  翻转")
    for stock, theme, old, new in sorted(pairs, key=lambda p: -abs(p[3] - p[2])):
        flip = "← 翻转" if (old >= BUSINESS_PASS) != (new >= BUSINESS_PASS) else ""
        print(f"{stock:<9}{theme[:14]:<16}{old:>6.0f}{new:>6.0f}"
              f"{new - old:>+6.0f}  {flip}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass
    parser = argparse.ArgumentParser(description="精简 prompt 的 A/B 验证")
    parser.add_argument("--stocks", type=int, default=10)
    parser.add_argument("--tier", default="decision",
                        help="LLM 路由层（旧跑用的是 reasoning）")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--allow-cloud", action="store_true",
                        help="**显式**允许付费云端（默认只用本地模型，0 元）；"
                             "开启后仍受 ≤1 元 单次上限约束")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    samples = pick_samples(conn, max(args.stocks, 1))
    conn.close()
    if not samples:
        print("❌ 没有可比的样本（旧缓存里题材名命中当前池的对为空）")
        return 2
    print(f"选中 {len(samples)} 只股票，"
          f"可比分数对 {sum(len(v) for _, v in samples)} 个")
    print("成本模式：" + ("**云端**（--allow-cloud，会计费）"
                          if args.allow_cloud else "本地模型（0 元）") + "\n")
    try:
        asyncio.run(run(samples, tier=args.tier, concurrency=args.concurrency,
                        allow_cloud=args.allow_cloud))
    except ScriptCostError as exc:
        print(f"\n⛔ {exc}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
