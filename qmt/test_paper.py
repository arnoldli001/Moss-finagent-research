# -*- coding: utf-8 -*-
"""纸面交易模式测试 —— 不连 QMT、不连柜台，用可控行情验证结算判定。

重点验证**回测看不见的那部分**：限价单到底成没成交、成交在什么价、滑点多大。
"""
from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auction_v3_qmt as V                                   # noqa: E402
import paper_trader as P                                     # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="paper_test_"))
V.TRADE_LOG = _TMP / "trades.csv"
V.RUN_LOG = _TMP / "run.log"
P.PAPER_LEDGER = _TMP / "paper_fills.csv"

DAY = datetime.now().strftime("%Y%m%d")
fails: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        fails.append(f"{label}: 得到 {got!r}，期望 {want!r}")


class StubData:
    """可控行情：open / lastPrice 按代码分别设定。"""

    def __init__(self, quotes: dict[str, dict]) -> None:
        self.q = quotes

    def full_tick(self, codes):
        return {c: dict(self.q[c]) for c in codes if c in self.q}

    def trading_dates(self, start, end):
        return [DAY]


def fresh_account(name: str, cash: float = 1_000_000.0) -> P.PaperAccount:
    a = P.PaperAccount(path=_TMP / f"{name}.json")
    a.cash = cash
    return a


# ==============================================================================
# 一、买单：9:30 开盘价 ≤ 限价 → 成交；> 限价 → 未成交
# ==============================================================================
# 决策时 9:25 价 10.30，限价 = 10.30×1.01 = 10.403
acc = fresh_account("a1")
d1 = StubData({"600000.SH": {"lastPrice": 10.30, "open": 10.35, "lastClose": 10.0}})
br = P.PaperBroker(acc, d1)
oid = br._order("600000.SH", br.c.STOCK_BUY, 10000, 10.403, "测试买入")
check("登记：产生 1 笔待成交", len(acc.pending), 1)
check("登记：决策价取到 9:25 价", acc.pending[0]["decision_price"], 10.30)
check("登记：未扣现金", acc.cash, 1_000_000.0)

r = P.settle(account=acc, data=d1, day=DAY)
check("买入：开盘 10.35 ≤ 限价 → 成交", r["filled"], 1)
check("买入：成交价 = 真实开盘价", r["rows"][0]["fill_price"], 10.35)
check("买入：持仓已建立", acc.positions["600000"]["shares"], 10000)
check("买入：成本 = 成交价", acc.positions["600000"]["cost"], 10.35)
# 现金 = 1,000,000 − 103,500 − 佣金(10.35)
expect_cash = 1_000_000.0 - 103_500.0 - V.commission(103_500.0)
check("买入：现金扣减正确", round(acc.cash, 2), round(expect_cash, 2))
slip = r["rows"][0]["slip_bps"]
check("买入：滑点 = (10.35/10.30−1)×10000 ≈ 48.5bp",
      abs(slip - (10.35 / 10.30 - 1) * 10_000) < 0.02, True)
print(f"① 买单成交：决策 10.30 → 限价 10.403 → 开盘 10.35，滑点 {slip:+.1f}bp")

# 开盘跳空高于限价 → 未成交（回测里看不见的风险）
acc2 = fresh_account("a2")
d2 = StubData({"600000.SH": {"lastPrice": 10.30, "open": 10.60, "lastClose": 10.0}})
br2 = P.PaperBroker(acc2, d2)
br2._order("600000.SH", br2.c.STOCK_BUY, 10000, 10.403, "追不上")
r2 = P.settle(account=acc2, data=d2, day=DAY)
check("买入：开盘 10.60 > 限价 → 未成交", r2["filled"], 0)
check("买入：未成交率被记录", r2["unfilled"], 1)
check("买入：未成交不建仓", acc2.positions, {})
check("买入：未成交不动现金", acc2.cash, 1_000_000.0)
print(f"② 买单未成交：开盘 10.60 高于限价 10.403 → {r2['rows'][0]['note']}")

# ==============================================================================
# 二、卖单：收盘价 ≥ 限价 → 成交；< 限价 → 未成交
# ==============================================================================
acc3 = fresh_account("a3")
acc3.positions["600000"] = {"shares": 10000, "cost": 10.0, "buy_day": DAY,
                            "name": "A"}
