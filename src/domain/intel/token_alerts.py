"""数据源授权到期提醒 —— **发给管理员，不发用户**。

## 需求（用户口径 2026-09-25）

> 周期提醒时……5 天开始提醒，发邮件通知到 your_qq_number@qq.com

## 三层可见性（刻意的分层）

| 层 | 看到什么 | 为什么 |
|---|---|---|
| **普通用户** | 只看到「该来源今日暂无更新」 | 用户不需要知道内部源的状态；显示故障只会带来疑问且暴露架构 |
| **管理员界面** | 「授权将于 N 天后到期」+ 刷新命令 | 可操作、可归因 |
| **邮件** | 到期/临期提醒 + 一条命令 | 用户要求的方式；不指望管理员天天盯界面 |

## 为什么"提前"提醒而不是"失效后"报警

token 是 **opaque**（无 `exp` 可读），实测有效期 7–14 天。
失效后才发现 = 采集中断（历史上就这样**静默停了 10 天**）。
所以按"最后刷新时间 + 保守阈值"在第 5 天开始提醒，留出缓冲。

## 提醒去重

同一凭证**每个告警级别只发一次**（记录在凭证文件旁边的 state 里），
否则每天一封会把邮箱刷爆，最终被忽略 —— 那比不提醒更糟。
状态变化（fresh→expiring→stale）或重新授权后，计数重置。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from src.infrastructure.credentials import (
    CRED_DIR,
    STALE_AFTER_DAYS,
    WARN_AFTER_DAYS,
    ZSXQ_FILE,
    status,
)

logger = logging.getLogger(__name__)

#: 提醒收件人。**与 `ALERT_EMAIL_TO` 分开** —— 那是行情/信号告警的收件人，
#: 这是运维提醒的收件人，两者受众可能不同（一个给投研、一个给运维）。
DEFAULT_ALERT_TO: Final = "your_qq_number@qq.com"

#: 提醒去重状态文件名
_STATE_FILE: Final = "token_alert_state.json"


def _recipient() -> str:
    import os

    return (os.environ.get("TOKEN_ALERT_EMAIL_TO", "").strip()
            or DEFAULT_ALERT_TO)


def _state_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / CRED_DIR / _STATE_FILE


def _read_state(root: Path | None = None) -> dict[str, Any]:
    p = _state_path(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any], root: Path | None = None) -> None:
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def _subject_and_body(st: dict[str, Any], *, refresh_cmd: str) -> tuple[str, str]:
    """按状态生成提醒文案。**状态不同、紧迫度不同。**"""
    state = st.get("state")
    age = st.get("age_days")
    refreshed = st.get("refreshed_at") or "(未知)"
    age_txt = f"{age} 天" if age is not None else "未知"

    if state == "stale":
        subject = "【Moss 运维】数据源授权已到期，采集可能已中断"
        head = (f"知识星球授权**已超过 {STALE_AFTER_DAYS} 天**"
                f"（最后刷新：{refreshed}，已用 {age_txt}）。\n"
                f"采集**可能已经中断**，请尽快重新授权。")
    elif state == "expiring":
        subject = f"【Moss 运维】数据源授权将于近期到期（已用 {age_txt}）"
        head = (f"知识星球授权已使用 **{age_txt}**"
                f"（最后刷新：{refreshed}）。\n"
                f"实测有效期 7–14 天，**建议现在安排重新授权**。")
    elif state == "missing":
        subject = "【Moss 运维】数据源授权缺失"
        head = "未找到知识星球授权凭证，采集将无法进行。"
    else:
        subject = "【Moss 运维】数据源授权状态异常"
        head = f"授权状态为 {state}，无法判断剩余有效期。"

    body = (
        f"{head}\n"
        f"\n"
        f"── 重新授权（一次扫码，免重启）──\n"
        f"    {refresh_cmd}\n"
        f"\n"
        f"脚本会打开浏览器让你扫码，然后把新凭证写入热加载文件。\n"
        f"**后端不需要重启** —— 读取方每次调用都会重新读盘。\n"
        f"\n"
        f"── 说明 ──\n"
        f"    · 本提醒在授权使用满 {WARN_AFTER_DAYS} 天后开始发送，每级只发一次；\n"
        f"    · 重新授权成功后计数自动重置；\n"
        f"    · 采集对用户侧始终显示「该来源暂无更新」，不会暴露内部状态。\n"
        f"\n"
        f"（本邮件由 Moss 投研平台自动发送，请勿回复）"
    )
    return subject, body


async def check_and_notify(
    *, root: Path | None = None, dry_run: bool = False,
    refresh_cmd: str = "", force: bool = False,
) -> dict[str, Any]:
    """检查凭证状态，必要时发邮件。返回本次的检查结果（**可安全进日志**）。

    `root` 仅测试用。`dry_run=True` 时只生成文案不发送（打印出来）。
    """
    st = status(ZSXQ_FILE, root=root)
    state = st.get("state", "unknown")
    result: dict[str, Any] = {"state": state, "age_days": st.get("age_days"),
                              "notified": False, "skipped": "", "to": ""}

    if state in ("fresh",):
        result["skipped"] = "fresh：未到提醒阈值，不发"
        # 回到 fresh → 重置去重计数，让下次到期还能提醒
        data = _read_state(root)
        if data.get("last_state") != "fresh":
            _write_state({"last_state": "fresh"}, root)
        return result

    if state not in ("expiring", "stale", "missing"):
        result["skipped"] = f"{state}：非提醒状态"
        return result

    # ── 去重：同一状态只发一次 ──
    data = _read_state(root)
    if not force and data.get("last_state") == state \
            and data.get("notified_for") == st.get("refreshed_at"):
        result["skipped"] = f"{state}：本级已提醒过，不重复"
        return result

    cmd = refresh_cmd or str(
        Path(__file__).resolve().parents[2] / "scripts" / "zsxq_authorize.py")
    subject, body = _subject_and_body(st, refresh_cmd=cmd)
    to = _recipient()
    result["to"] = to

    if dry_run:
        print("─" * 62)
        print(f"收件人：{to}")
        print(f"主题　：{subject}")
        print("─" * 62)
        print(body)
        result["notified"] = False
        result["skipped"] = "dry_run：未实际发送"
        return result

    # ── 真发 ──
    from src.core.config import get_settings
    from src.infrastructure.notify import SmtpEmailNotifier

    s = get_settings()
    if not (getattr(s, "alert_smtp_user", "") and
            getattr(s, "alert_smtp_auth_code", "")):
        result["skipped"] = "SMTP 未配置，无法发送（请检查 ALERT_SMTP_*）"
        logger.warning("授权提醒未发送：SMTP 未配置")
        return result

    notifier = SmtpEmailNotifier(
        host=getattr(s, "alert_smtp_host", "smtp.qq.com"),
        port=int(getattr(s, "alert_smtp_port", 465)),
        user=s.alert_smtp_user,
        auth_code=s.alert_smtp_auth_code,
    )
    # 用 `send_template` 风格的直发：这里需要自定义主题与正文，
    # 而内置 SCENE_* 模板是给验证码/告警用的，套不上。
    ok = await _send_custom(notifier, to, subject, body)
    result["notified"] = ok
    if ok:
        _write_state({"last_state": state,
                      "notified_for": st.get("refreshed_at"),
                      "notified_at": datetime.now(timezone.utc).astimezone()
                      .isoformat(timespec="seconds")}, root)
        logger.info("数据源授权提醒已发送（state=%s）", state)
    else:
        result["skipped"] = "发送失败"
    return result


async def _send_custom(notifier: Any, to: str, subject: str,
                       body: str) -> bool:
    """直发自定义主题/正文的邮件。

    内置 `Notifier.send(target, scene, params)` 走**固定模板**（那是有意的：
    验证码邮件要防钓鱼仿冒）。运维提醒的主题与正文是动态的，套不上模板，
    所以这里直接用发送器的底层能力。
    """
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = notifier._user            # noqa: SLF001 同包内协作
    msg["To"] = to

    def _blocking() -> None:
        with smtplib.SMTP_SSL(notifier._host, notifier._port,  # noqa: SLF001
                              timeout=20) as srv:
            srv.login(notifier._user, notifier._auth_code)  # noqa: SLF001
            srv.sendmail(notifier._user, [to], msg.as_string())

    import asyncio

    try:
        await asyncio.to_thread(_blocking)
        return True
    except Exception as exc:  # noqa: BLE001 发送失败不该让调度任务崩
        from src.core.redaction import sanitize_error

        logger.warning("授权提醒发送失败：%s", sanitize_error(exc))
        return False


__all__ = [
    "DEFAULT_ALERT_TO",
    "check_and_notify",
]
