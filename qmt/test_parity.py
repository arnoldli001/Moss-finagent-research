# -*- coding: utf-8 -*-
"""校验 QMT 移植版的打分维度是否与仓库 rulebook.py **逐点一致**。

这是移植正确性的唯一硬标准：同一输入必须得到同一分数。
不通过就不该上线。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "qmt"))

import auction_v3_qmt as Q                                    # noqa: E402
from src.auction_select import rulebook                        # noqa: E402

rs = rulebook.ruleset_for()
print(f"规则指纹 rules_hash = {rs.hash}\n")


def ref(dim_key: str, row: dict) -> float | None:
    """仓库口径：规则表算分。"""
    score, _ = rs.score_dim(rs.dim(dim_key), row)
    return score


fails: list[str] = []
checks = 0


def cmp(dim_key: str, row: dict, mine: float | None, label: str) -> None:
    global checks
    checks += 1
    theirs = ref(dim_key, row)
    ok = (mine is None and theirs is None) or (
        mine is not None and theirs is not None and abs(mine - theirs) < 5e-4)
    if not ok:
        fails.append(f"{dim_key:<18} {label:<28} 我={mine} 仓库={theirs}")


# ---- 竞价量能 ----
for v in (0, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 7, 10, 12, 20):
    cmp("auction_strength", {"auction_volume_vs_yesterday": v},
        Q.dim_auction_strength(v), f"今昨竞比={v}")

# ---- 开盘位置 ----
for v in (0, 1, 1.99, 2, 3, 4, 5, 6, 7, 9, 9.5, 12):
    cmp("price_position", {"open_gap_pct": v},
        Q.dim_price_position(v), f"开盘涨幅={v}")

# ---- 承接强度 ----
for v in (0, 10, 25, 50, 75, 100):
    cmp("takeover", {"takeover_score": v}, Q.dim_takeover(v), f"承接={v}")

# ---- 题材热度 ----
for v in (0, 0.3, 0.7, 1.0):
    cmp("theme_heat", {"theme_heat": v}, Q.dim_theme_heat(v), f"热度={v}")

# ---- 连板梯队 ----
for v in (0, 1, 2, 3, 4, 5, 6, 10):
    cmp("ladder", {"prev_limit_up_streak": v}, Q.dim_ladder(v), f"连板={v}")

# ---- 封流比 ----
for v in (0, 0.005, 0.01, 0.03, 0.05):
    cmp("seal_flow_ratio", {"prev_seal_to_float_ratio": v},
        Q.dim_seal_flow(v), f"封流比={v}")

# ---- 竞价情绪 ----
for v in (0, 0.4, 1.0):
    cmp("auction_sentiment", {"auction_sentiment_score": v},
        Q.dim_auction_sentiment(v), f"竞价情绪={v}")

# ---- 盘口适配（仓库读的是 auction_amount + circulating_market_value 两个原始字段）----
CAP = 5e9
for v in (0.0005, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1):
    cmp("capital_fit",
        {"auction_amount": v * CAP, "circulating_market_value": CAP},
        Q.dim_capital_fit(v), f"竞价额/市值={v}")
cmp("capital_fit", {"auction_amount": None, "circulating_market_value": CAP},
    Q.dim_capital_fit(None), "竞价额缺失")
cmp("capital_fit", {"auction_amount": 1e6, "circulating_market_value": 0},
    None, "市值为 0（应缺席）")

# ---- 换手率 ----
for v in (0, 1, 3, 4, 5, 10, 20, 25, 30, 40):
    cmp("turnover", {"turnover_rate": v}, Q.dim_turnover(v), f"换手={v}")

# ---- 昨日质量 ----
for seal in (None, 0, 0.01, 0.03, 0.05):
    for amt in (None, 0.003, 0.005, 0.02, 0.06, 0.1):
        row = {}
        if seal is not None:
            row["prev_seal_to_float_ratio"] = seal
        if amt is not None:
            row["auction_amount_ratio"] = amt
        cmp("previous_day", row, Q.dim_previous_day(seal, amt),
            f"封流={seal}/额比={amt}")

# ---- 情绪周期（温度 × 阶段）----
for temp in (0, 30, 50, 80, 100):
    for stage in ("主升期", "发酵期", "高位震荡期", "试错期", "退潮期", "冰点", ""):
        cmp("sentiment", {"market_temperature": temp, "market_stage": stage},
            Q.dim_sentiment(temp, stage), f"温度={temp}/阶段={stage}")

# ---- 题材龙头 ----
for leader in (None, True, False):
    for high in (None, 2, 3):
        for streak in (None, 1, 2, 3):
            row = {}
            if leader is not None:
                row["theme_is_leader"] = leader
            if high is not None:
                row["theme_highest_ladder"] = high
            if streak is not None:
                row["prev_limit_up_streak"] = streak
            cmp("theme_leader", row,
                Q.dim_theme_leader(leader, high, streak),
                f"龙头={leader}/最高={high}/连板={streak}")

print(f"共比对 {checks} 个点")
if fails:
    print(f"\n❌ 打分维度不一致 {len(fails)} 处：")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("✅ 打分维度：全部一致\n")

# ======================================================================
# 第二部分：市场情绪周期（对照 src/intraday/market_cycle.py）
# ======================================================================
from src.intraday import market_cycle as MC                       # noqa: E402

mc_fail: list[str] = []
mc_n = 0
for lu in (0, 10, 15, 29, 30, 45, 60, 80):
    for br in (None, 0.1, 0.29, 0.3, 0.45, 0.6, 0.61, 0.8):
        for ms in (0, 2, 3, 5, 7):
            for s2 in (0, 3, 4, 10):
                for ld in (0, 5, 10, 11):
                    for bl in (0, 9, 10):
                        mc_n += 1
                        a, _ = MC.classify_stage(limit_up_count=lu, broken_rate=br,
                                                 max_streak=ms, streak2plus=s2,
                                                 big_loss_count=bl,
                                                 limit_down_count=ld)
                        b, _ = Q.classify_stage(limit_up_count=lu, broken_rate=br,
                                                max_streak=ms, streak2plus=s2,
                                                big_loss_count=bl, limit_down_count=ld)
                        if a != b:
                            mc_fail.append(f"阶段 lu={lu} br={br} ms={ms} s2={s2} "
                                           f"ld={ld} bl={bl}: 我={b} 仓库={a}")
                        t1 = MC.temperature_from(limit_up_count=lu, broken_rate=br,
                                                 max_streak=ms, limit_down_count=ld)
                        t2 = Q.temperature_from(limit_up_count=lu, broken_rate=br,
                                                max_streak=ms, limit_down_count=ld)
                        if t1 != t2:
                            mc_fail.append(f"温度 lu={lu} br={br} ms={ms} ld={ld}: "
                                           f"我={t2} 仓库={t1}")

print(f"市场周期：比对 {mc_n} 组")
if mc_fail:
    print(f"❌ 不一致 {len(mc_fail)} 处：")
    for f in mc_fail[:12]:
        print("   " + f)
    sys.exit(1)
print("✅ 市场周期：全部一致\n")

# ======================================================================
# 第三部分：承接强度（对照 src/auction_select/features.takeover_strength）
# ======================================================================
from src.auction_select import features as FT                     # noqa: E402
import random                                                      # noqa: E402

random.seed(7)
tk_fail: list[str] = []
tk_n = 0
for trial in range(300):
    n = random.randint(0, 14)
    base = random.uniform(8.0, 30.0)
    pts, series = [], []
    for i in range(n):
        mm = 20 + i
        if mm > 25:
            break
        t = f"09:{20 + i // 3:02d}:{(i * 7) % 60:02d}"
        t = f"09:{20 + (i * 5) // 60:02d}:{(i * 5) % 60:02d}"
        p = round(base * (1 + random.uniform(-0.03, 0.03)), 2)
        d = random.choice((-1, 0, 1))
        u = round(random.uniform(0, 5e5), 0)
        pts.append(Q.AuctionPoint(t=t, price=p, volume=0.0, amount=0.0,
                                  unmatched=u, direction=d))
        series.append({"time": t, "price": p, "unmatched": u, "direction": d})
    tk_n += 1
    mine = Q.takeover_strength(pts)
    theirs = FT.takeover_strength(series)
    if (mine.get("score") is None) != (theirs.get("score") is None):
        tk_fail.append(f"trial{trial} 缺席判定不一致 我={mine.get('score')} "
                       f"仓库={theirs.get('score')}")
    elif mine.get("score") is not None:
        if abs(float(mine["score"]) - float(theirs["score"])) > 0.02:
            tk_fail.append(f"trial{trial} 我={mine['score']} 仓库={theirs['score']}")

print(f"承接强度：随机比对 {tk_n} 组序列")
if tk_fail:
    print(f"❌ 不一致 {len(tk_fail)} 处：")
    for f in tk_fail[:10]:
        print("   " + f)
    sys.exit(1)
print("✅ 承接强度：全部一致\n")

# ======================================================================
# 第四部分：卖出规则（对照回测 V3 的优先级与阈值，显式断言）
# ======================================================================
S = Q.SellAction
cases: list[tuple[dict, str | None, float | None]] = [
    # (入参, 期望原因片段, 期望卖出比例)
    (dict(held_days=0, cost=10, price=9.0, is_sealed=False, ma_exit=None,
          d_chg=-0.05, market_high=False, rush=[]), None, None),          # T+1 不可卖
    (dict(held_days=1, cost=10, price=9.4, is_sealed=False, ma_exit=9.0,
          d_chg=-0.06, market_high=False, rush=[]), "收盘亏损5%", 1.0),    # ② 止损优先
    (dict(held_days=3, cost=10, price=10.5, is_sealed=False, ma_exit=10.0,
          d_chg=-0.075, market_high=False, rush=[]), "跌幅≥7%", 1.0),      # ③
    (dict(held_days=3, cost=10, price=10.2, is_sealed=False, ma_exit=10.3,
          d_chg=-0.01, market_high=False, rush=[]), "破5日线", 1.0),       # ④
    (dict(held_days=10, cost=10, price=10.2, is_sealed=True, ma_exit=10.0,
          d_chg=0.01, market_high=False, rush=[]), "持有满10日", 1.0),     # ⑤
    (dict(held_days=4, cost=10, price=12.0, is_sealed=True, ma_exit=11.0,
          d_chg=-0.03, market_high=False, rush=[]), "卖30%", 0.30),        # ⑥
    (dict(held_days=1, cost=10, price=10.1, is_sealed=False, ma_exit=9.0,
          d_chg=0.01, market_high=False, rush=[]), "次日收盘未涨停", 0.50),  # ⑦
    (dict(held_days=1, cost=10, price=10.5, is_sealed=True, ma_exit=9.0,
          d_chg=0.05, market_high=False, rush=[]), None, None),            # 封板 → 不卖
    (dict(held_days=2, cost=10, price=11.5, is_sealed=False, ma_exit=10.0,
          d_chg=0.02, market_high=True, rush=["抢跑"]), "最高连板≥7", 1.0),  # ① 最高优先级
]
sl_fail: list[str] = []
for i, (kw, want_reason, want_ratio) in enumerate(cases, 1):
    act = Q.plan_sells(code="600000", name="测试", **kw)
    if want_reason is None:
        if act is not None:
            sl_fail.append(f"case{i} 应为「不卖」，实际={act.reason}")
        continue
    if act is None:
        sl_fail.append(f"case{i} 应为「{want_reason}」，实际=None")
        continue
    if want_reason not in act.reason:
        sl_fail.append(f"case{i} 原因不符：期望含「{want_reason}」实际「{act.reason}」")
    if want_ratio is not None and abs(act.qty_ratio - want_ratio) > 1e-9:
        sl_fail.append(f"case{i} 比例不符：期望 {want_ratio} 实际 {act.qty_ratio}")

print(f"卖出规则：断言 {len(cases)} 个用例")
if sl_fail:
    print(f"❌ 失败 {len(sl_fail)} 处：")
    for f in sl_fail:
        print("   " + f)
    sys.exit(1)
print("✅ 卖出规则：全部通过\n")

# ---- 抢筹/抢跑标签 ----
b_fail: list[str] = []
b_cases = [
    (dict(jump=1.05, volume_vs_yesterday=3.0, volume_ratio_pct=5.0,
          open_gap_pct=3.0, prev_streak=1, code="600000"), ["抢筹"], "跳空>1.01且放量"),
    (dict(jump=1.05, volume_vs_yesterday=1.5, volume_ratio_pct=5.0,
          open_gap_pct=3.0, prev_streak=1, code="600000"), [], "跳空够但今昨竞比不足"),
    (dict(jump=1.05, volume_vs_yesterday=3.0, volume_ratio_pct=16.0,
          open_gap_pct=3.0, prev_streak=1, code="600000"), ["抢筹", "大量抢筹"],
     "量比>15% → 大量抢筹"),
    (dict(jump=1.07, volume_vs_yesterday=3.0, volume_ratio_pct=8.0,
          open_gap_pct=6.5, prev_streak=1, code="600000"), ["抢筹", "大量抢筹"],
     "主板>6%且量比>7% → 大量抢筹"),
    (dict(jump=0.95, volume_vs_yesterday=5.0, volume_ratio_pct=12.0,
          open_gap_pct=3.0, prev_streak=1, code="600000"), ["抢跑"], "跳空<0.99且量比>10%"),
    (dict(jump=0.95, volume_vs_yesterday=1.5, volume_ratio_pct=12.0,
          open_gap_pct=3.0, prev_streak=2, code="600000"), [], "命中三条件豁免"),
]
for i, (kw, want, label) in enumerate(b_cases, 1):
    got, _ = Q.rush_labels(**kw)
    if sorted(got) != sorted(want):
        b_fail.append(f"case{i} {label}: 期望 {want} 实际 {got}")
print(f"抢筹/抢跑标签：断言 {len(b_cases)} 个用例")
if b_fail:
    print(f"❌ 失败 {len(b_fail)} 处：")
    for f in b_fail:
        print("   " + f)
    sys.exit(1)
print("✅ 抢筹/抢跑：全部通过\n")

# ---- 涨停价四舍五入（不得用银行家舍入）----
lim_cases = [(10.0, 11.0), (10.05, 11.06), (3.33, 3.66), (7.77, 8.55),
             (12.34, 13.57), (2.5, 2.75)]
lim_fail = [f"{pc}→我们{Q.limit_up_price(pc)} 期望{exp}"
            for pc, exp in lim_cases
            if abs(Q.limit_up_price(pc) - exp) > 1e-9]
print(f"涨停价：断言 {len(lim_cases)} 个用例")
if lim_fail:
    print("❌ " + "; ".join(lim_fail))
    sys.exit(1)
print("✅ 涨停价：全部通过\n")

print("=" * 66)
print("全部校验通过 —— QMT 移植版与仓库口径一致，可进入 dry-run 实盘验证")
print("=" * 66)