d3 = StubData({"600000.SH": {"lastPrice": 12.00, "open": 11.5, "lastClose": 11.0}})
br3 = P.PaperBroker(acc3, d3)
br3._order("600000.SH", br3.c.STOCK_SELL, 10000, 11.88, "止盈")
r3 = P.settle(account=acc3, data=d3, day=DAY)
check("卖出：收盘 12.00 ≥ 限价 11.88 → 成交", r3["filled"], 1)
check("卖出：成交价 = 收盘价（不是开盘价）", r3["rows"][0]["fill_price"], 12.00)
check("卖出：持仓已清空", "600000" not in acc3.positions, True)
expect_cash3 = 1_000_000.0 + 120_000.0 - V.commission(120_000.0) \
    - 120_000.0 * V.STAMP_TAX_RATE
check("卖出：现金含印花税扣减正确", round(acc3.cash, 2), round(expect_cash3, 2))
print(f"③ 卖单成交：收盘 12.00 ≥ 限价 11.88，成交价用收盘价，"
      f"印花税已扣 {120_000 * V.STAMP_TAX_RATE:.2f} 元")

# 收盘砸穿限价 → 未成交
acc4 = fresh_account("a4")
acc4.positions["600000"] = {"shares": 10000, "cost": 10.0, "buy_day": DAY,
                            "name": "A"}
d4 = StubData({"600000.SH": {"lastPrice": 9.50, "open": 11.0, "lastClose": 11.0}})
br4 = P.PaperBroker(acc4, d4)
br4._order("600000.SH", br4.c.STOCK_SELL, 10000, 11.88, "砸穿")
r4 = P.settle(account=acc4, data=d4, day=DAY)
check("卖出：收盘 9.50 < 限价 → 未成交", r4["filled"], 0)
check("卖出：未成交仍持仓", acc4.positions["600000"]["shares"], 10000)
print(f"④ 卖单未成交：收盘 9.50 低于限价 11.88 → {r4['rows'][0]['note']}")

# ==============================================================================
# 三、与真实决策链路联通：execute_buy / execute_sell 一行不改地被复用
# ==============================================================================
acc5 = fresh_account("a5")
d5 = StubData({"600000.SH": {"lastPrice": 10.20, "open": 10.25, "lastClose": 10.0}})
br5 = P.PaperBroker(acc5, d5)
plan = {
    "schema": "auction_v3.signal/1", "plan_id": "PAPER-1", "trade_date": DAY,
    "phase": "buy",
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "expire_at": datetime.now().replace(hour=23, minute=59).isoformat(
        timespec="seconds"),
    "source": {"host": "t", "rules_hash": "x", "mode": "full", "missing_dims": []},
    "market": {"stage": "发酵期", "max_streak": 5, "allowed": True},
    "guards": {"max_total_amount": 500_000.0, "max_orders": 5,
               "min_cash_reserve": 0.0},
    "positions": [{"code": "600000.SH", "name": "A", "action": "BUY",
                   "weight": 0.30, "add_ratio": 0.30, "max_weight": 0.35,
                   "ref_price": 10.20, "slip_pct": 0.01, "score": 70,
                   "reason": "r", "tags": []}],
}
import qmt_executor as X                                     # noqa: E402
res = X.execute_buy(broker=br5, data=d5, st=V.State(path=_TMP / "s.json"),
                    plan=plan, day=DAY, dry=False)
check("联通：execute_buy 在纸面 broker 上登记意向", len(acc5.pending), 1)
check("联通：未产生真实委托", len(res["orders"]), 1)
r5 = P.settle(account=acc5, data=d5, day=DAY)
check("联通：意向可结算", r5["filled"], 1)
print("⑤ 决策链路联通：qmt_executor.execute_buy 零改动跑在 PaperBroker 上")

# ==============================================================================
# 四、流水与报告
# ==============================================================================
with P.PAPER_LEDGER.open(encoding="utf-8", newline="") as f:
    rows = list(csv.DictReader(f))
check("流水：已落盘", len(rows) >= 5, True)
check("流水：含滑点列", "slip_bps" in rows[0], True)
check("流水：含成交判定列", "filled" in rows[0], True)
try:
    rc = P.report(acc5, DAY)
    check("报告：可正常输出", rc, 0)
except Exception as e:                                       # noqa: BLE001
    fails.append(f"报告崩溃：{type(e).__name__} {e}")
print(f"⑥ 流水与报告：{len(rows)} 条记录已落盘，report() 正常")

# ==============================================================================
print("\n" + "=" * 70)
if fails:
    print(f"❌ 失败 {len(fails)} 处：")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("✅ 纸面模式测试全部通过（成交判定/滑点/资金/未成交/联通/流水）")
print("=" * 70)
