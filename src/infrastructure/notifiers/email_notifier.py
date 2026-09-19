"""QQ邮箱SMTP SSL告警邮件通道（FR-11，阻塞IO经to_thread）。

- 发送门槛按告警类型与分数（阈值来自Settings，环境变量可调）：
  风险类 risk_score 严格 > alert_email_risk_min_score（默认>69）；
  机会类 opportunity_score >= alert_email_opp_min_score（默认≥85）；
- 账号/授权码/收件人全部来自环境变量，未配置→unconfigured，不联网不抛错；
- SMTP异常→failed（不影响扫描主流程）；授权码绝不写日志。
"""

from __future__ import annotations

import asyncio
import html
import logging
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from src.core.config import Settings, get_settings
from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.domain.alerts.models import Alert, AlertType, EmailSendResult

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT_S = 20


class EmailNotifier:
    """SMTP SSL 邮件发送器（QQ邮箱465）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    def is_configured(self) -> bool:
        return bool(
            self._s.alert_smtp_user
            and self._s.alert_smtp_auth_code
            and self._s.alert_email_to
        )

    def _email_gate(self, alert: Alert) -> tuple[bool, str]:
        """按类型+分数判定是否发邮件。返回(是否发送, 原因)。"""
        if alert.alert_type == AlertType.OPPORTUNITY:
            threshold = self._s.alert_email_opp_min_score
            if alert.opportunity_score >= threshold:
                return True, ""
            return False, (
                f"机会分{alert.opportunity_score:.0f}未达邮件门槛"
                f"≥{threshold:g}")
        # 默认按风险类处理
        threshold = self._s.alert_email_risk_min_score
        if alert.risk_score > threshold:
            return True, ""
        return False, (
            f"风险分{alert.risk_score:.0f}未达邮件门槛"
            f">{threshold:g}")

    async def send(self, alert: Alert) -> EmailSendResult:
        should_send, reason = self._email_gate(alert)
        if not should_send:
            return EmailSendResult(
                alert_id=alert.alert_id, status="suppressed", detail=reason)
        if not self.is_configured():
            logger.info("邮件通道未配置（ALERT_SMTP_USER/AUTH_CODE缺失），跳过发送")
            return EmailSendResult(
                alert_id=alert.alert_id, status="unconfigured",
                detail="未配置SMTP账号或授权码")
        try:
            detail = await asyncio.to_thread(self._send_sync, alert)
            return EmailSendResult(
                alert_id=alert.alert_id, status="sent", detail=detail)
        except Exception as exc:  # noqa: BLE001 邮件失败不阻断扫描
            logger.warning("告警邮件发送失败 %s: %s",
                           alert.alert_id, brief(exc, BRIEF_DEFAULT))
            return EmailSendResult(
                alert_id=alert.alert_id, status="failed", detail=brief(exc, BRIEF_DEFAULT))

    def _send_sync(self, alert: Alert) -> str:
        message = self._build_message(alert)
        with smtplib.SMTP_SSL(
            self._s.alert_smtp_host, self._s.alert_smtp_port,
            timeout=_SMTP_TIMEOUT_S,
        ) as smtp:
            smtp.login(self._s.alert_smtp_user, self._s.alert_smtp_auth_code)
            smtp.sendmail(
                self._s.alert_smtp_user, [self._s.alert_email_to],
                message.as_string())
        return f"已发送至{self._s.alert_email_to}"

    def _build_message(self, alert: Alert) -> MIMEMultipart:
        direction = "风险告警" if alert.alert_type == AlertType.RISK else "机会提醒"
        subject = (
            f"【Moss{direction}·{alert.alert_level.value}】{alert.title[:40]}")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(
            self._s.alert_email_from_name, "utf-8")), self._s.alert_smtp_user))
        msg["To"] = self._s.alert_email_to
        msg.attach(MIMEText(self._render_text(alert), "plain", "utf-8"))
        msg.attach(MIMEText(self._render_html(alert), "html", "utf-8"))
        return msg

    @staticmethod
    def _stock_lines(alert: Alert) -> list[str]:
        icon = {"positive": "受益", "negative": "受损", "mixed": "分化"}
        return [
            f"  · {s.name}({s.code or '代码待核'})—"
            f"{icon.get(s.impact, s.impact)}—{s.reason}"
            for s in alert.affected_stocks
        ]

    def _render_text(self, a: Alert) -> str:
        lines = [
            f"事件：{a.title}",
            f"方向/级别：{a.alert_type.value} / {a.alert_level.value}",
            f"风险分：{a.risk_score:.0f}  机会分：{a.opportunity_score:.0f}"
            f"  置信度：{a.confidence:.2f}",
            f"事件时间：{a.event_publish_time or '未知'}",
            f"触发时间：{a.trigger_time}",
            f"来源：{a.source_name}",
        ]
        if a.source_url:
            lines.append(f"原文链接：{a.source_url}")
        if a.affected_industries:
            lines.append("相关行业：" + "、".join(a.affected_industries))
        stocks = self._stock_lines(a)
        if stocks:
            lines.append("受影响个股：")
            lines.extend(stocks)
        if a.impact_path:
            lines.append(f"影响路径：{a.impact_path}")
        if a.description:
            lines.append(f"AI摘要：{a.description}")
        lines.append("")
        lines.append(a.disclaimer)
        return "\n".join(lines)

    def _render_html(self, a: Alert) -> str:
        color = "#c0392b" if a.alert_type == AlertType.RISK else "#b8860b"
        # 所有外部文本HTML转义，URL经normalize阶段scheme白名单，href再转义防注入
        title = html.escape(a.title)
        source = html.escape(a.source_name)
        pub_time = html.escape(a.event_publish_time or "未知")
        url = html.escape(a.source_url, quote=True)
        industries = html.escape("、".join(a.affected_industries))
        impact = html.escape(a.impact_path)
        summary = html.escape(a.description)
        stocks = "".join(
            f"<li>{html.escape(s.name)}({html.escape(s.code or '代码待核')})："
            f"{html.escape(s.reason)}</li>"
            for s in a.affected_stocks)
        link = f'<a href="{url}">查看原文</a>' if url else "无"
        return f"""<div style="font-family:'Microsoft YaHei',Arial;
        max-width:680px;margin:auto;color:#222">
  <h3 style="color:{color};border-left:5px solid {color};
  padding-left:10px">{title}</h3>
  <p><b>{a.alert_type.value} / {a.alert_level.value}</b>
  &nbsp;风险分 <b>{a.risk_score:.0f}</b>
  &nbsp;机会分 <b>{a.opportunity_score:.0f}</b>
  &nbsp;置信度 <b>{a.confidence:.2f}</b></p>
  <p>来源：{source}｜事件时间：{pub_time}<br>
  原文：{link}</p>
  {f'<p>相关行业：{industries}</p>' if a.affected_industries else ''}
  {'<ul>' + stocks + '</ul>' if stocks else ''}
  {f'<p>影响路径：{impact}</p>' if impact else ''}
  {f'<p>AI摘要：{summary}</p>' if summary else ''}
  <hr><p style="color:#888;font-size:12px">{html.escape(a.disclaimer)}</p>
</div>"""
