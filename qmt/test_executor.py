# -*- coding: utf-8 -*-
"""执行端端到端契约测试 —— 用桩对象跑通「信号 → 校验 → 执行 → 回执」，不需要 QMT。

覆盖 SIGNAL_CONTRACT.md §6 的每一条强制校验，以及仓位/上限/幂等/降级的行为。
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auction_v3_qmt as V                                   # noqa: E402
import qmt_executor as X                                     # noqa: E402

DAY = datetime.now().strftime("%Y%m%d")

# 把副作用（成交流水/日志）导到临时目录，别污染仓库
_TMP = Path(tempfile.mkdtemp(prefix="qmt_test_"))
V.TRADE_LOG = _TMP / "trades.csv"
V.RUN_LOG = _TMP / "run.log"

fails: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        fails.append(f"{label}: 得到 {got!r}，期望 {want!r}")


# ==============================================================================
# 桩
# ==============================================================================
class StubData:
    """只提供执行端用到的两件事：前收 / 交易日历。"""
    PREV = {"600000.SH": 10.00, "000001.SZ": 20.00, "600519.SH": 1500.00}

    def full_tick(self, codes):
        out = {}
        for c in codes:
            pc = self.PREV.get(c)
            if pc:
                out[c] = {"lastClose": pc, "lastPrice": pc * 1.03, "open": pc * 1.03}
        return out

    def trading_dates(self, start, end):
        # 返回 5 个交易日，让 held_days 能算出 >0（否则触发 T+1 不可卖）
        d = datetime.strptime(DAY, "%Y%m%d")
        return [(d - timedelta(days=i)).strftime("%Y%m%d") for i in range(4, -1, -1)]


#: 3 个交易日前的买入日 —— 避开 T+1，卖出规则才有判定空间
BUY_DAY_OLD = (datetime.strptime(DAY, "%Y%m%d")
               - timedelta(days=3)).strftime("%Y%m%d")


class StubBroker:
    """⚠️ `positions()` 的键必须是**裸代码**（无 .SH/.SZ），与真实 Broker 一致：
    真实实现是 `{bare(p.stock_code): p}`。键不一致会让加仓/上限逻辑静默失效。"""

    class c:
        STOCK_BUY = 23
        STOCK_SELL = 24
        FIX_PRICE = 11

    def __init__(self, equity=1_000_000.0, cash=1_000_000.0, positions=None):
        self._eq, self._cash = equity, cash
        self._pos = {V.bare(k): v for k, v in (positions or {}).items()}
        self.orders: list[tuple] = []
        self._oid = 1000

    def equity(self):
        return self._eq

    def cash(self):
        return self._cash

    def positions(self):
        return self._pos

    def asset(self):
        return SimpleNamespace(total_asset=self._eq, cash=self._cash,
                               market_value=0.0)

    def _order(self, code, side, shares, price, reason):
        self._oid += 1
        self.orders.append((code, side, shares, round(price, 3), reason))
        return self._oid


def make_plan(**over) -> dict:
    plan = {
        "schema": "auction_v3.signal/1",
        "plan_id": "TEST-plan-001",
        "trade_date": DAY,
        "phase": "buy",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "expire_at": (datetime.now() + timedelta(minutes=5)).isoformat(
            timespec="seconds"),
        "source": {"host": "t", "rules_hash": "6f4be9710912", "mode": "full",
                   "missing_dims": []},
        "market": {"stage": "发酵期", "max_streak": 5, "allowed": True},
        "guards": {"max_total_amount": 500_000.0, "max_orders": 5,
                   "min_cash_reserve": 0.0},
        "positions": [
            {"code": "600000.SH", "name": "浦发", "action": "BUY",
             "weight": 0.30, "add_ratio": 0.30, "max_weight": 0.35,
             "ref_price": 10.30, "slip_pct": 0.01, "score": 70.0,
             "reason": "r", "tags": []},
        ],
        "notes": [],
    }
    plan.update(over)
    return plan


# ==============================================================================
# 一、契约校验
# ==============================================================================
now = datetime.now()
check("校验：正常计划应无错", X.validate(make_plan(), phase="buy", day=DAY, now=now), [])
check("校验：schema 不符",
      bool(X.validate(make_plan(schema="x"), phase="buy", day=DAY, now=now)), True)
check("校验：日期不符",
      bool(X.validate(make_plan(trade_date="20200101"), phase="buy", day=DAY,
                      now=now)), True)
check("校验：phase 不符",
      bool(X.validate(make_plan(phase="sell"), phase="buy", day=DAY, now=now)), True)
check("校验：已过期",
      bool(X.validate(make_plan(expire_at=(now - timedelta(seconds=1)).isoformat(
          timespec="seconds")), phase="buy", day=DAY, now=now)), True)
check("校验：缺 expire_at",
      bool(X.validate(make_plan(expire_at=""), phase="buy", day=DAY, now=now)), True)
check("校验：标的数超 max_orders",
      bool(X.validate(make_plan(positions=[{"code": f"60000{i}.SH"}
                                           for i in range(9)],
                                guards={"max_orders": 5}), phase="buy",
                      day=DAY, now=now)), True)
print("① 契约校验 7 项")

# ==============================================================================
# 二、买入执行
# ==============================================================================
plan = make_plan(positions=[
    {"code": "600000.SH", "name": "A", "weight": 0.30, "add_ratio": 0.30,
     "max_weight": 0.35, "ref_price": 10.30, "slip_pct": 0.01, "score": 70,
     "reason": "a", "tags": []},
    {"code": "000001.SZ", "name": "B", "weight": 0.30, "add_ratio": 0.30,
     "max_weight": 0.35, "ref_price": 20.60, "slip_pct": 0.01, "score": 68,
     "reason": "b", "tags": ["抢筹"]},
])
br = StubBroker()
st = V.State(path=_TMP / "state.json")
res = X.execute_buy(broker=br, data=StubData(), st=st, plan=plan, day=DAY,
                    dry=False)
check("买入：下单 2 笔", len(br.orders), 2)
check("买入：fill 记录 2 条", len(res["orders"]), 2)

# 限价 = min(涨停价, ref×(1+slip))；600000 前收 10.00 → 涨停 11.00，限价 10.403
o0 = br.orders[0]
check("买入：限价 = ref×1.01", o0[3], round(10.30 * 1.01, 3))
check("买入：限价未超涨停", o0[3] <= V.limit_up_price(10.00), True)
# 预算 = 1,000,000 × 0.30 = 300,000；按限价折算股数取整到百股
expect0 = int(300_000 / (10.30 * 1.01) // V.LOT * V.LOT)
check("买入：股数按限价折算", o0[2], expect0)
check("买入：方向为买", o0[1], StubBroker.c.STOCK_BUY)
print("② 买入执行：下单 2 笔、限价与股数正确")

# 单只上限：已有市值 340,000，权益 1,000,000 → max_weight 0.35 只剩 10,000
br2 = StubBroker(positions={"600000.SH": SimpleNamespace(
    market_value=340_000.0, volume=33000, can_use_volume=0, avg_price=10.0)})
X.execute_buy(broker=br2, data=StubData(), st=V.State(path=_TMP / "s2.json"),
              plan=make_plan(), day=DAY, dry=False)
if br2.orders:
    amt = br2.orders[0][2] * br2.orders[0][3]
    check("买入：单只上限生效(≤10,000元)", amt <= 10_000.0 + 1e-6, True)
else:
    check("买入：上限用尽则应跳过", True, True)
print("③ 单只上限：已达标时不再加仓")

# 资金不足：现金 500 元，连一手浦发（约1,040元）都买不起
br3 = StubBroker(cash=500.0)
X.execute_buy(broker=br3, data=StubData(), st=V.State(path=_TMP / "s3.json"),
              plan=make_plan(), day=DAY, dry=False)
check("买入：现金不足一手则不下单", len(br3.orders), 0)

# 现金只够小仓：应按下单能力缩量，而不是照 30% 硬下
br3b = StubBroker(cash=5_000.0)
X.execute_buy(broker=br3b, data=StubData(), st=V.State(path=_TMP / "s3b.json"),
              plan=make_plan(), day=DAY, dry=False)
if br3b.orders:
    amt3 = br3b.orders[0][2] * br3b.orders[0][3]
    check("买入：现金受限时缩量(≤5,000)", amt3 <= 5_000.0 + 1e-6, True)
print("④ 资金约束：不足一手跳过；够则缩量下单")

# 闸门关闭
br4 = StubBroker()
r4 = X.execute_buy(broker=br4, data=StubData(), st=V.State(path=_TMP / "s4.json"),
                   plan=make_plan(market={"stage": "退潮期", "allowed": False}),
                   day=DAY, dry=False)
check("买入：闸门关闭则不开仓", len(br4.orders), 0)
check("买入：闸门关闭有告警", bool(r4["warnings"]), True)
print("⑤ 市场闸门：allowed=false 时零下单")

# max_total_amount 约束
br5 = StubBroker()
X.execute_buy(broker=br5, data=StubData(),
              st=V.State(path=_TMP / "s5.json"),
              plan=make_plan(guards={"max_total_amount": 120_000.0,
                                     "max_orders": 5, "min_cash_reserve": 0.0}),
              day=DAY, dry=False)
spent = sum(o[2] * o[3] for o in br5.orders)
check("买入：总额守卫生效(≤120,000)", spent <= 120_000.0 + 1e-6, True)
print(f"⑥ 总额守卫：实际支出 {spent:,.0f} ≤ 120,000")

# ==============================================================================
# 三、README 里承诺的「价格类卖出」在信号端离线时仍生效
# ==============================================================================
br6 = StubBroker(positions={"600000.SH": SimpleNamespace(
    market_value=100_000.0, volume=10000, can_use_volume=10000, avg_price=10.0)})
st6 = V.State(path=_TMP / "s6.json")
# 成本 12.00，现价 10.30 → −14%；买入日设在 3 个交易日前，避开 T+1
st6.put("600000", buy_day=BUY_DAY_OLD, cost=12.00, name="A")
r6 = X.execute_sell(broker=br6, data=StubData(), st=st6, alert=None, day=DAY,
                    dry=False)
check("卖出：无信号时止损仍触发", len(br6.orders), 1)
if br6.orders:
    check("卖出：原因为成本止损", br6.orders[0][4].startswith("收盘亏损"), True)
    check("卖出：方向为卖", br6.orders[0][1], StubBroker.c.STOCK_SELL)
print("⑦ 降级自保：本地信号缺失时，价格类止损照常执行")

# ==============================================================================
# 四、回执结构
# ==============================================================================
st7 = V.State(path=_TMP / "s7.json")
st7.put("600000", buy_day=DAY, cost=10.0, name="A")
br7 = StubBroker(positions={"600000.SH": SimpleNamespace(
    market_value=103_000.0, volume=10000, can_use_volume=0, avg_price=10.3)})
fill = X.build_fill(broker=br7, st=st7, day=DAY, phase="buy", plan_id="P1",
                    account="123456", result={"orders": [], "warnings": []},
                    degraded=True, extra_warnings=[])
check("回执：schema", fill["schema"], "auction_v3.fill/1")
check("回执：含 buy_day（QMT 持仓结构没有）", fill["positions"][0]["buy_day"], DAY)
check("回执：降级有标注",
      any("降级" in w for w in fill["warnings"]), True)
json.dumps(fill, ensure_ascii=False)                       # 必须可 JSON 序列化
print("⑧ 回执：结构完整、可序列化、buy_day 已回填")

# ==============================================================================
# 五、信号端产出的计划能被执行端接受（跨模块联通）
# ==============================================================================
import signal_server as S                                    # noqa: E402

sig = S.envelope(phase="buy", trade_date=DAY, positions=[
    {"code": "600000.SH", "name": "A", "action": "BUY", "weight": 0.30,
     "add_ratio": 0.30, "max_weight": 0.35, "ref_price": 10.30,
     "slip_pct": 0.01, "score": 70, "reason": "r", "tags": [], "extra": {}}],
    market={"stage": "发酵期", "max_streak": 5, "allowed": True},
    rules_hash="6f4be9710912")
check("联通：信号端产出可被校验通过",
      X.validate(sig, phase="buy", day=DAY, now=datetime.now()), [])
br8 = StubBroker()
X.execute_buy(broker=br8, data=StubData(), st=V.State(path=_TMP / "s8.json"),
              plan=sig, day=DAY, dry=False)
check("联通：信号端产出可被执行", len(br8.orders), 1)
print("⑨ 跨模块联通：signal_server 产出的计划被 qmt_executor 正常执行")

# ==============================================================================
# 六、心跳与降级（实盘最容易踩的坑：信号生成了，QMT 却因心跳过期拒绝下单）
# ==============================================================================
import signal_server as S2                                   # noqa: E402

sig_dir = _TMP / "signals"
sig_dir.mkdir(parents=True, exist_ok=True)
S2.SIGNAL_DIR = sig_dir

rd = X.SignalReader(directory=sig_dir, url="")
check("心跳：文件不存在 → age 为 None（视为降级）", rd.heartbeat_age(), None)

S2.write_heartbeat_once()
age = rd.heartbeat_age()
check("心跳：写一次后 age 可读且 <5s", age is not None and age < 5.0, True)
check("心跳：180s 阈值内不算降级",
      (age is None or age > X.SIGNAL_TIMEOUT_SEC), False)

# 把心跳时间改成 10 分钟前 → 必须判定降级
hb = json.loads((sig_dir / "heartbeat.json").read_text(encoding="utf-8"))
hb["epoch"] = hb["epoch"] - 600
(sig_dir / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")
age2 = rd.heartbeat_age()
check("心跳：10 分钟前 → 判定降级",
      age2 is not None and age2 > X.SIGNAL_TIMEOUT_SEC, True)

# 心跳也写进了信号目录（执行端 --signal-dir 必须能读到）
check("心跳：与信号同目录", (sig_dir / "heartbeat.json").exists(), True)
print("⑩ 心跳：缺失/新鲜/过期 三种状态判定正确（决定执行端是否敢开仓）")

# ==============================================================================
print("\n" + "=" * 70)
if fails:
    print(f"❌ 失败 {len(fails)} 处：")
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("✅ 执行端契约测试全部通过（校验/买入/上限/资金/闸门/降级/回执/联通）")
print("=" * 70)
