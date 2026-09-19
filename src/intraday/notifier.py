"""做T信号推送（飞书 / 钉钉 / 企业微信 群机器人 + 邮件兜底）。

设计要点：
- **只推送正式信号**（实心三角）与止损警告：提示线（空心三角）留在界面里，
  避免把「观察级」噪音推到手机上（可由 notify.push_solid_only 关闭）；
- 同标的同方向信号有冷却窗口（默认30分钟），防止盘中反复触发刷屏；
- Webhook 地址一律经环境变量注入（configs/intraday.yaml 只写变量名，不落密钥）；
  正文中永不回显 Webhook 地址；
- 三种群机器人全部支持，配置哪个用哪个；都未配置时回落邮件通道；
  全部不可用则返回 unconfigured（站内 WebSocket 推送不受影响）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from src.core.config import Settings, get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.intraday.config import IntradayConfig
from src.intraday.models import IntradaySnapshot, NotifyResult, TradeSignal

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_STRENGTH_LABEL = {
    "solid": "正式信号（实心三角）",
    "hollow": "软提示（空心三角）",
    "forced_exit": "强制卖出警告",
    "none": "无信号",
}
_KIND_LABEL = {
    "low_buy": "低吸做T", "high_sell": "高抛做T",
    "stop_loss": "止损", "none": "无",
}


class SignalNotifier:
    """做T信号多渠道推送（含冷却去重）。"""

    def __init__(self, config: IntradayConfig, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._last_push: dict[str, float] = {}
        # 冷却键需要标的代码；push() 前由调用方或 push() 自身设置
        self._pending_code: str = ""

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- 渠道状态 ----------

    def channel_status(self) -> dict[str, Any]:
        """当前可用推送渠道（供前端展示；绝不回显Webhook地址）。"""
        notify = self._config.notify
        channels = []
        for label, env_name in (
            ("飞书", notify.feishu_webhook_env),
            ("钉钉", notify.dingtalk_webhook_env),
            ("企业微信", notify.wecom_webhook_env),
        ):
            channels.append({
                "name": label, "env": env_name,
                "configured": bool(_env(env_name)),
            })
        return {
            "enabled": notify.enabled,
            "push_solid_only": notify.push_solid_only,
            "cooldown_minutes": notify.cooldown_minutes,
            "channels": channels,
            "email_fallback": notify.email_fallback,
            "email_configured": self._settings.alert_email_enabled,
        }

    # ---------- 推送判定 ----------

    def should_push(self, signal: TradeSignal, *, now: float | None = None) -> tuple[bool, str]:
        """是否需要推送该信号（正式信号门槛 + 冷却窗口）。"""
        if not self._config.notify.enabled:
            return False, "推送开关已关闭（notify.enabled=false）"
        if not signal.triggered:
            return False, "无信号"
        if self._config.notify.push_solid_only and signal.strength not in (
                "solid", "forced_exit"):
            return False, "非正式信号（提示线级别），仅站内展示不推送"
        stamp = time.monotonic() if now is None else now
        cooldown = self._config.notify.cooldown_minutes * 60
        if cooldown > 0:
            key = self._cooldown_key(signal)
            last = self._last_push.get(key)
            if last is not None and stamp - last < cooldown:
                remain = int((cooldown - (stamp - last)) / 60) + 1
                return False, f"冷却中（同标的同方向信号 {remain} 分钟内已推送过）"
        return True, ""

    def _cooldown_key(self, signal: TradeSignal) -> str:
        return f"{self._pending_code}:{signal.kind}:{signal.strength}"

    def mark_pushed(self, signal: TradeSignal, *, now: float | None = None) -> None:
        self._last_push[self._cooldown_key(signal)] = (
            time.monotonic() if now is None else now)

    # ---------- 发送 ----------

    async def push(self, snapshot: IntradaySnapshot,
                   signal: TradeSignal) -> tuple[list[NotifyResult], bool]:
        """推送信号；返回 (结果列表, 是否实际发出)。"""
        self._pending_code = snapshot.code
        should, reason = self.should_push(signal)
        if not should:
            return [NotifyResult(channel="all", status="suppressed", detail=reason)], False
        title, text = self.render(snapshot, signal)
        results = await self._dispatch(title, text)
        if any(result.status == "sent" for result in results):
            self.mark_pushed(signal)
            return results, True
        return results, False

    async def _dispatch(self, title: str, text: str) -> list[NotifyResult]:
        notify = self._config.notify
        results: list[NotifyResult] = []
        for channel, env_name in (
            ("feishu", notify.feishu_webhook_env),
            ("dingtalk", notify.dingtalk_webhook_env),
            ("wecom", notify.wecom_webhook_env),
        ):
            url = _env(env_name)
            if not url:
                results.append(NotifyResult(
                    channel=channel, status="unconfigured",
                    detail=f"未配置环境变量 {env_name}"))
                continue
            try:
                await self._post_webhook(channel, url, title, text)
                results.append(NotifyResult(
                    channel=channel, status="sent", detail="群机器人已送达"))
            except Exception as exc:  # noqa: BLE001 单个渠道失败不影响其它渠道
                logger.warning("做T推送失败 %s: %s", channel, brief(exc, BRIEF_DEFAULT))
                results.append(NotifyResult(
                    channel=channel, status="failed", detail=brief(exc, BRIEF_DEFAULT)))
        if not any(result.status == "sent" for result in results) and notify.email_fallback:
            results.append(await self._send_email(title, text))
        return results

    async def _post_webhook(self, channel: str, url: str,
                            title: str, text: str) -> None:
        client = await self._http()
        if channel == "feishu":
            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": title},
                        "template": "red",
                    },
                    "elements": [{"tag": "div", "text": {
                        "tag": "lark_md", "content": text}}],
                },
            }
        elif channel == "dingtalk":
            payload = {"msgtype": "markdown",
                       "markdown": {"title": title, "text": text}}
        else:  # wecom
            payload = {"msgtype": "markdown",
                       "markdown": {"content": f"**{title}**\n{text}"}}
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        # 群机器人常以 HTTP 200 + errcode 返回业务错误，需要额外校验
        try:
            body = resp.json()
        except ValueError:
            return
        code = body.get("code", body.get("errcode", 0))
        if code not in (0, None):
            raise RuntimeError(f"机器人返回错误码 {code}: {str(body)[:120]}")

    async def _send_email(self, title: str, text: str) -> NotifyResult:
        settings = self._settings
        if not settings.alert_email_enabled:
            return NotifyResult(
                channel="email", status="unconfigured",
                detail="邮件通道未配置（ALERT_SMTP_USER/AUTH_CODE）")
        try:
            await asyncio.to_thread(self._send_email_sync, title, text)
            return NotifyResult(
                channel="email", status="sent",
                detail=f"已发送至{settings.alert_email_to}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("做T邮件推送失败: %s", brief(exc, BRIEF_DEFAULT))
            return NotifyResult(
                channel="email", status="failed", detail=brief(exc, BRIEF_DEFAULT))

    def _send_email_sync(self, title: str, text: str) -> None:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText
        from email.utils import formataddr

        settings = self._settings
        message = MIMEText(text, "plain", "utf-8")
        message["Subject"] = Header(title, "utf-8")
        message["From"] = formataddr(
            (str(Header("Moss做T辅助", "utf-8")), settings.alert_smtp_user))
        message["To"] = settings.alert_email_to
        with smtplib.SMTP_SSL(
                settings.alert_smtp_host, settings.alert_smtp_port,
                timeout=20) as smtp:
            smtp.login(settings.alert_smtp_user, settings.alert_smtp_auth_code)
            smtp.sendmail(
                settings.alert_smtp_user, [settings.alert_email_to],
                message.as_string())

    # ---------- 文案 ----------

    @staticmethod
    def render(snapshot: IntradaySnapshot,
               signal: TradeSignal) -> tuple[str, str]:
        """渲染推送标题与正文（Markdown，飞书/钉钉/企业微信通用）。"""
        scorecard = snapshot.scorecard
        levels = snapshot.levels
        quote = snapshot.quote
        arrow = {"low_buy": "🟢", "high_sell": "🔴", "stop_loss": "⛔"}.get(
            signal.kind, "⚪")
        title = (
            f"{arrow} {snapshot.name or snapshot.code} "
            f"{_KIND_LABEL.get(signal.kind, signal.kind)}"
            f"（总分{signal.total_score:+.1f}）")
        lines = [
            f"**{_KIND_LABEL.get(signal.kind, signal.kind)}** · "
            f"{_STRENGTH_LABEL.get(signal.strength, signal.strength)}",
            f"标的：{snapshot.name or ''}({snapshot.code})",
            f"现价：{signal.price:.2f}"
            + (f"（{quote.change_pct:+.2f}%）" if quote and quote.change_pct is not None
               else ""),
            f"总分：{signal.total_score:+.1f}"
            + (f"（动手线±{scorecard.threshold_action:g} / "
               f"提示线±{scorecard.threshold_hint:g}）" if scorecard else ""),
        ]
        if levels is not None:
            lines.append(
                f"档位：低吸 {levels.low_buy:.2f} / 高抛 {levels.high_sell:.2f} / "
                f"止损 {levels.stop_loss:.2f}")
        lines.append(f"依据：{signal.reason}")
        if scorecard is not None:
            top = sorted(
                (f for f in scorecard.factors if f.available),
                key=lambda f: abs(f.contribution), reverse=True)[:4]
            lines.append("主导因子：" + "；".join(
                f"{f.label} {f.score:+.2f}×{f.weight:g}={f.contribution:+.1f}"
                for f in top))
        lines.append(f"时间：{signal.ts}")
        lines.append(f"_{snapshot.disclaimer}_")
        return title, "\n".join(lines)


def _env(name: str) -> str:
    """读取环境变量（空字符串视为未配置）。"""
    import os

    return (os.environ.get(name) or "").strip()
