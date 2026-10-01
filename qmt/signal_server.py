# -*- coding: utf-8 -*-
"""本地选股信号端 —— 产出买卖信号给 QMT 执行端（契约见 SIGNAL_CONTRACT.md）

跑在**有完整数据栈的机器**上（warehouse.db + eltdx），复用生产选股链路：
    sources.fetch_preheat → sources.build_candidates → service.run_selection
所以 12 个打分维度**一个都不缺**，不需要在 QMT 里做权重归一化降级。

用法
----
    # 09:10 预热（昨日涨停/连板/题材，约 180s）
    python qmt/signal_server.py --phase preheat

    # 09:25 竞价定格 → 完整打分 → 写买入计划
    python qmt/signal_server.py --phase buy

    # 09:26 算持仓股当日「抢跑」标签 → 写规则①清仓名单（需 QMT 回执里的持仓）
    python qmt/signal_server.py --phase sellalert

    # 另开一个进程暴露 HTTP（跨机部署时才需要）
    python qmt/signal_server.py --http-port 8765

产出目录 signals/：buy_*.json / sellalert_*.json / heartbeat.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = "auction_v3.signal/1"
SIGNAL_DIR = ROOT / "signals"

# ---- 仓位意图（与回测 V3 一致；本地只给比例，QMT 用实时权益换算金额）----
WEIGHT_BASE = 0.30
WEIGHT_HIGH = 0.20
MAX_WEIGHT_SINGLE = 0.35
ADD_RATIO = 0.30
RUSH_MULT = 1.20
HIGH_STREAK = 7
MAX_BUY_PER_DAY = 3
MAX_BUY_HIGH = 2

# ---- 守卫（QMT 端会强制校验）----
MAX_TOTAL_AMOUNT = 500_000.0    # 单次计划买入总额上限（元），防信号端出 bug 打爆仓位
MAX_ORDERS = 5
MIN_CASH_RESERVE = 0.0

BUY_TTL_SEC = 360             # 买入计划有效期：9:25 生成 → 9:31 过期
SELLALERT_TTL_SEC = 6 * 3600  # 规则①名单全天有效
HEARTBEAT_INTERVAL = 5.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再改名，避免对端读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def make_plan_id(trade_date: str, phase: str, positions: list[dict]) -> str:
    body = json.dumps([[p.get("code"), p.get("weight"), p.get("reason")]
                       for p in positions], ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:6]
    return f"{trade_date}-{phase.replace('_', '')}-{h}"


def envelope(*, phase: str, trade_date: str, positions: list[dict],
             market: dict, rules_hash: str = "", mode: str = "full",
             missing: list[str] | None = None, ttl: int = BUY_TTL_SEC,
             notes: list[str] | None = None) -> dict[str, Any]:
    now = datetime.now()
    return {
        "schema": SCHEMA,
        "plan_id": make_plan_id(trade_date, phase, positions),
        "trade_date": trade_date,
        "phase": phase,
        "generated_at": now.isoformat(timespec="seconds"),
        "expire_at": (now + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
        "source": {"host": socket.gethostname(), "rules_hash": rules_hash,
                   "mode": mode, "missing_dims": missing or []},
        "market": market,
        "guards": {"max_total_amount": MAX_TOTAL_AMOUNT, "max_orders": MAX_ORDERS,
                   "min_cash_reserve": MIN_CASH_RESERVE},
        "positions": positions,
        "notes": notes or [],
    }


# ==============================================================================
# 复用生产选股链路
# ==============================================================================
def run_production_selection(trade_date: str) -> dict[str, Any]:
    """跑一次生产选股，返回 RunResult.to_dict()。"""
    from src.auction_select import service, sources
    from src.auction_select.config import load_config
    cfg = load_config()
    log("预热：昨日涨停 / 连板梯队 / 题材热度 …")
    preheat = sources.fetch_preheat(trade_date, config=cfg)
    log(f"预热完成：{len(getattr(preheat, 'limit_up', []) or [])} 只昨日涨停"
        f"，耗时 {getattr(preheat, 'seconds', 0):.1f}s")
    log("主任务：取竞价序列 → 前置筛选 → 12 维打分 …")
    res = service.run_selection(trade_date=trade_date, config=cfg, preheat=preheat,
                                persist=True)
    log(f"选股完成：候选 {res.candidates} / 打分 {res.scored} / "
        f"入选 {len(res.picked)} / 耗时 {res.seconds:.1f}s")
    return res.to_dict()


def market_block(run: dict[str, Any]) -> dict[str, Any]:
    mc = run.get("market_cycle") or {}
    return {
        "stage": mc.get("stage") or "",
        "temperature": mc.get("temperature"),
        "max_streak": mc.get("max_streak") or 0,
        "broken_rate": mc.get("broken_rate"),
        "allowed": bool(run.get("status") == "done"
                        and getattr_safe(mc, "t_allowed", True)),
    }


def getattr_safe(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def missing_dims(run: dict[str, Any]) -> list[str]:
    """列出本次实际缺席的打分维度（用于在信号里如实标注）。"""
    picks = run.get("picked") or []
    if not picks:
        return []
    import json as _json
    feat = picks[0].get("features")
    if isinstance(feat, str):
        try:
            feat = _json.loads(feat)
        except (ValueError, TypeError):
            feat = {}
    feat = feat or {}
    need = {"theme_heat": "题材热度", "takeover_score": "承接强度",
            "prev_seal_to_float_ratio": "封流比", "market_temperature": "情绪周期",
            "turnover_rate": "换手率"}
    return [label for key, label in need.items()
            if feat.get(key) in (None, "", 0)]


def build_buy_plan(run: dict[str, Any], trade_date: str) -> dict[str, Any]:
    """RunResult → 买入计划。"""
    import json as _json
    picks = run.get("picked") or []
    mc = run.get("market_cycle") or {}
    max_streak = int(mc.get("max_streak") or 0)
    high = max_streak >= HIGH_STREAK
    top_n = MAX_BUY_HIGH if high else MAX_BUY_PER_DAY
    weight = WEIGHT_HIGH if high else WEIGHT_BASE

    positions: list[dict[str, Any]] = []
    notes: list[str] = []
    for item in picks[:top_n]:
        feat = item.get("features")
        if isinstance(feat, str):
            try:
                feat = _json.loads(feat)
            except (ValueError, TypeError):
                feat = {}
        feat = feat or {}
        tags = list(feat.get("rush_labels") or [])
        mult = RUSH_MULT if "抢筹" in tags else 1.0
        try:
            ref = float(feat.get("open_price") or 0.0)
        except (TypeError, ValueError):
            ref = 0.0
        if ref <= 0:
            notes.append(f"{item.get('code')} 缺 9:25 参考价(open_price)，已跳过")
            continue
        positions.append({
            "code": str(item.get("code") or ""),
            "name": str(item.get("name") or ""),
            "action": "BUY",
            "weight": round(weight * mult, 4),
            "add_ratio": ADD_RATIO,
            "max_weight": MAX_WEIGHT_SINGLE,
            "ref_price": round(ref, 3),
            "slip_pct": 0.01,
            "score": round(float(item.get("total_score") or 0.0), 2),
            "reason": str(item.get("buy_reason") or "")[:300],
            "tags": tags,
            "extra": {
                "open_gap_pct": feat.get("open_gap_pct"),
                "takeover_score": feat.get("takeover_score"),
                "auction_volume_ratio": feat.get("auction_volume_ratio"),
                "auction_volume_vs_yesterday":
                    feat.get("auction_volume_vs_yesterday"),
                "theme_name": feat.get("theme_name"),
                "theme_heat": feat.get("theme_heat"),
                "jump_gap": feat.get("jump_gap"),
                "prev_limit_up_streak": feat.get("prev_limit_up_streak"),
                "rush_note": feat.get("rush_note"),
            },
        })
    miss = missing_dims(run)
    return envelope(
        phase="buy", trade_date=trade_date, positions=positions,
        market=market_block(run), rules_hash=str(run.get("rules_hash") or ""),
        mode="degraded" if miss else "full", missing=miss,
        ttl=BUY_TTL_SEC,
        notes=notes + [f"入选 {len(picks)} 只，本计划取前 {len(positions)} 只",
                       f"最高连板 {max_streak} → {'高位日' if high else '常规日'}"
                       f"，单只仓位 {weight:.0%}"])


# ==============================================================================
# 规则①清仓名单：持仓股的当日「抢跑」标签
# ==============================================================================
def load_qmt_positions() -> list[str]:
    """从 QMT 最近一次回执里读持仓代码。"""
    files = sorted(SIGNAL_DIR.glob("fill_*.json"))
    if not files:
        log("[WARN] 没有 fill_*.json，无法得知 QMT 持仓 → 规则①名单为空。"
            "请确认 QMT 执行端已跑过至少一次。")
        return []
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        return [str(p.get("code")) for p in (data.get("positions") or [])
                if p.get("volume")]
    except Exception as e:                                   # noqa: BLE001
        log(f"[WARN] 读回执失败：{type(e).__name__} {e}")
        return []


def build_sell_alert(trade_date: str, held: list[str]) -> dict[str, Any]:
    """算持仓股 9:25 的「抢跑」标签 —— **完全复用生产链路**，不另写一套判定。

    规则①需要三个条件：市场最高连板 ≥7、持仓盈利、当日竞价抢跑。
    QMT 自己判前两条，本端只负责第三条 —— 因为「抢跑」依赖
    9:24:40~9:25:00 的竞价撮合价序列，14:56 已经取不到了。

    装配方式**照抄 `service.run_selection`**：preheat + themes + market_cycle
    三个注入齐全，`build_feature` 才算得出 `rush_labels`。
    """
    from src.auction_select import service, sources
    from src.auction_select.config import load_config
    cfg = load_config()

    preheat = sources.fetch_preheat(trade_date, config=cfg)
    market = sources.fetch_market_cycle(trade_date) or {}
    max_streak = int(market.get("max_streak") or 0)
    log(f"市场情绪：{market.get('stage')} 最高连板 {max_streak} "
        f"温度 {market.get('temperature')}")

    positions: list[dict[str, Any]] = []
    if max_streak < HIGH_STREAK:
        log(f"最高连板 {max_streak} < {HIGH_STREAK} → 规则①本次不适用，名单为空")
    elif held:
        cands = [{"code": c, "name": "", "full_code": ""} for c in held]
        themes = sources.fetch_stock_themes([c["code"] for c in cands], config=cfg)
        snaps = sources.fetch_auction_batch(cands, config=cfg)
        snaps = snaps if isinstance(snaps, list) else [snaps]
        for snap in snaps:
            code = getattr(snap, "code", "")
            try:
                feat = service.build_feature(snap, preheat=preheat, themes=themes,
                                             market_cycle=market, config=cfg)
            except Exception as e:                           # noqa: BLE001
                log(f"[WARN] {code} 特征计算失败：{type(e).__name__} {e}")
                continue
            tags = list(feat.get("rush_labels") or [])
            if "抢跑" in tags:
                positions.append({
                    "code": code, "action": "SELL_ALL",
                    "reason": "最高连板≥7 且盈利且竞价抢跑 清仓",
                    "tags": tags,
                    "jump_gap": feat.get("jump_gap"),
                    "auction_volume_ratio": feat.get("auction_volume_ratio"),
                    "open_gap_pct": feat.get("open_gap_pct"),
                    "rush_note": feat.get("rush_note"),
                })
    return envelope(
        phase="sell_alert", trade_date=trade_date, positions=positions,
        market={"stage": market.get("stage") or "", "max_streak": max_streak,
                "temperature": market.get("temperature"),
                "broken_rate": market.get("broken_rate"), "allowed": True},
        ttl=SELLALERT_TTL_SEC,
        notes=[f"最高连板 {max_streak}；持仓 {len(held)} 只；"
               f"命中规则① {len(positions)} 只",
               "另两个条件（持仓盈利、执行清仓）由 QMT 端判定"])


# ==============================================================================
# HTTP 模式（跨机部署）
# ==============================================================================
def serve_http(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):                           # noqa: D102
            pass

        def _send(self, payload: Any, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                    # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            day = (q.get("date") or [datetime.now().strftime("%Y%m%d")])[0]
            if u.path == "/heartbeat":
                p = SIGNAL_DIR / "heartbeat.json"
            elif u.path == "/buy":
                p = SIGNAL_DIR / f"buy_{day}.json"
            elif u.path == "/sellalert":
                p = SIGNAL_DIR / f"sellalert_{day}.json"
            else:
                return self._send({"error": "not found"}, 404)
            if not p.exists():
                return self._send({"error": "no signal", "path": p.name}, 404)
            try:
                return self._send(json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:                           # noqa: BLE001
                return self._send({"error": str(e)}, 500)

        def do_POST(self):                                   # noqa: N802
            if urlparse(self.path).path != "/fill":
                return self._send({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                day = str(data.get("trade_date") or
                          datetime.now().strftime("%Y%m%d"))
                atomic_write(SIGNAL_DIR / f"fill_{day}.json", data)
                log(f"收到 QMT 回执：{len(data.get('orders') or [])} 笔委托")
                return self._send({"ok": True})
            except Exception as e:                           # noqa: BLE001
                return self._send({"error": str(e)}, 400)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    log(f"HTTP 信号服务已启动：http://0.0.0.0:{port}/  "
        f"（GET /heartbeat /buy /sellalert，POST /fill）")
    srv.serve_forever()


def write_heartbeat_once() -> None:
    """写一次心跳。每次产出信号后都要调 —— 否则执行端会因为心跳过期而判定降级，
    **拒绝一切开仓**（这是实盘最容易踩的坑：信号明明生成了，QMT 却不下单）。"""
    atomic_write(SIGNAL_DIR / "heartbeat.json", {
        "schema": "auction_v3.heartbeat/1",
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "at": datetime.now().isoformat(timespec="seconds"),
        "epoch": time.time(),
    })


def heartbeat_loop(stop_after: float = 0.0) -> None:
    """持续写心跳，供 QMT 判断本地是否在线。

    `stop_after=0`（默认）→ **常驻**，写一次就 sleep 5 秒、永不退出。
    建议交易时段开一个常驻进程：`--phase heartbeat`
    """
    start = time.time()
    while True:
        write_heartbeat_once()
        if stop_after and time.time() - start > stop_after:
            return
        time.sleep(HEARTBEAT_INTERVAL)


# ==============================================================================
def main(argv: list[str] | None = None) -> int:
    global SIGNAL_DIR                     # 必须在任何引用之前声明
    ap = argparse.ArgumentParser(description="本地选股信号端（产出信号给 QMT）")
    ap.add_argument("--phase", choices=("preheat", "buy", "sellalert", "heartbeat"),
                    default="buy")
    ap.add_argument("--trade-date", default="", help="默认今天 YYYYMMDD")
    ap.add_argument("--out-dir", default=str(SIGNAL_DIR))
    ap.add_argument("--http-port", type=int, default=0,
                    help="额外暴露 HTTP 服务（跨机部署用）")
    ap.add_argument("--heartbeat-seconds", type=float, default=0.0,
                    help="写心跳的持续秒数（0=写一次就退出）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    SIGNAL_DIR = Path(args.out_dir)
    day = args.trade_date or datetime.now().strftime("%Y%m%d")
    log(f"本地信号端启动：phase={args.phase} trade_date={day} out={SIGNAL_DIR}")

    if args.http_port:
        import threading
        threading.Thread(target=serve_http, args=(args.http_port,),
                         daemon=True).start()

    try:
        if args.phase == "preheat":
            from src.auction_select import sources
            from src.auction_select.config import load_config
            sources.fetch_preheat(day, config=load_config())
            write_heartbeat_once()
            log("预热完成")

        elif args.phase == "buy":
            run = run_production_selection(day)
            plan = build_buy_plan(run, day)
            path = SIGNAL_DIR / f"buy_{day}.json"
            atomic_write(path, plan)
            write_heartbeat_once()
            log(f"[OK] 买入计划已写出：{path.name}  plan_id={plan['plan_id']}  "
                f"{len(plan['positions'])} 只")
            for p in plan["positions"]:
                log(f"     {p['code']} {p['name']} 权重 {p['weight']:.0%} "
                    f"参考价 {p['ref_price']} 得分 {p['score']} {p['tags']}")

        elif args.phase == "sellalert":
            held = load_qmt_positions()
            log(f"QMT 回执里的持仓：{held or '（无）'}")
            alert = build_sell_alert(day, held)
            path = SIGNAL_DIR / f"sellalert_{day}.json"
            atomic_write(path, alert)
            write_heartbeat_once()
            log(f"[OK] 规则①名单已写出：{path.name}  "
                f"{len(alert['positions'])} 只命中")

        else:                                                # heartbeat
            heartbeat_loop(args.heartbeat_seconds)
            log("心跳已写出")

        if args.heartbeat_seconds and args.phase != "heartbeat":
            heartbeat_loop(args.heartbeat_seconds)
    except Exception as e:                                   # noqa: BLE001
        log(f"[ERROR] {type(e).__name__} {e}")
        log(traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
