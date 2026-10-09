#!/usr/bin/env python3
"""Moss **外部**可用性监控 —— 跑在香港 VPS 上，每 1 分钟一跳（`CHG-0168`）。

## 为什么必须有一个不跑在本机的东西

2026-10-05 的事故里，本机的自检/值守全都在正常工作，**没有任何东西被通知** ——
因为整条链路的"人"是本机自己，它没法告诉自己"我挂了"。更极端的情形是
整机没开、断网、Windows 起不来：那时本机上的任何脚本都**一行都不会跑**。

所以这一层刻意放在**另一台机器**上：只看一个外部事实 ——
`https://hk.wujiaitool.cn/api/v1/health/live` 还能不能拿到 200。

| 层 | 跑在哪 | 覆盖 |
|---|---|---|
| `moss_autostart.py` | 本机（计划任务，30 分钟） | 自启/值守任务被删、被停用、动作被改 |
| **本文件** | **VPS（cron，1 分钟）** | **整机没开 / 断网 / 5 跳链路任一跳断** |

## 判据与闸门

* 连续失败 **≥ `--fail-threshold`（默认 3）次**才算故障 —— 单次超时不值得半夜叫人；
* 恢复时**也发一封**（"故障已恢复"与"故障"是两件事，各有各的去重键）；
* 闸门三层复用 `moss_ops_alert.py`：本文件与 `moss_autostart.py`
  **import 同一份代码**（判据写成"同一个函数对象"，见单测），不写第二套；
* **每一跳都写流水**（含健康心跳）：否则"在跑且没事"与"早就没了"
  在文件里长得一模一样 —— 本项目实测过这个形状。

## 通道（都不需要把仓库 `.env` 整个搬上来）

优先 `MOSS_ALERT_WEBHOOK`（POST JSON，凭据面最小）；
否则用 `ALERT_SMTP_* + ALERT_EMAIL_TO`（从 `--notify-env` 指定的文件读，
不经命令行、不进日志）；两者都没有 → **明确记 `notify=unconfigured`**，
绝不假装通知过。

## 部署与实际用法（部署细节见 `docs/OPS_GUIDE.md`）

    python3 moss_vps_monitor.py --once                    # 探一次（cron 用这个）
    python3 moss_vps_monitor.py --once --url <url>        # 换目标（自测用）
    python3 moss_vps_monitor.py --status                  # 打印当前状态
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from moss_ops_alert import (  # noqa: E402 同目录共用件
    UNCONFIGURED,
    GateState,
    append_line,
    decide_notify,
    load_env_file,
    mail_config,
    parse_quiet_hours,
    send_mail,
)

DEFAULT_URL = "https://hk.wujiaitool.cn/api/v1/health/live"
DEFAULT_STATE = "/var/lib/moss-monitor/state.json"
DEFAULT_LOG = "/var/log/moss-monitor.log"

FP_DOWN = "VPS:ENTRY_DOWN"
FP_RECOVERED = "VPS:ENTRY_RECOVERED"


# ======================================================================
# 纯函数层（可离线单测）
# ======================================================================
def probe(url: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """`(是否健康, 说明)`。**只认 HTTP 200** —— 502/301/000 都不是"活着"。"""
    req = urllib.request.Request(url, headers={"User-Agent": "moss-vps-monitor/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 固定 https
            code = int(getattr(resp, "status", 0) or 0)
            return (code == 200), f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 网络类异常统一按"不通"处理
        return False, f"{type(exc).__name__}: {exc}"


def next_counter(is_ok: bool, prev: int) -> int:
    """连续失败计数：成功清零、失败 +1（**纯函数**，便于钉住"连续"的语义）。"""
    return 0 if is_ok else prev + 1


def should_alert(is_ok: bool, fail_consec: int, already_down: bool, *,
                 threshold: int) -> str:
    """→ `""` / `"down"` / `"recovered"`。

    * 失败但未达阈值 ⇒ 不响（单次抖动不值得半夜叫人）；
    * 失败且达阈值、且**之前没报过 down** ⇒ `down`（去重交给闸门，这里只管事件语义）；
    * 成功且**之前报过 down** ⇒ `recovered`（恢复必须说一声，否则人以为还挂着）。
    """
    if is_ok:
        return "recovered" if already_down else ""
    if fail_consec >= threshold and not already_down:
        return "down"
    return ""


def notify(subject: str, body: str, *, webhook: str, env_file: Path | None,
           timeout: float = 20.0) -> tuple[str, str]:
    """`(三态, 说明)`：`sent` / `unconfigured` / `failed`。"""
    if webhook:
        payload = json.dumps({"text": f"{subject}\n{body}"},
                             ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 用户自配
                return "sent", f"webhook HTTP {getattr(resp, 'status', 0)}"
        except Exception as exc:  # noqa: BLE001
            return "failed", f"webhook {type(exc).__name__}: {exc}"
    if env_file:
        for key, val in load_env_file(env_file).items():
            os.environ.setdefault(key, val)
    return send_mail(subject, body, mail_config(None), timeout=timeout)


# ======================================================================
# 状态
# ======================================================================
def _load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        # 读不到按"没有历史"处理（宁可多发一次，不可漏发）；但**记下这件事**。
        return {}


def _save_state(path: Path, data: dict) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        append_line(Path(DEFAULT_LOG), f"state_save_failed {type(exc).__name__}: {exc}")


# ======================================================================
# 一次探测（cron 每 1 分钟一跳）
# ======================================================================
def once(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    log_path = Path(args.log)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    is_ok, detail = probe(args.url, timeout=args.timeout)
    state = _load_state(state_path)
    cross = state.get("cross") or {}
    fail_consec = next_counter(is_ok, int(cross.get("fail_consec") or 0))
    was_down = bool(cross.get("alerted_down"))
    event = should_alert(is_ok, fail_consec, was_down, threshold=args.fail_threshold)

    append_line(log_path,
                f"{stamp} {detail} ok={int(is_ok)} fail_consec={fail_consec} "
                f"event={event or '-'} url={args.url}")

    # 心跳也要落一份到状态里：人问"监控还活着吗"时不必去翻日志
    cross.update({"fail_consec": fail_consec, "last_detail": detail,
                  "last_ok_at": stamp if is_ok else cross.get("last_ok_at", ""),
                  "last_checked_at": stamp})
    state["cross"] = cross

    if event:
        gate = GateState(**{
            "last_fingerprint": (state.get("gate") or {}).get("last_fingerprint", ""),
            "last_alert_at": (state.get("gate") or {}).get("last_alert_at", ""),
            "alert_times": (state.get("gate") or {}).get("alert_times", []),
        })
        fp = FP_DOWN if event == "down" else FP_RECOVERED
        send, why, gate = decide_notify(
            fp, now, gate,
            dedup_hours=args.dedup_hours, max_per_day=args.max_per_day,
            quiet_hours=parse_quiet_hours(args.quiet_hours))
        state["gate"] = {"last_fingerprint": gate.last_fingerprint,
                         "last_alert_at": gate.last_alert_at,
                         "alert_times": gate.alert_times}
        if not send:
            append_line(log_path, f"{stamp} notify_suppressed {why}")
        else:
            if event == "down":
                subject = "【Moss 运维】对外入口不可用（外部监控）"
                body = (f"外部监控（VPS）连续 {fail_consec} 次探测失败"
                        f"（阈值 {args.fail_threshold}）。\n"
                        f"目标：{args.url}\n最后一次结果：{detail}\n"
                        f"检查时间：{stamp}\n\n"
                        "这台机器不在本机网络上，所以「本机没开 / 断网 / 链路任一跳断」"
                        "都在它的覆盖范围内。\n")
            else:
                subject = "【Moss 运维】对外入口已恢复（外部监控）"
                body = (f"外部监控（VPS）重新探测成功。\n目标：{args.url}\n"
                        f"结果：{detail}\n恢复时间：{stamp}\n")
            status, note = notify(subject, body, webhook=args.webhook,
                                  env_file=Path(args.notify_env) if args.notify_env else None)
            append_line(log_path, f"{stamp} notify_{status} {note}")
            if status == "sent":
                cross["alerted_down"] = event == "down"
                state["cross"] = cross
            else:
                # 告警发不出去**本身**就是事故：不许悄悄咽下去
                append_line(log_path,
                            f"{stamp} ⚠️ 告警未能送达（{status}: {note}）"
                            "—— 这不是'没故障'，是'通知链路没配/坏了'")
                _save_state(state_path, state)
                return 2
    _save_state(state_path, state)
    return 0


def show_status(args: argparse.Namespace) -> int:
    state = _load_state(Path(args.state))
    print(json.dumps(state or {"note": "还没有状态文件（监控从未跑过？）"},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Moss 外部可用性监控（部署在 VPS）")
    ap.add_argument("--once", action="store_true", help="探一次（cron 用这个）")
    ap.add_argument("--status", action="store_true", help="打印当前状态")
    ap.add_argument("--url", default=os.environ.get("MOSS_MONITOR_URL", DEFAULT_URL))
    ap.add_argument("--state", default=os.environ.get("MOSS_MONITOR_STATE", DEFAULT_STATE))
    ap.add_argument("--log", default=os.environ.get("MOSS_MONITOR_LOG", DEFAULT_LOG))
    ap.add_argument("--fail-threshold", type=int, default=3,
                    help="连续失败几次才算故障（默认 3）")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--dedup-hours", type=float, default=6.0)
    ap.add_argument("--max-per-day", type=int, default=8)
    ap.add_argument("--quiet-hours", default=os.environ.get("MOSS_QUIET_HOURS", ""),
                    help='形如 "23-7"；留空 = 不静默（可用性事故不分时段）')
    ap.add_argument("--webhook", default=os.environ.get("MOSS_ALERT_WEBHOOK", ""))
    ap.add_argument("--notify-env", default=os.environ.get("MOSS_NOTIFY_ENV", ""),
                    help="邮件凭据文件（如 /etc/moss-monitor/notify.env，0600）")
    args = ap.parse_args(argv)
    if args.status:
        return show_status(args)
    return once(args)


if __name__ == "__main__":
    raise SystemExit(main())
