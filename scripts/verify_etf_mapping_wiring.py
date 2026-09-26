"""验证：人工 ETF 映射的接线与覆盖面。

要求：`load_mapping` 必须**同时**读到自动表与人工表，人工优先、允许一对多，
且一只 ETF 不会同时算到两个无关板块上。

用法：.venv\\Scripts\\python.exe scripts\\verify_etf_mapping_wiring.py
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402
from src.mainline.etf import (  # noqa: E402
    MANUAL_MAPPING_FILE,
    board_etf_signals,
    load_mapping,
)


def main() -> int:
    cfg = load_config()
    mapping = load_mapping(cfg)
    print(f"映射表：keywords={len(mapping.keywords)} "
          f"overrides={len(mapping.overrides)} loaded={mapping.loaded} "
          f"gap={mapping.gap!r}")
    print(f"   人工表 {MANUAL_MAPPING_FILE} 已加载："
          f"{(ROOT / 'configs' / MANUAL_MAPPING_FILE).exists()}")

    store = MainlineDataStore(config=cfg)
    pool = {str(r["code"]): str(r["name"]) for r in store._read(  # noqa: SLF001
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'")}
    boards = {name for names in mapping.overrides.values() for name in names}
    in_pool = {name for name in boards if name in pool.values()}
    print()
    print("=" * 76)
    print("覆盖面")
    print("-" * 76)
    print(f"  映射到 ETF 的板块   {len(in_pool)}/{len(pool)}"
          f"（{len(in_pool) / len(pool) * 100:.1f}%）")
    print(f"  涉及 ETF           {len(mapping.overrides)} 只")
    print(f"  (板块,ETF) 关系    "
          f"{sum(len(v) for v in mapping.overrides.values())} 条")
    multi = Counter(code for code, names in mapping.overrides.items()
                    if len(names) > 1)
    print(f"  一对多的 ETF       {len(multi)} 只"
          f"（最多归 {max((len(v) for v in mapping.overrides.values()), default=0)} 个板块）")
    missing = sorted(set(pool.values()) - in_pool)
    print(f"  未覆盖板块         {len(missing)} 个"
          f"（前 8：{'、'.join(missing[:8])}）")

    # 关键回归：一只 ETF 不能同时算到**互不相关**的两个板块上。
    # 一对多是允许的，但只应发生在"同一主题族"内部；跨族共享是数据错误。
    print()
    print("=" * 76)
    print("跨主题族共享检查（同一只 ETF 归到互不相关的板块 = 数据错误）")
    print("-" * 76)
    suspect = 0
    for code, names in sorted(mapping.overrides.items(),
                              key=lambda kv: -len(kv[1])):
        if len(names) < 2:
            continue
        # 粗判据：板块名之间没有任何公共汉字 → 可能跨族
        head = set(names[0])
        disjoint = [n for n in names[1:] if not (set(n) & head)]
        if disjoint:
            suspect += 1
            print(f"  ⚠️ {code} {names[0]} ↔ {disjoint[:3]}"
                  f"（共 {len(names)} 个板块）")
    if not suspect:
        print("  ✅ 没有发现明显的跨族共享")

    # 真值里最频繁的板块必须有 ETF（这是加人工映射的主要动机）
    print()
    print("=" * 76)
    print("真值事件最频繁的板块是否有 ETF")
    print("-" * 76)
    for name in ("人形机器人", "东数西算(算力)", "CRO概念", "创新药",
                 "农业种植", "机器人概念"):
        codes = [code for code, names in mapping.overrides.items()
                 if name in names]
        code_of = next((c for c, n in pool.items() if n == name), "")
        detail = (f"✅ {len(codes)} 只：{','.join(codes[:5])}" if codes
                  else "❌ 无 ETF 映射")
        print(f"  {name:<16}{code_of:<12}{detail}")

    # 端到端：跑一天的板块级信号，确认一对多真的生效
    print()
    print("=" * 76)
    print("端到端（20260706）：按映射算一遍板块级 ETF 信号")
    print("-" * 76)
    end = "20260706"
    wanted = {code.split(".")[0] for code in mapping.overrides}
    etf_codes = [c for c in store.etf_codes() if c.split(".")[0] in wanted]
    bars = store.etf_bars(etf_codes, start="20250101", end=end)
    board_by_name = {name: code for code, name in pool.items()}
    signals = board_etf_signals(
        bars, mapping, board_by_name=board_by_name,
        window=int(cfg.etf.window),
        breakout_amount_ratio=float(cfg.etf.breakout_amount_ratio),
        breakout_percentile=float(cfg.etf.breakout_percentile))
    hit = {code: s for code, s in signals.items() if s.level_of() > 0}
    print(f"  参与计算 {len(bars)} 只 ETF → 有信号的板块 {len(hit)} 个")
    for code, signal in sorted(hit.items(),
                               key=lambda kv: -kv[1].level_of())[:12]:
        print(f"    {code} {pool.get(code, ''):<14} 级别 {signal.level_of()} "
              f"{signal.etf_count} 只 ETF  "
              f"放大 {(signal.amount_ratio or 0):.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
