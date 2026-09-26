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
from datetime import datetime
from pathlib import Path
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
    "forced_exit": "止损提示",
    "none": "无信号",
}
_KIND_LABEL = {
    "low_buy": "回踩区间提示", "high_sell": "冲高区间提示",
    "stop_loss": "止损", "none": "无",
}


class SignalNotifier:
    """做T信号多渠道推送（含当日去重 + 冷却）。"""

    def __init__(self, config: IntradayConfig, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None,
                 dedup_path: str | Path | None = None) -> None:
        self._config = config
        self._settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._last_push: dict[str, float] = {}
        # ⚠️ 冷却的**主判据**是落盘的 `notify_dedup`（跨进程重启有效）。
        # 这个内存字典现在只作兜底：`daily_cap_enabled=false`（排查用的
        # 总开关）时它仍在生效，另外供 `channel_status()` 之类做快速自查。
        # 不要再拿它当唯一的冷却依据 —— 进程一重启它就空了，
        # 而那正是 2026-09-25 刷屏事故的成因之一。
        # 冷却键需要标的代码；push() 前由调用方或 push() 自身设置
        self._pending_code: str = ""
        # 当日去重记录的落盘位置。`None` = 用 `notify_dedup.DEFAULT_PATH`
        # （`data/cache/intraday/notified_signals.json`）。测试注入临时目录，
        # 免得污染仓库里的真实记录。
        self._dedup_path = dedup_path
        # 本进程内最后一次推送用的交易日，供 `push()` 记账用
        self._pending_trade_date: str = ""

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

    def should_push(self, signal: TradeSignal, *, now: float | None = None,
                    snapshot: IntradaySnapshot | None = None,
                    trade_date: str = "") -> tuple[bool, str]:
        """是否需要推送该信号。

        判定顺序（从"最硬的判据"到"最软的节流"）：

        1. 推送开关 / 有无信号 / 只推正式信号  —— 配置门槛；
        2. **数据新鲜度闸门** —— 数据非当日绝不推（见下）；
        3. ★ **当日去重闸门** —— 同一天同一只票同一方向**只通知一次**；
        4. 冷却窗口 —— 最后一道节流（同一方向 30 分钟内不重复）。

        ## 为什么必须有"数据新鲜度"闸门（2026-09-25 事故）

        中秋节（9/25，周五休市）当天用户持续收到做T提醒邮件。根因不是"发送滞后"，
        而是**假期根本没有当日分时**，数据链回落到上一交易日（9/24）的**完整**分时，
        于是快照照常打分、引擎照常在这份收盘数据上算出正式信号 ——
        同一个输入必然算出同一个输出，9/24 收盘那 4 个信号被一轮轮重算、重发。

        `service.snapshot()` 其实**已经正确识别**了这种情况（`stale = trade_date !=
        今天`，节假日必然为真），但它只把"当前非当日行情"写进 `health.gaps` 当
        UI 提示，**没有拦推送**。信号的时间戳还明明白白写着 `2026-09-24 15:00`。

        所以闸门加在这里：**数据不是当日的 → 绝不推送**。这不是限流（冷却窗口是
        限流，只能让重复变慢），而是"这份信号属于哪一天"的归属判定 ——
        昨日的信号今天推给用户，会让他对着一个已经不存在的价位做决策。

        ## ★ 为什么还要"当日节流"（2026-09-25 用户第二次报障，口径收敛）

        > "昨天盘中的邮件提醒也是做T辅助重复刷屏，要修改成个股股价触及当日
        >   冲高线或回踩线才通知，不是一直循环刷信息。"
        > "可以设置成发出一次通知就静默半小时，半小时后再检测，
        >   一天最多单个票发4次。"

        引擎的"触及"是**状态**不是**事件**：只要价格停在回踩/冲高带内
        （`engine.decide_signal` 的价格带判据），自选池每分钟重算就重新
        `triggered=True` 一次。原来的冷却窗口只活在进程内存里，重启即清空，
        所以同一只票在线上趴一上午能刷出十几封一样的邮件。

        现在的两道闸门（都落盘、跨重启有效）：
          1. **冷却窗口** —— 通知后静默 `notify.cooldown_minutes`（默认 30 分钟）；
          2. **每票每日上限** —— `notify.daily_max_per_code`（默认 4 次，按票不按方向）。
        细节与理由见 `notify_dedup` 模块。
        """
        from src.intraday import notify_dedup

        if not self._config.notify.enabled:
            return False, "推送开关已关闭（notify.enabled=false）"
        if not signal.triggered:
            return False, "无信号"
        if self._config.notify.push_solid_only and signal.strength not in (
                "solid", "forced_exit"):
            return False, "非正式信号（提示线级别），仅站内展示不推送"
        if snapshot is not None:
            blocked, reason = self._stale_guard(snapshot, signal)
            if blocked:
                return False, reason
        # ---- 当日节流：冷却窗口 + 每票每日上限（用户口径 2026-09-25）----
        #
        # > "可以设置成发出一次通知就静默半小时，半小时后再检测，
        # >   一天最多单个票发4次。"
        #
        # 上限按**票**计（不分方向）：一只票"回踩 4 次 + 冲高 4 次"不该是 8 封。
        # 细节与理由见 `notify_dedup` 模块。
        day = trade_date or str(signal.ts or "")[:10]
        notify_cfg = self._config.notify
        use_daily = bool(getattr(notify_cfg, "daily_cap_enabled", True))
        if day and signal.kind != "none" and use_daily:
            decision = notify_dedup.check(
                trade_date=day, code=self._pending_code, kind=signal.kind,
                price=signal.price,
                cooldown_minutes=float(
                    getattr(notify_cfg, "cooldown_minutes", 0) or 0),
                daily_max=int(getattr(notify_cfg, "daily_max_per_code",
                                     notify_dedup.DEFAULT_DAILY_MAX) or 0),
                rearm_pct=self._rearm_pct(),
                path=self._dedup_path)
            if not decision:
                return False, decision.reason
        return True, ""

    def _rearm_pct(self) -> float:
        """重新武装阈值（%）—— 价格离开上次通知价多远才算新的一次触及。

        `0`（默认）= 不重新武装：行为严格等于"冷却 + 每日上限"。
        """
        from src.intraday import notify_dedup

        value = getattr(self._config.levels, "notify_rearm_pct", None)
        try:
            pct = float(value) if value is not None else notify_dedup.DEFAULT_REARM_PCT
        except (TypeError, ValueError):
            return notify_dedup.DEFAULT_REARM_PCT
        return pct if pct > 0 else notify_dedup.DEFAULT_REARM_PCT

    @staticmethod
    def _stale_guard(snapshot: IntradaySnapshot,
                     signal: TradeSignal) -> tuple[bool, str]:
        """数据不是当日（节假日/盘前/行情源未更新）→ (是否拦截, 人话原因)。

        两个判据都用，缺一不可：

        1. `health.stale`：`service.snapshot()` 已经算好的权威标记
           （`trade_date != 今天`），节假日必然为真；
        2. `signal.ts` 的日期：`stale` 万一因为快照来自旧版缓存/字段缺失为假，
           信号自己的时间戳仍然说得出它属于哪一天 —— 双保险。

        原因文案带上**信号所属日期**，用户一眼能看出"这封邮件讲的是昨天的行情"，
        而不是把它当成今天的信号。
        """
        day = ""
        if snapshot.health is not None:
            day = str(snapshot.health.trade_date or snapshot.trade_date or "")
        day = day or str(snapshot.trade_date or "")
        stamp = str(signal.ts or "")
        signal_day = stamp[:10] if len(stamp) >= 10 else ""
        today = datetime.now().strftime("%Y-%m-%d")
        stale = bool(snapshot.health is not None and snapshot.health.stale)
        # 信号时间戳与"今天"不一致同样是硬证据（`stale` 字段缺失时的兜底）
        stale = stale or bool(signal_day and signal_day != today)
        if not stale:
            return False, ""
        shown = signal_day or day or "未知日期"
        return True, (f"数据非当日（信号属于 {shown}，今天 {today}）—— "
                      "节假日/盘前只有上一交易日的行情，昨日信号不重复推送")

    def _cooldown_key(self, signal: TradeSignal) -> str:
        return f"{self._pending_code}:{signal.kind}:{signal.strength}"

    def mark_pushed(self, signal: TradeSignal, *, now: float | None = None) -> None:
        self._last_push[self._cooldown_key(signal)] = (
            time.monotonic() if now is None else now)

    # ---------- 发送 ----------

    async def push(self, snapshot: IntradaySnapshot,
                   signal: TradeSignal, *, trade_date: str = "") -> tuple[list[NotifyResult], bool]:
        """推送信号；返回 (结果列表, 是否实际发出)。

        `trade_date` 是**行情所属交易日**（不是本机日期）—— 当日去重按它分桶，
        所以节假日/盘前会自然落在上一交易日那一桶里，不会误开新桶。
        不传则回落到信号时间戳的日期部分。
        """
        self._pending_code = snapshot.code
        day = trade_date or str(snapshot.trade_date or "") or str(signal.ts or "")[:10]
        should, reason = self.should_push(signal, snapshot=snapshot, trade_date=day)
        if not should:
            return [NotifyResult(channel="all", status="suppressed", detail=reason)], False
        title, text = self.render(snapshot, signal)
        results = await self._dispatch(title, text)
        if any(result.status == "sent" for result in results):
            self.mark_pushed(signal)
            # ★ 记账：**只有真发出去了**才记"今天已通知"。
            # 全渠道失败时不能记 —— 否则用户一封信都没收到，系统却认为
            # 已经通知过，此后一整天都不再尝试（静默失效）。
            self._mark_today_notified(day, signal)
            return results, True
        return results, False

    def _mark_today_notified(self, trade_date: str, signal: TradeSignal) -> None:
        """把"今天这个方向已通知"落盘（供跨进程重启后继续去重）。"""
        day = trade_date or str(signal.ts or "")[:10]
        if not day or signal.kind == "none":
            return
        from src.intraday import notify_dedup

        notify_dedup.mark_notified(
            trade_date=day, code=self._pending_code, kind=signal.kind,
            price=signal.price, path=self._dedup_path)

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
                f"档位：回踩 {levels.low_buy:.2f} / 冲高 {levels.high_sell:.2f} / "
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
