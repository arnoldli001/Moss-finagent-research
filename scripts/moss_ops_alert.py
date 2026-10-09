#!/usr/bin/env python
"""运维告警的**共用件**：三层闸门 + 邮件通道 + 流水落盘（`CHG-0168`）。

## 为什么单独一个文件

本项目的运维告警有**两个互相独立的发送方**：

| 发送方 | 跑在哪 | 覆盖什么故障 |
|---|---|---|
| `moss_autostart.py` | **本机**（计划任务，30 分钟） | 自启/值守任务缺失、停用、动作被改 |
| `moss_vps_monitor.py` | **香港 VPS**（cron，1 分钟） | 整机没开 / 没网 / 链路断 |

两者**必须用同一套闸门**。写成两份的代价本项目付过很多次
（"同一个 key 写在 3 处，只改一处 ⇒ 情报 5 个端点对所有人 403"）——
所以闸门、邮件、流水三件事都放在这里，两边 import 同一份代码，
并由 `tests/unit/test_moss_autostart.py::test_both_senders_share_one_gate`
钉住"它们是同一个函数对象"。

## 三层闸门（`AGENTS.md`「告警/通知硬约束」）

1. **事件级去重** —— 同一组故障在 `dedup_hours` 内只发一次；
2. **渠道级速率** —— 24 小时内最多 `max_per_day` 封（一天很多件事别淹人）；
3. **接收者级静默** —— `quiet_hours` 内不发。

闸门状态**必须落盘**：放进程内存里，重启即失效，等于没有闸门。
被抑制时的原因必须是**人话 + 剩余时间**（"冷却中，还剩 12 分钟"），不是枚举值。

## 三态口径（本项目反复强调）

* `sent` —— 真的投出去了；
* `unconfigured` —— 通道没配（**不许说成已通知**）；
* `failed` —— 尝试了但失败。

把 `unconfigured` 混进 `sent` 是告警链路最容易骗自己的地方：
代码以为发了，实际没人知道。
"""
from __future__ import annotations

import json
import os
import smtplib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

#: 邮件三态
SENT = "sent"
UNCONFIGURED = "unconfigured"
FAILED = "failed"


# ======================================================================
# 闸门
# ======================================================================
@dataclass
class GateState:
    """闸门状态（**必须落盘**）。"""

    last_fingerprint: str = ""
    last_alert_at: str = ""
    alert_times: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> GateState:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(last_fingerprint=str(data.get("last_fingerprint") or ""),
                       last_alert_at=str(data.get("last_alert_at") or ""),
                       alert_times=[str(t) for t in (data.get("alert_times") or [])])
        except (OSError, ValueError, TypeError):
            # 读不到就按"没有历史"处理：宁可多发一次，不可漏发。
            return cls()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)          # 原子替换：半截文件会让下次读成"没有历史"


