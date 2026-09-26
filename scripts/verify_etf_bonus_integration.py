"""验收（端到端）：真实打分链路里 ETF 加分是否落到板块上。

`verify_etf_breakout_case.py` 只验到 `EtfSignal` / `breakout_bonus` 这一层。
本脚本跑一次**完整的 `MainlineService.score_date`**（不落库），确认三件事：

  1. `etf_signals` 的键（板块代码）真的对上了 `BoardScore.code`；
  2. `board.etf_bonus` / `board.etf_level` 在真实链路上非零；
  3. `total = base_total + etf_bonus + gate_bonus` 这条合成式成立。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_etf_bonus_integration.py
    .venv\\Scripts\\python.exe scripts\\verify_etf_bonus_integration.py --date 20260706
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings  # noqa: E402
from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402
from src.mainline.service import MainlineService  # noqa: E402
from src.mainline.storage import build_mainline_repository  # noqa: E402

DATES = ("20260701", "20260706", "20260707")


async def run_one(day: str) -> int:
    config = load_config()
    store = MainlineDataStore(config=config)
    repo = build_mainline_repository(get_settings())
    if repo is None:
        print("❌ 存储不可用")
        return 2
    service = MainlineService(config=config, store=store, repo=repo)
    snapshot = await service.score_date(day, save=False)

    scored = list(snapshot.scores)
    with_bonus = [b for b in scored if b.etf_bonus > 0]
    alerted = {a.board_code: a for a in snapshot.alerts}
    print(f"\n=== {day} ===")
    print(f"板块 {len(scored)} 个 / 候选 {snapshot.candidate_count} / "
          f"精选 {snapshot.selected_count} / 告警 {len(snapshot.alerts)}")
    print(f"拿到 ETF 加分的板块：{len(with_bonus)} 个"
          f"（其中告警 {sum(1 for b in with_bonus if b.code in alerted)} 个，"
          f"提名 {sum(1 for b in with_bonus if b.promoted)} 个）")
    for board in sorted(with_bonus, key=lambda b: -b.total):
        alert = alerted.get(board.code)
        flag = (f"告警:{alert.level.label}" if alert else "未告警")
        print(f"   {board.code} {board.name:<12} 级别 {board.etf_level} "
              f"加分 {board.etf_bonus:>4.1f}  "
              f"总分 {board.total:>6.2f} = 基准 {board.base_total:>6.2f}"
              f" + ETF {board.etf_bonus:.1f} + 门控 {board.gate_bonus:.1f}"
              f"  [{flag}]"
              f"{'  ★提名' if board.promoted else ''}"
              f"{'  候选' if board.candidate else ''}")
        for reason in board.reasons[:2]:
            if "ETF" in reason:
                print(f"        · {reason}")

    # 合成式必须成立（允许四舍五入口径一致）
    bad = [b.code for b in scored
           if abs(b.total - min(100.0, b.base_total + b.etf_bonus
                                + b.gate_bonus)) > 0.01]
    if bad:
        print(f"❌ {len(bad)} 个板块的 total ≠ base + etf + gate：{bad[:5]}")
        return 1
    print("✅ total = clamp(base_total + etf_bonus + gate_bonus) 全部成立")
    # 提名通道的容量：两条通道各自独立限流（见 config.alert.nominate_etf_limit）
    promoted = [b for b in scored if b.promoted]
    # ⚠️ 不能按"reasons 里含 ETF"去数 ETF 通道：共振提名的板块也可能同时
    # 带着 ETF 加分理由（加分是对全体板块算的），那样会重复计数。
    # 提名的**通道**要看 `_nominate` 追加的那条理由（它以"提名："开头）。
    etf_named = [b for b in promoted
                 if any("ETF 资金异动提名" in r for r in b.reasons)]
    print(f"   提名 {len(promoted)} 个"
          f"（共振上限 {config.alert.nominate_limit} + "
          f"ETF 上限 {config.alert.nominate_etf_limit}），"
          f"其中 ETF 通道 {len(etf_named)} 个")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="验收 ETF 加分的端到端落地")
    parser.add_argument("--date", default="",
                        help="只跑一天（默认跑 20260701/06/07）")
    args = parser.parse_args()
    days = (args.date,) if args.date else DATES
    worst = 0
    for day in days:
        worst = max(worst, await run_one(day))
    return worst


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
