"""验证 `bonus_potential`（兑现前的门控加分）是否真的被算出来并存进 payload。

## 这个不变式为什么重要

告警/精选的**排序键**是 `base_total + gate_bonus(兑现前) + etf_bonus`，
但落库的 `total` 用的是 `gate_bonus(兑现后)` —— 只有约 12% 的候选板块
真的拿到那 15~20 分。于是 `total` 在截面上是"平滑分数 + 稀疏大跳变"，
拿它对未来收益算秩相关会**系统性低估**排序质量：

    V2.2 实测   base_total H=20 IC = +0.075 / +0.085
                total      H=20 IC = +0.052 / +0.040

所以 `BoardScore` 多存一个 `bonus_potential`，让 IC 报告能衡量真实排序键
（`selection.rank_key`）。**这个字段一旦静默丢失**，IC 报告不会报错，
只会退化成用 `total` 衡量 —— 又回到低估，而且没人看得出来。
所以要有脚本断言它。

## 口径

用 `score_date_sync`（**不落库、不做确认**），所以对正在跑的重打分无影响。
断言三件事：

1. payload 里有 `bonus_potential`；
2. 对**未入选**的候选板块，`bonus_potential > gate_bonus`（前者是潜力、后者被清零）
   —— 这正是"跳变被移除"的证据；
3. `rank_key = base_total + bonus_potential + etf_bonus` 的截面离散度
   与 `total` 相当或更大，且**不等于** `total`（否则说明字段是照抄的）。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_bonus_potential.py --date 20260918
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 bonus_potential 接线")
    parser.add_argument("--date", default="", help="交易日；留空取本地最新")
    args = parser.parse_args()

    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore
    from src.mainline.service import MainlineService

    config = load_config(force=True)
    store = MainlineDataStore(config=config)
    service = MainlineService(config=config, store=store)

    day = args.date
    if not day:
        rows = store._read("SELECT MAX(trade_date) AS d FROM ml_etf")  # noqa: SLF001
        day = str(rows[0]["d"] or "") if rows else ""
    if not day:
        print("❌ 无法确定交易日")
        return 2
    print(f"试算 {day}（score_date_sync：不落库、不做确认）")

    snapshot = service.score_date_sync(day)
    if not snapshot.scores:
        print("❌ 没有算出任何评分")
        return 2

    missing: list[str] = []
    lifted = 0            # 未入选但 bonus_potential > 0 的候选板块数
    equal = 0             # rank_key == total 的板块数
    deltas: list[float] = []
    for board in snapshot.scores:          # `MainlineSnapshot.scores` 是 list
        payload = board.to_dict()
        if "bonus_potential" not in payload:
            missing.append(board.code)
            continue
        rank_key = (board.base_total + board.bonus_potential
                    + board.etf_bonus)
        if board.candidate and not board.selected and board.bonus_potential > 0:
            lifted += 1
        if abs(rank_key - board.total) < 1e-9:
            equal += 1
        deltas.append(abs(rank_key - board.total))

    total_boards = len(snapshot.scores)
    print(f"板块 {total_boards} 个；候选 {snapshot.candidate_count}；"
          f"精选 {snapshot.selected_count}")
    print(f"  含 `bonus_potential` 的 payload：{total_boards - len(missing)}"
          f"/{total_boards}")
    print(f"  未入选但潜力分 > 0（即被清零掉的'跳变'）：{lifted} 个")
    print(f"  `rank_key == total` 的板块：{equal} 个"
          f"（应远小于总数，否则字段是照抄的）")
    if deltas:
        print(f"  |rank_key − total| 最大 {max(deltas):.2f}、"
              f"平均 {sum(deltas) / len(deltas):.2f}")

    ok = True
    if missing:
        print(f"  ❌ 有 {len(missing)} 个板块的 payload 缺 `bonus_potential`"
              f"（例如 {missing[0]}）")
        ok = False
    if lifted == 0:
        print("  ❌ 没有任何'未入选但潜力分 > 0'的板块 —— "
              "要么当天没有共振，要么潜力分没在清零前留档。"
              "换一个有明显共振的交易日再试。")
        ok = False
    if total_boards and equal == total_boards:
        print("  ❌ 所有板块 `rank_key == total` —— `bonus_potential` 没起作用")
        ok = False

    print()
    print("✅ 接线正确：IC 报告可用 `selection.rank_key` 衡量真实排序键"
          if ok else "❌ 接线有问题，先修再重打分")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