def parse_dt(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def in_quiet_hours(now: datetime, quiet: tuple[int, int] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    hour = now.hour
    if start == end:
        return False
    return (hour >= start or hour < end) if start > end else (start <= hour < end)


def decide_notify(fingerprint: str, now: datetime, state: GateState, *,
                  dedup_hours: float = 6.0,
                  max_per_day: int = 8,
                  quiet_hours: tuple[int, int] | None = None,
                  ) -> tuple[bool, str, GateState]:
    """三层闸门 → `(是否发, 人话原因, 新状态)`。**纯函数**（时间是入参，可单测）。"""
    recent = [t for t in state.alert_times
              if parse_dt(t) and parse_dt(t) > now - timedelta(hours=24)]
    state.alert_times = recent

    if state.last_fingerprint == fingerprint and state.last_alert_at:
        last = parse_dt(state.last_alert_at)
        if last is not None:
            left = timedelta(hours=dedup_hours) - (now - last)
            if left.total_seconds() > 0:
                mins = int(left.total_seconds() // 60) + 1
                return False, (f"同一组故障（{fingerprint}）{dedup_hours:g} 小时内已通知过，"
                               f"冷却中，还剩约 {mins} 分钟"), state

    if len(recent) >= max_per_day:
        return False, (f"24 小时内已发 {len(recent)} 封（上限 {max_per_day} 封），"
                       "本轮不再打扰；故障仍在，日志里每轮都有记录"), state

    if in_quiet_hours(now, quiet_hours):
        start, end = quiet_hours            # type: ignore[misc]
        return False, (f"当前处于静默时段 {start:02d}:00–{end:02d}:00，"
                       "本轮不发（日志仍在记）；故障若持续，过静默期会再次通知"), state

    state.last_fingerprint = fingerprint
    state.last_alert_at = now.isoformat(timespec="seconds")
    state.alert_times = [*recent, state.last_alert_at]
    return True, "", state


# ======================================================================
# 邮件通道（复用既有 .env 的 key，不新增配置项）
# ======================================================================
@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str
    auth_code: str
    to: str
    from_name: str

    @property
    def configured(self) -> bool:
        # ★ 占位默认值（`your_qq_number@qq.com`）**不算已配置** ——
        #   "填了个样例地址"与"能收到信"是两件事，混了就会静默丢告警。
        if not (self.user and self.auth_code and self.to):
            return False
        if "@" not in self.to or self.to.startswith("your_"):
            return False
        return True


def load_env_file(path: Path) -> dict[str, str]:
    """读 `.env`（去注释/空行/两端引号）。**进程环境优先于文件**（见 `mail_config`）。"""
    env: dict[str, str] = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            env[key] = val
    except OSError:
        pass
    return env


def mail_config(env_file: Path | None = None) -> MailConfig:
    """配置来源：**进程环境 > `.env` 文件 > 默认值**。

    VPS 上没有仓库 `.env`，所以那边靠 `/etc/moss-monitor/notify.env` 以进程环境注入
    （部署脚本的做法），本文件不需要为它加一条分支。
    """
    file_env = load_env_file(env_file) if env_file else {}

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_env.get(key) or default

    return MailConfig(
        host=get("ALERT_SMTP_HOST", "smtp.qq.com"),
        port=int(get("ALERT_SMTP_PORT", "465") or "465"),
        user=get("ALERT_SMTP_USER"),
        auth_code=get("ALERT_SMTP_AUTH_CODE"),
        to=get("ALERT_EMAIL_TO"),
        from_name=get("ALERT_EMAIL_FROM_NAME", "Moss 运维值守"),
    )


def send_mail(subject: str, body: str, cfg: MailConfig, *,
              timeout: float = 20.0) -> tuple[str, str]:
    """返回 `(三态, 说明)`：`sent` / `unconfigured` / `failed`。

    **绝不在未发送时说"已通知"** —— 这是告警链路最容易骗自己的地方。
    """
    if not cfg.configured:
        return UNCONFIGURED, "邮件通道未配置（ALERT_SMTP_USER/AUTH_CODE/ALERT_EMAIL_TO）"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg.user
    msg["To"] = cfg.to
    try:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=timeout) as smtp:
            smtp.login(cfg.user, cfg.auth_code)
            smtp.sendmail(cfg.user, [cfg.to], msg.as_string())
        return SENT, f"已发送至 {cfg.to}"
    except Exception as exc:  # noqa: BLE001 告警失败不阻断体检
        return FAILED, f"{type(exc).__name__}: {exc}"


# ======================================================================
# 流水（**健康时也要写**，否则"在跑且没事"与"早就没了"长得一模一样）
# ======================================================================
def append_line(path: Path, text: str, *,
                keep_lines: int = 500, max_bytes: int = 512_000) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > max_bytes:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()
            path.write_text("\n".join(tail[-keep_lines:]) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text.replace("\n", " | ") + "\n")
    except OSError:
        pass          # 日志写不进去不该让体检/探测本身失败


def parse_quiet_hours(text: str | None) -> tuple[int, int] | None:
    """`"23-7"` → `(23, 7)`；空/不合法 → `None`（= 不静默）。

    **空字符串必须等于"没有静默时段"** —— 可用性事故不分时段；
    被静默的只是重复提醒，首次告知从不静默。
    """
    if not text:
        return None
    try:
        start, _, end = text.partition("-")
        s, e = int(start), int(end)
    except (ValueError, AttributeError):
        return None
    if not (0 <= s <= 23 and 0 <= e <= 23):
        return None
    return (s, e)
