# -*- coding: utf-8 -*-
"""纸面交易（paper）模式 —— 用真实信号 + 真实行情，但不下任何真实委托。

## 它要回答什么问题

整套回测最大的未验证假设是**成交价**：

| 环节 | 回测假设 | 真实世界 |
|---|---|---|
| 买入 | 9:25 集合竞价撮合价 | 9:25 定格后报单 → **9:30 开盘价成交** |
| 卖出 | 收盘价判定 + 收盘价成交 | 14:56 判定 → **15:00 收盘价成交** |

纸面模式把「模型以为的价格」和「同一时刻真实可成交的价格」逐笔记下来，
一周后就能算出**滑点到底有多大、限价单有多少次根本没成交**。

## 怎么做到「不改变决策链路」

**只替换 broker**。`qmt_executor.execute_buy / execute_sell` 一行不改地被复用，
它们拿到的是一个 `PaperBroker` —— 接口与真实 `Broker` 完全一致，
唯一区别是 `_order()` 不报单，而是记一笔待成交意向，稍后用**真实行情**结算。

所以纸面模式验证的是**真实的决策路径**，不是另一套简化逻辑。

## 用法

    # 09:25 出信号后跑；决定 → 等待 → 用 9:30 真实开盘价结算
    python paper_trader.py --phase buy --settle-after 360

    # 14:52 判定卖出；等收盘后用真实收盘价结算
    python paper_trader.py --phase sell --settle-after 540

    # 看滑点报告
    python paper_trader.py --phase report

**不需要 --account / --mini-path**：纸面模式不连交易柜台，只用行情。
（但 MiniQMT 客户端仍须在运行，xtdata 要连它取行情。）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auction_v3_qmt as V                                   # noqa: E402
import qmt_executor as X                                     # noqa: E402

PAPER_STATE = HERE / "paper_state.json"
PAPER_LEDGER = HERE / "paper_fills.csv"

#: 纸面账户初始资金（与回测一致，便于横向对比）
PAPER_INIT_CASH = 1_000_000.0

#: 交易日计数用的最小日期（只用于 held_days 的日期切片）
_FMT = "%Y%m%d"


def log(msg: str) -> None:
    V.log(f"[paper] {msg}")


def _xtconst() -> Any:
    """取 xtconstant；拿不到就用等值常量兜底（值见官方数据字典）。"""
    try:
        from xtquant import xtconstant
        return xtconstant
    except Exception:                                        # noqa: BLE001
        return SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11)


# ==============================================================================
# 纸面账户（持久化）
# ==============================================================================
class PaperAccount:
    """模拟账户：现金 / 持仓 / 待成交意向 / 成交流水。

    ⚠️ 与真实账户的关键差别：**没有资金约束以外的任何摩擦**（不模拟排队、不模拟
    部分成交）。所以纸面结果应视为**滑点下界**，真实成交只会更差。
    """

    def __init__(self, path: Path = PAPER_STATE) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "cash": PAPER_INIT_CASH,
            "init_cash": PAPER_INIT_CASH,
            "positions": {},          # code -> {shares, cost, buy_day, name}
            "pending": [],            # 待结算意向
            "equity": [],             # [{day, equity}]
            "settled": [],            # 已结算 plan_id（幂等）
        }
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:                           # noqa: BLE001
                log(f"[WARN] 纸面账户损坏，已重置：{e}")

    # ---- 账户 ----
    @property
    def cash(self) -> float:
        return float(self.data.get("cash") or 0.0)

    @cash.setter
    def cash(self, v: float) -> None:
        self.data["cash"] = v

    @property
    def positions(self) -> dict[str, dict]:
        return self.data.setdefault("positions", {})

    @property
    def pending(self) -> list[dict]:
        return self.data.setdefault("pending", [])

    def save(self) -> None:
        V.atomic_write(self.path, self.data)

    # ---- 估值 ----
    def market_value(self, data: Any) -> float:
        if not self.positions:
            return 0.0
        codes = [V.to_qmt_code(c) for c in self.positions]
        ticks = data.full_tick(codes)
        mv = 0.0
        for c, p in self.positions.items():
            tk = ticks.get(V.to_qmt_code(c)) or {}
            px = float(tk.get("lastPrice") or tk.get("open") or 0.0) or float(p["cost"])
            mv += px * float(p["shares"])
        return mv

    def equity(self, data: Any) -> float:
        return self.cash + self.market_value(data)


# ==============================================================================
# PaperBroker —— 与真实 Broker 接口一致，但不报单
# ==============================================================================
class PaperBroker:
    """`qmt_executor` 拿到的就是这个。接口与 `auction_v3_qmt.Broker` 对齐。"""

    def __init__(self, account: PaperAccount, data: Any) -> None:
        self.acc = account
        self.d = data
        self.c = _xtconst()
        self._oid = 900000
        self.orders: list[tuple] = []

    # ---- 查询接口 ----
    def equity(self) -> float:
        return self.acc.equity(self.d)

    def cash(self) -> float:
        return self.acc.cash

    def positions(self) -> dict[str, Any]:
        """返回与真实 Broker 同形状的对象：键为**裸代码**。"""
        out: dict[str, Any] = {}
        ticks = self.d.full_tick([V.to_qmt_code(c) for c in self.acc.positions])
        for c, p in self.acc.positions.items():
            tk = ticks.get(V.to_qmt_code(c)) or {}
            px = float(tk.get("lastPrice") or tk.get("open") or 0.0) or float(p["cost"])
            out[V.bare(c)] = SimpleNamespace(
                stock_code=V.to_qmt_code(c),
                volume=float(p["shares"]),
                can_use_volume=float(p["shares"]),          # 纸面不模拟 T+1 冻结
                avg_price=float(p["cost"]),
                market_value=px * float(p["shares"]),
            )
        return out

    def asset(self) -> Any:
        mv = self.acc.market_value(self.d)
        return SimpleNamespace(total_asset=self.acc.cash + mv, cash=self.acc.cash,
                               market_value=mv)

    # ---- 下单：只登记意向 ----
    def _order(self, code: str, side: int, shares: int, price: float,
               reason: str) -> int:
        self._oid += 1
        tk = self.d.full_tick([V.to_qmt_code(code)]).get(V.to_qmt_code(code)) or {}
        decision = float(tk.get("lastPrice") or tk.get("open") or 0.0)
        self.acc.pending.append({
            "oid": self._oid,
            "code": V.bare(code),
            "qmt_code": V.to_qmt_code(code),
            "side": "BUY" if side == self.c.STOCK_BUY else "SELL",
            "shares": int(shares),
            "limit": round(float(price), 4),
            "decision_price": round(decision, 4),
            "reason": reason,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        self.orders.append((V.to_qmt_code(code), side, shares, round(price, 3), reason))
        return self._oid

    def order(self, code, shares, price, reason):            # 兼容别名
        return self._order(code, self.c.STOCK_BUY, shares, price, reason)


# ==============================================================================
# 结算：用真实行情决定「到底成没成交、成交在什么价」
# ==============================================================================
def settle(*, account: PaperAccount, data: Any, day: str) -> dict[str, Any]:
    """把待成交意向按真实行情结算。

    成交判定（**这是纸面模式最有价值的部分**）：
      · 买单：限价 L，9:30 真实开盘价 P。P ≤ L → 成交于 P；P > L → **未成交**（追不上）
      · 卖单：限价 L，收盘价 P。      P ≥ L → 成交于 P；P < L → **未成交**（砸穿了）
    未成交的记 `unfilled`，并保留下来供报告统计 ——
    **「限价单没成交」的比例本身就是回测看不见的风险。**
    """
    pending = account.pending
    if not pending:
        return {"filled": 0, "unfilled": 0, "rows": []}

    codes = sorted({p["qmt_code"] for p in pending})
    ticks = data.full_tick(codes)
    rows: list[dict[str, Any]] = []
    remain: list[dict] = []
    filled_n = unfilled_n = 0

    for it in pending:
        tk = ticks.get(it["qmt_code"]) or {}
        # 买用当日开盘价、卖用最新价（收盘后即为收盘价）
        px = (float(tk.get("open") or 0.0) if it["side"] == "BUY"
              else float(tk.get("lastPrice") or tk.get("open") or 0.0))
        if px <= 0:
            remain.append(it)                                # 取不到价，留到下轮
            continue
        limit = float(it["limit"])
        ok = (px <= limit) if it["side"] == "BUY" else (px >= limit)
        slip_bps = (px / it["decision_price"] - 1.0) * 10_000.0 \
            if it["decision_price"] else 0.0
        code = it["code"]
        row = {
            "date": day, "code": code, "side": it["side"],
            "reason": it["reason"], "shares": it["shares"],
            "decision_price": it["decision_price"], "limit": limit,
            "market_price": round(px, 4),
            "filled": int(ok), "fill_price": round(px, 4) if ok else 0.0,
            "slip_bps": round(slip_bps, 2),
            "amount": 0.0, "fee": 0.0, "note": "",
        }
        if not ok:
            unfilled_n += 1
            row["note"] = ("9:30 开盘价高于买单限价，未成交" if it["side"] == "BUY"
                           else "收盘价低于卖单限价，未成交")
            rows.append(row)
            continue

        if it["side"] == "BUY":
            amount = px * it["shares"]
            fee = V.commission(amount)
            if amount + fee > account.cash:
                row["filled"] = 0
                row["note"] = f"纸面现金不足（需 {amount + fee:,.0f}，有 {account.cash:,.0f}）"
                unfilled_n += 1
                rows.append(row)
                continue
            account.cash -= amount + fee
            pos = account.positions.get(code)
            if pos:
                tot = pos["shares"] + it["shares"]
                pos["cost"] = (pos["cost"] * pos["shares"] + px * it["shares"]) / tot
                pos["shares"] = tot
            else:
                account.positions[code] = {"shares": it["shares"], "cost": px,
                                           "buy_day": day, "name": ""}
            row.update(amount=round(amount, 2), fee=round(fee, 2))
            filled_n += 1
        else:
            pos = account.positions.get(code)
            if not pos:
                row["filled"] = 0
                row["note"] = "纸面无该持仓"
                unfilled_n += 1
                rows.append(row)
                continue
            qty = min(it["shares"], int(pos["shares"]))
            amount = px * qty
            fee = V.commission(amount) + amount * V.STAMP_TAX_RATE
            account.cash += amount - fee
            pos["shares"] -= qty
            if pos["shares"] <= 0:
                account.positions.pop(code, None)
            row.update(shares=qty, amount=round(amount, 2), fee=round(fee, 2))
            filled_n += 1
        rows.append(row)

    account.data["pending"] = remain
    _append_ledger(rows)
    return {"filled": filled_n, "unfilled": unfilled_n, "rows": rows}


def _append_ledger(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    header = ["date", "code", "side", "reason", "shares", "decision_price",
              "limit", "market_price", "filled", "fill_price", "slip_bps",
              "amount", "fee", "note"]
    new = not PAPER_LEDGER.exists()
    try:
        with PAPER_LEDGER.open("a", encoding="utf-8", newline="") as f:
            if new:
                f.write(",".join(header) + "\n")
            for r in rows:
                f.write(",".join(str(r.get(k, "")) for k in header) + "\n")
    except OSError as e:
        log(f"[WARN] 写流水失败：{e}")


# ==============================================================================
# 报告
# ==============================================================================
def report(account: PaperAccount, day: str) -> int:
    if not PAPER_LEDGER.exists():
        log("尚无纸面成交流水，先跑几次 --phase buy/sell")
        return 0
    import csv
    with PAPER_LEDGER.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        log("流水为空")
        return 0

    def num(r, k, d=0.0):
        try:
            return float(r.get(k) or d)
        except (TypeError, ValueError):
            return d

    print("=" * 90)
    print(f"纸面交易报告 —— 共 {len(rows)} 笔意向，"
          f"{rows[0].get('date')} ~ {rows[-1].get('date')}")
    print("=" * 90)
    for side, label in (("BUY", "买入"), ("SELL", "卖出")):
        sub = [r for r in rows if r.get("side") == side]
        if not sub:
            continue
        got = [r for r in sub if int(num(r, "filled")) == 1]
        miss = [r for r in sub if int(num(r, "filled")) != 1]
        print(f"\n【{label}】意向 {len(sub)} 笔｜成交 {len(got)}｜未成交 {len(miss)}"
              f"（未成交率 {len(miss) / len(sub):.1%}）")
        if got:
            slips = [num(r, "slip_bps") for r in got]
            print(f"  滑点(成交价 vs 决策时价)：均值 {statistics.mean(slips):+.1f} bp"
                  f"  中位 {statistics.median(slips):+.1f} bp"
                  f"  最差 {min(slips):+.1f} bp  最好 {max(slips):+.1f} bp")
            # ⚠️ 方向化：买入滑点为正 = 买贵了（不利）；卖出滑点为正 = 卖高了（有利）。
            #    两边符号语义相反，不区分就会得出"卖出滑点很大所以很好"的荒谬结论。
            sign = 1.0 if side == "BUY" else -1.0
            adv = [sign * s for s in slips]
            cost = sum(num(r, "amount") * max(0.0, sign * num(r, "slip_bps"))
                       / 10_000 for r in got)
            turnover = sum(num(r, "amount") for r in got)
            print(f"  **不利滑点**（已按方向取正）：均值 "
                  f"{statistics.mean(adv):+.1f} bp  中位 {statistics.median(adv):+.1f} bp"
                  f"  最差 {max(adv):+.1f} bp")
            print(f"  滑点成本 {cost:,.0f} 元"
                  f"（占成交额 {cost / max(1.0, turnover):.3%}，"
                  f"成交额 {turnover:,.0f} 元）")
        if miss:
            print("  未成交样本：" + "；".join(
                f"{r['date']} {r['code']} {r.get('note', '')[:26]}"
                for r in miss[:5]))
    tot_fee = sum(num(r, "fee") for r in rows)
    eq = account.equity(SimpleNamespace(full_tick=lambda c: {}))
    print(f"\n【账户】初始 {account.data.get('init_cash', 0):,.0f}  "
          f"现金 {account.cash:,.0f}  持仓成本口径净值 {eq:,.0f}  "
          f"累计费用 {tot_fee:,.0f}")
    print(f"  ⚠️ 纸面模式**不模拟排队与部分成交**，结果是滑点的下界；"
          f"真实成交只会更差。")
    print("=" * 90)
    return 0


# ==============================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="纸面交易（真实信号+真实行情，不下单）")
    ap.add_argument("--phase", choices=("buy", "sell", "report", "settle"),
                    default="buy")
    ap.add_argument("--signal-dir", default=str(X.SIGNAL_DIR))
    ap.add_argument("--signal-url", default="")
    ap.add_argument("--settle-after", type=float, default=0.0,
                    help="决策后等待 N 秒再结算（买入建议 360，卖出建议 540）。"
                         "0 = 立即用当前价结算（不准确，仅用于冒烟测试）")
    ap.add_argument("--no-settle", action="store_true",
                    help="只登记意向，不结算（留到下一次 --phase settle）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    now = datetime.now()
    day = now.strftime(_FMT)
    account = PaperAccount()

    if args.phase == "report":
        return report(account, day)

    try:
        data = V.QmtData()
    except Exception as e:                                   # noqa: BLE001
        log(f"[ERROR] 行情不可用（MiniQMT 是否在运行？）：{type(e).__name__} {e}")
        return 3

    broker = PaperBroker(account, data)
    reader = X.SignalReader(directory=Path(args.signal_dir), url=args.signal_url)

    try:
        if args.phase == "settle":
            r = settle(account=account, data=data, day=day)
            log(f"结算完成：成交 {r['filled']} / 未成交 {r['unfilled']}")
            account.save()
            return 0

        if args.phase == "buy":
            plan = reader.buy(day)
            if not plan:
                log(f"无买入信号（buy_{day}.json），无事可做")
                return 0
            errs = X.validate(plan, phase="buy", day=day, now=now)
            if errs:
                for e in errs:
                    log(f"❌ 信号被拒：{e}")
                return 0
            if str(plan.get("plan_id")) in (account.data.get("settled") or []):
                log("该 plan_id 已结算过 → 幂等跳过")
                return 0
            log(f"纸面买入：plan_id={plan.get('plan_id')}  "
                f"{len(plan.get('positions') or [])} 只")
            res = X.execute_buy(broker=broker, data=data, st=V.State(), plan=plan,
                                day=day, dry=False)
            log(f"登记意向 {len(res['orders'])} 笔（未报单）")
            pid = str(plan.get("plan_id") or "")
            account.data.setdefault("settled", []).append(pid)
            account.data["settled"] = account.data["settled"][-200:]
        else:                                                # sell
            alert = reader.sell_alert(day)
            log("纸面卖出：规则②-⑦本地判 + 规则①读 sell_alert")
            res = X.execute_sell(broker=broker, data=data, st=V.State(), alert=alert,
                                 day=day, dry=False)
            log(f"登记意向 {len(res['orders'])} 笔（未报单）")

        account.data.setdefault("equity", []).append(
            {"day": day, "equity": round(account.equity(data), 2)})
        account.data["equity"] = account.data["equity"][-400:]
        account.save()

        if args.no_settle:
            log("--no-settle：意向已留待下次 --phase settle 结算")
            return 0

        if args.settle_after > 0:
            log(f"等待 {args.settle_after:.0f}s 后按真实行情结算"
                f"（买入等 9:30 开盘价，卖出等收盘价）…")
            time.sleep(args.settle_after)
        r = settle(account=account, data=data, day=day)
        log(f"结算：成交 {r['filled']} / 未成交 {r['unfilled']}")
        for row in r["rows"]:
            tag = "✅" if row["filled"] else "❌"
            log(f"  {tag} {row['side']} {row['code']} "
                f"决策价 {row['decision_price']} → 限价 {row['limit']} → "
                f"市场 {row['market_price']}  滑点 {row['slip_bps']:+.1f}bp "
                f"{row.get('note', '')}")
        account.save()
    except Exception as e:                                   # noqa: BLE001
        log(f"[ERROR] {type(e).__name__} {e}")
        log(traceback.format_exc())
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
