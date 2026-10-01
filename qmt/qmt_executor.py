# -*- coding: utf-8 -*-
"""QMT 执行端 —— 读取本地信号并下单（契约见 SIGNAL_CONTRACT.md）

职责边界（**刻意做薄**）：
  · 收不到买入信号 → **绝不开仓**（QMT 没有题材数据，自己选出来的票是错的）
  · 价格类卖出规则 ②-⑦ → **本地判定**（14:56 这一刻 QMT 离行情最近）
  · 需要题材/竞价序列的规则① → 读本地 sell_alert

复用 `auction_v3_qmt.py` 的：券商封装 Broker、状态 State、卖出规则 plan_sells、
行情适配 QmtData、涨跌停价、连接逻辑。**卖出口径只有一份实现，不会漂移。**

用法
----
    # 09:25 后执行买入计划
    python qmt_executor.py --phase buy --account 你的账号 --mini-path ...\\userdata_mini

    # 14:52 执行卖出（规则①来自 sell_alert，②-⑦本地判）
    python qmt_executor.py --phase sell --account 你的账号 --mini-path ...

    # 跨机部署：信号走 HTTP
    python qmt_executor.py --phase buy --signal-url http://192.168.1.10:8765

    # 不加 --live 就是 dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auction_v3_qmt as V                                   # noqa: E402

SCHEMA_SIGNAL = "auction_v3.signal/1"
SCHEMA_FILL = "auction_v3.fill/1"
SIGNAL_DIR = HERE.parent / "signals"

#: 心跳超过这个秒数 → 进入降级模式：停止开仓，但价格类止损照常生效。
SIGNAL_TIMEOUT_SEC = 180
#: 买入计划允许的最晚执行时刻（超过就拒收，防隔夜/陈旧信号）
BUY_CUTOFF_HHMM = "09:40"
SELL_CUTOFF_HHMM = "14:59"


def log(msg: str) -> None:
    V.log(f"[executor] {msg}")


# ==============================================================================
# 信号读取（文件 / HTTP）
# ==============================================================================
class SignalReader:
    def __init__(self, *, directory: Path | None = None, url: str = "") -> None:
        self.dir = Path(directory) if directory else SIGNAL_DIR
        self.url = url.rstrip("/")

    # ---- 底层 ----
    def _get(self, name: str) -> dict[str, Any] | None:
        if self.url:
            import urllib.error
            import urllib.request
            try:
                with urllib.request.urlopen(f"{self.url}/{name}", timeout=5) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                log(f"[WARN] HTTP 读 {name} 失败：HTTP {e.code}")
                return None
            except Exception as e:                           # noqa: BLE001
                log(f"[WARN] HTTP 读 {name} 失败：{type(e).__name__} {e}")
                return None
        p = self.dir / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:                               # noqa: BLE001
            log(f"[WARN] 读 {p.name} 失败：{type(e).__name__} {e}")
            return None

    # ---- 语义 ----
    def heartbeat_age(self) -> float | None:
        hb = self._get("heartbeat.json")
        if not hb:
            return None
        try:
            return max(0.0, time.time() - float(hb["epoch"]))
        except (KeyError, TypeError, ValueError):
            try:
                t = datetime.fromisoformat(str(hb.get("at")))
                return max(0.0, (datetime.now() - t).total_seconds())
            except (TypeError, ValueError):
                return None

    def buy(self, day: str) -> dict[str, Any] | None:
        return self._get(f"buy_{day}.json")

    def sell_alert(self, day: str) -> dict[str, Any] | None:
        return self._get(f"sellalert_{day}.json")

    def write_fill(self, day: str, payload: dict[str, Any]) -> None:
        if self.url:
            import urllib.request
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.url}/fill", data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
                return
            except Exception as e:                           # noqa: BLE001
                log(f"[WARN] HTTP 回传 fill 失败：{type(e).__name__} {e}")
        V.atomic_write(self.dir / f"fill_{day}.json", payload)


# ==============================================================================
# 契约校验
# ==============================================================================
def validate(plan: dict[str, Any], *, phase: str, day: str,
             now: datetime) -> list[str]:
    """逐条校验契约 §6 的强制项。返回错误列表，非空则**整单拒收**。"""
    errs: list[str] = []
    if plan.get("schema") != SCHEMA_SIGNAL:
        errs.append(f"schema 不匹配：{plan.get('schema')!r} != {SCHEMA_SIGNAL!r}")
    if str(plan.get("trade_date") or "") != day:
        errs.append(f"trade_date 不是今天：{plan.get('trade_date')} != {day}")
    if str(plan.get("phase") or "") != phase:
        errs.append(f"phase 不匹配：{plan.get('phase')} != {phase}")
    exp = str(plan.get("expire_at") or "")
    if exp:
        try:
            if now > datetime.fromisoformat(exp):
                errs.append(f"信号已过期：expire_at={exp}")
        except ValueError:
            errs.append(f"expire_at 格式非法：{exp}")
    else:
        errs.append("缺 expire_at")
    g = plan.get("guards") or {}
    n = len(plan.get("positions") or [])
    mx = int(g.get("max_orders") or 0)
    if mx and n > mx:
        errs.append(f"标的数 {n} 超过 max_orders {mx}")
    return errs


def plan_amount(plan: dict[str, Any]) -> float:
    """计划的权重合计（用于日志；真正的金额校验在执行时用实时权益再算一次）。"""
    tot = 0.0
    for p in plan.get("positions") or []:
        try:
            tot += float(p.get("weight") or 0.0)
        except (TypeError, ValueError):
            pass
    return tot


# ==============================================================================
# 执行：买入
# ==============================================================================
def execute_buy(*, broker: V.Broker, data: V.QmtData, st: V.State,
                plan: dict[str, Any], day: str, dry: bool) -> dict[str, Any]:
    eq = broker.equity()
    cash = broker.cash()
    pos = broker.positions()
    guards = plan.get("guards") or {}
    max_total = float(guards.get("max_total_amount") or 0.0)
    min_reserve = float(guards.get("min_cash_reserve") or 0.0)
    market = plan.get("market") or {}

    orders: list[dict[str, Any]] = []
    warnings: list[str] = []
    spent = 0.0

    if not market.get("allowed", True):
        warnings.append(f"市场闸门关闭（{market.get('stage')}）→ 本计划不开仓")
        log("⚠️ " + warnings[-1])
        return {"orders": orders, "warnings": warnings, "spent": 0.0}

    codes = [to_qmt(p) for p in plan.get("positions") or []]
    ticks = data.full_tick(codes) if codes else {}

    for p in plan.get("positions") or []:
        qc = to_qmt(p)
        code = V.bare(qc)
        try:
            weight = float(p.get("weight") or 0.0)
            add_ratio = float(p.get("add_ratio") or 0.0)
            max_w = float(p.get("max_weight") or 1.0)
            ref = float(p.get("ref_price") or 0.0)
            slip = float(p.get("slip_pct") or V.BUY_SLIP)
        except (TypeError, ValueError) as e:
            orders.append(_skip(code, p, f"字段非法 {e}"))
            continue
        if ref <= 0 or weight <= 0:
            orders.append(_skip(code, p, "ref_price/weight 缺失"))
            continue

        tk = ticks.get(qc) or {}
        prev_close = float(tk.get("lastClose") or 0.0)
        if prev_close <= 0:
            orders.append(_skip(code, p, "取不到前收，无法定涨停价"))
            continue
        limit = min(V.limit_up_price(prev_close), ref * (1.0 + slip))

        held = pos.get(code)
        if held is not None and add_ratio > 0:
            already = float(getattr(held, "market_value", 0) or 0)
            budget = already * add_ratio
            why = f"加仓（已有市值 {already:,.0f} × {add_ratio:.0%}）"
        else:
            already = float(getattr(held, "market_value", 0) or 0) if held else 0.0
            budget = eq * weight
            why = f"新建仓（权益 {eq:,.0f} × {weight:.1%}）"
        cap = eq * max_w
        budget = min(budget, max(0.0, cap - already))
        budget = min(budget, cash - min_reserve - spent)
        if max_total > 0:
            budget = min(budget, max_total - spent)
        if budget <= 0:
            orders.append(_skip(code, p, f"预算为 0（{why}）"))
            continue

        shares = int(budget / limit // V.LOT * V.LOT)
        if shares <= 0:
            orders.append(_skip(code, p, f"预算 {budget:,.0f} 不足一手"))
            continue

        if dry:
            log(f"  [DRY-RUN] 买入 {qc} {shares}股 限价 {limit:.3f}（{why}）")
            oid = 0
        else:
            oid = broker._order(qc, broker.c.STOCK_BUY, shares, limit,
                                f"本地信号 得分{p.get('score')}")
        if oid is not None and int(oid) >= 0:
            spent += shares * limit
            st.put(code, buy_day=day, cost=ref, shares=shares,
                   name=str(p.get("name") or ""), score=p.get("score"),
                   src_plan=str(plan.get("plan_id") or ""))
            orders.append({"code": qc, "side": "BUY", "plan_status": "pending",
                           "planned_amount": round(budget, 2), "order_id": oid,
                           "volume": shares, "price": round(limit, 3),
                           "amount": round(shares * limit, 2),
                           "fee": round(V.commission(shares * limit), 2),
                           "reason": why, "error": ""})
            V.log_trade({"time": datetime.now().strftime("%H:%M:%S"), "code": code,
                         "name": p.get("name") or "", "side": "BUY",
                         "price": round(limit, 3), "volume": shares,
                         "amount": round(shares * limit, 2),
                         "fee": round(V.commission(shares * limit), 2),
                         "reason": "本地信号", "score": p.get("score")})
        else:
            orders.append(_skip(code, p, f"下单被拒（oid={oid}）"))
    return {"orders": orders, "warnings": warnings, "spent": round(spent, 2)}


def _skip(code: str, p: dict, why: str) -> dict[str, Any]:
    log(f"  ⏭ {code} 跳过：{why}")
    return {"code": code, "side": "BUY", "plan_status": "skipped",
            "planned_amount": 0, "order_id": -1, "volume": 0, "price": 0,
            "amount": 0, "fee": 0, "reason": why, "error": ""}


def to_qmt(p: dict) -> str:
    return V.to_qmt_code(str(p.get("code") or ""))


# ==============================================================================
# 执行：卖出（规则① 读信号，②-⑦ 本地判）
# ==============================================================================
def execute_sell(*, broker: V.Broker, data: V.QmtData, st: V.State,
                 alert: dict[str, Any] | None, day: str,
                 dry: bool) -> dict[str, Any]:
    pos = broker.positions()
    orders: list[dict[str, Any]] = []
    warnings: list[str] = []

    alert_map: dict[str, dict] = {}
    if alert and str(alert.get("trade_date")) == day:
        for a in alert.get("positions") or []:
            alert_map[V.bare(str(a.get("code") or ""))] = a
    elif alert:
        warnings.append("sell_alert 不是今天的，规则①本次不判定")

    if not pos:
        log("无持仓，无需卖出")
        return {"orders": orders, "warnings": warnings, "spent": 0.0}

    market_streak = int((alert or {}).get("market", {}).get("max_streak")
                        or (st.data.get("market") or {}).get("max_streak") or 0)
    market_high = market_streak >= V.HIGH_STREAK

    ticks = data.full_tick([V.to_qmt_code(c) for c in pos])
    actions: list[V.SellAction] = []
    for code, p in pos.items():
        stp = st.get(code) or {}
        cost = float(stp.get("cost") or getattr(p, "avg_price", 0)
                     or getattr(p, "open_price", 0) or 0)
        buy_day = str(stp.get("buy_day") or "")
        if not buy_day:
            if not V.ADOPT_UNKNOWN_POSITIONS:
                warnings.append(f"{code} 非本策略持仓（无状态记录），跳过")
                continue
            buy_day = day
        held = held_days(data, buy_day, day)
        tk = ticks.get(V.to_qmt_code(code)) or {}
        px = float(tk.get("lastPrice") or tk.get("open") or 0.0)
        if px <= 0:
            warnings.append(f"{code} 取不到现价，跳过")
            continue
        pc = float(tk.get("lastClose") or 0.0) or None
        d_chg = (px / pc - 1.0) if pc else None
        up = V.limit_up_price(pc) if pc else None
        sealed = bool(up and px >= up - 0.001)
        rush = list((alert_map.get(code) or {}).get("tags") or [])
        act = V.plan_sells(
            code=code, name=str(stp.get("name") or code), held_days=held,
            cost=cost, price=px, is_sealed=sealed,
            ma_exit=V.ma_of(code, day, V.MA_EXIT_WINDOW, data, last_price=px),
            d_chg=d_chg, market_high=market_high, rush=rush)
        if act:
            actions.append(act)
    actions.sort(key=lambda a: a.priority)

    for a in actions:
        p = pos[a.code]
        can_use = float(getattr(p, "can_use_volume", 0) or 0)
        qty = int(can_use * a.qty_ratio // V.LOT * V.LOT)
        tk = ticks.get(V.to_qmt_code(a.code)) or {}
        px = float(tk.get("lastPrice") or 0.0)
        if qty <= 0:
            log(f"  ⏭ {a.code} 可卖 {can_use:.0f} 股不足（{a.reason}）")
            continue
        low = V.limit_down_price(float(tk.get("lastClose") or px))
        sell_px = max(low, px * (1 - V.SELL_SLIP))
        if dry:
            log(f"  [DRY-RUN] 卖出 {a.code} {qty}股 限价 {sell_px:.3f}（{a.reason}）")
            oid = 0
        else:
            oid = broker._order(V.to_qmt_code(a.code), broker.c.STOCK_SELL, qty,
                                sell_px, a.reason)
        if oid is not None and int(oid) >= 0:
            if a.qty_ratio >= 1.0:
                st.pop(a.code)
            orders.append({"code": V.to_qmt_code(a.code), "side": "SELL",
                           "plan_status": "pending", "planned_amount": 0,
                           "order_id": oid, "volume": qty,
                           "price": round(sell_px, 3),
                           "amount": round(qty * sell_px, 2),
                           "fee": round(V.commission(qty * sell_px), 2),
                           "reason": a.reason, "error": ""})
            V.log_trade({"time": datetime.now().strftime("%H:%M:%S"),
                         "code": a.code,
                         "name": str((st.get(a.code) or {}).get("name") or ""),
                         "side": "SELL", "price": round(sell_px, 3), "volume": qty,
                         "amount": round(qty * sell_px, 2),
                         "fee": round(V.commission(qty * sell_px), 2),
                         "reason": a.reason, "score": ""})
    return {"orders": orders, "warnings": warnings, "spent": 0.0}


def held_days(data: V.QmtData, buy_day: str, today: str) -> int:
    try:
        dates = data.trading_dates(buy_day, today)
        ds = [str(d)[:8] for d in dates if str(d)[:8] <= today]
        return max(0, len(ds) - 1) if ds else 0
    except Exception:                                        # noqa: BLE001
        return 0


# ==============================================================================
# 回执
# ==============================================================================
def build_fill(*, broker: V.Broker, st: V.State, day: str, phase: str,
               plan_id: str, account: str, result: dict[str, Any],
               degraded: bool, extra_warnings: list[str]) -> dict[str, Any]:
    asset = broker.asset()
    pos = broker.positions()
    warn = list(result.get("warnings") or []) + list(extra_warnings)
    if degraded:
        warn.insert(0, f"降级运行：本地心跳超时（>{SIGNAL_TIMEOUT_SEC}s），"
                       f"本次仅执行价格类卖出规则，未开仓")
    positions = []
    for c, p in pos.items():
        stp = st.get(c) or {}
        positions.append({
            "code": V.to_qmt_code(c),
            "volume": float(getattr(p, "volume", 0) or 0),
            "can_use": float(getattr(p, "can_use_volume", 0) or 0),
            "avg_price": float(getattr(p, "avg_price", 0) or 0),
            "market_value": float(getattr(p, "market_value", 0) or 0),
            # QMT 持仓结构不含买入日，靠状态文件补 —— 本地据此判规则①/②/⑤/⑦
            "buy_day": str(stp.get("buy_day") or ""),
            "strategy_cost": stp.get("cost"),
        })
    return {
        "schema": SCHEMA_FILL,
        "trade_date": day,
        "phase": phase,
        "plan_id": plan_id,
        "executed_at": datetime.now().isoformat(timespec="seconds"),
        "account": account,
        "equity": round(float(getattr(asset, "total_asset", 0) or 0), 2),
        "cash": round(float(getattr(asset, "cash", 0) or 0), 2),
        "market_value": round(float(getattr(asset, "market_value", 0) or 0), 2),
        "orders": result.get("orders") or [],
        "positions": positions,
        "rejected": [],
        "warnings": warn,
    }


# ==============================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QMT 执行端（读本地信号下单）")
    ap.add_argument("--phase", choices=("buy", "sell", "auto"), default="auto")
    ap.add_argument("--account", default=os.environ.get("QMT_ACCOUNT", ""))
    ap.add_argument("--mini-path", default=os.environ.get("QMT_MINI_PATH", ""))
    ap.add_argument("--session-id", type=int, default=20260920)
    ap.add_argument("--signal-dir", default=str(SIGNAL_DIR))
    ap.add_argument("--signal-url", default="", help="跨机部署时的 HTTP 基址")
    ap.add_argument("--live", action="store_true", help="实盘下单（默认 dry-run）")
    ap.add_argument("--force", action="store_true",
                    help="忽略心跳降级（不建议：等于放弃本地选股依据）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    V.DRY_RUN = not args.live
    dry = not args.live
    now = datetime.now()
    day = now.strftime("%Y%m%d")
    phase = args.phase
    if phase == "auto":
        phase = "buy" if now.strftime("%H:%M") < "09:45" else "sell"

    log("=" * 92)
    log(f"QMT 执行端 —— {'【实盘】' if args.live else '【DRY-RUN】'} phase={phase} "
        f"{now:%Y-%m-%d %H:%M:%S}")
    log(f"信号源：{args.signal_url or args.signal_dir}")

    if not args.account or not args.mini_path:
        log("[ERROR] 需要 --account 与 --mini-path（或环境变量）")
        return 2

    reader = SignalReader(directory=Path(args.signal_dir), url=args.signal_url)
    age = reader.heartbeat_age()
    degraded = age is None or age > SIGNAL_TIMEOUT_SEC
    if age is None:
        log(f"[WARN] 读不到心跳 → 视为降级模式")
    else:
        log(f"本地心跳：{age:.0f}s 前（阈值 {SIGNAL_TIMEOUT_SEC}s）"
            f"{' → 降级' if degraded else ' → 正常'}")

    try:
        trader, acc, const = V.connect(args.account, args.mini_path, args.session_id)
    except Exception as e:                                   # noqa: BLE001
        log(f"[ERROR] 连接失败：{type(e).__name__} {e}")
        return 3

    try:
        data = V.QmtData()
        st = V.State()
        broker = V.Broker(trader, acc, const)
        extra_warnings: list[str] = []
        plan_id = ""
        result: dict[str, Any] = {"orders": [], "warnings": [], "spent": 0.0}

        if phase == "buy":
            if now > _cutoff(now, BUY_CUTOFF_HHMM):
                log(f"已过买入截止 {BUY_CUTOFF_HHMM}，不执行买入")
                result["warnings"].append("超过 BUY_CUTOFF，未执行")
            elif degraded and not args.force:
                log("降级模式 → 不开仓（本地不在线，没有选股依据）")
                result["warnings"].append("降级模式，未开仓")
            else:
                plan = reader.buy(day)
                if not plan:
                    log(f"无买入信号（buy_{day}.json 不存在）→ 不交易")
                    result["warnings"].append("无买入信号")
                else:
                    plan_id = str(plan.get("plan_id") or "")
                    errs = validate(plan, phase="buy", day=day, now=now)
                    if errs:
                        for e in errs:
                            log(f"❌ 拒收：{e}")
                        result["warnings"] = [f"拒收：{e}" for e in errs]
                    elif plan_id and plan_id in (st.data.get("executed") or []):
                        log(f"plan_id {plan_id} 已执行过 → 幂等跳过")
                        result["warnings"].append("重复信号，已跳过")
                    else:
                        log(f"执行买入计划 {plan_id}："
                            f"{len(plan.get('positions') or [])} 只")
                        result = execute_buy(broker=broker, data=data, st=st,
                                             plan=plan, day=day, dry=dry)
                        if plan_id:
                            ex = st.data.setdefault("executed", [])
                            ex.append(plan_id)
                            st.data["executed"] = ex[-200:]
        else:                                                # sell
            if degraded and not args.force:
                log("降级模式 → 只跑价格类卖出规则 ②-⑦，规则①不判")
            alert = None if degraded else reader.sell_alert(day)
            result = execute_sell(broker=broker, data=data, st=st, alert=alert,
                                  day=day, dry=dry)
            plan_id = str((alert or {}).get("plan_id") or "")

        st.save()
        fill = build_fill(broker=broker, st=st, day=day, phase=phase,
                          plan_id=plan_id, account=args.account, result=result,
                          degraded=degraded and not args.force,
                          extra_warnings=extra_warnings)
        reader.write_fill(day, fill)
        log(f"[OK] 委托 {len(fill['orders'])} 笔｜权益 {fill['equity']:,.0f}"
            f"｜现金 {fill['cash']:,.0f}｜持仓 {len(fill['positions'])} 只")
        for w in fill["warnings"]:
            log(f"  ⚠️ {w}")
    except Exception as e:                                   # noqa: BLE001
        log(f"[ERROR] 执行异常：{type(e).__name__} {e}")
        log(traceback.format_exc())
        return 4
    finally:
        try:
            trader.stop()
        except Exception:                                    # noqa: BLE001
            pass
    return 0


def _cutoff(now: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


if __name__ == "__main__":
    raise SystemExit(main())
