"""主线挖掘：告警推送（钉钉 / 飞书机器人）。

## 为什么 webhook 只从环境变量读

`configs/mainline.yaml` 会进 git。把 webhook 写进 YAML 就等于把凭据提交进
仓库 —— 钉钉/飞书的群机器人 webhook 一旦泄露，任何人都能往群里发消息。
因此 `NotifyConfig` 只暴露两个 `@property` 去读
`MOSS_MAINLINE_DINGTALK_WEBHOOK` / `MOSS_MAINLINE_FEISHU_WEBHOOK`。

## 三个刻意的设计

**1. 推送失败绝不影响打分。** 网络抖动、webhook 被限流、群被解散，
都不该让"今天的主线算不出来"。因此所有推送函数**只返回结果、不抛异常**，
调用方拿到 `{"sent": False, "reason": "..."}` 写进 `AlertSignal.push_note`。

**2. 只在有 webhook 时才尝试。** 没配 webhook 是**正常状态**（本地开发、
开源用户），此时返回"未配置"而不是"失败" —— 把"没配"报成"失败"会让
台账天天报红，真正的失败反而被淹没。

**3. 未配置时把消息写进日志。** 这样本地开发也能看到"如果配了会推什么"，
不必为了调试去申请一个机器人。

## 推送内容口径

只推**等级 ≥ `notify.min_level`** 的告警（默认 medium），并且带上：
板块名、预警分、等级、触发维度、关键理由、以及**免责声明**。
前三条让人决定要不要看，触发维度与理由让人能复核 ——
只推一个分数等于让人盲信。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.config import MainlineConfig, load_config
from src.mainline.models import AlertSignal, FutureAlert, SignalLevel

logger = logging.getLogger(__name__)

#: 单条消息最多带几条告警（钉钉/飞书都有单条消息长度上限，超了会被截断）
MAX_ITEMS = 8


@dataclass
class PushResult:
    """一次推送的结果（**不抛异常**，失败也返回对象）。"""

    sent: bool = False
    channel: str = ""
    reason: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "channel": self.channel,
                "reason": self.reason, "count": self.count}

    @property
    def note(self) -> str:
        if self.sent:
            return f"已推送 {self.count} 条到{self.channel}"
        return f"未推送（{self.reason}）"


def _levels(value: Any) -> int:
    if isinstance(value, SignalLevel):
        return value.rank
    text = str(value or "").strip().lower()
    return {"strong": 3, "medium": 2, "weak": 1}.get(text, 0)


def _post(url: str, payload: dict[str, Any], timeout: float) -> tuple[bool, str]:
    """发一个 JSON POST；返回 `(是否成功, 说明)`。"""
    try:
        import requests

        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        body = {}
        try:
            body = response.json() or {}
        except ValueError:
            body = {}
        # 两家机器人都是 HTTP 200 + body 里带错误码，只看状态码会漏掉失败
        code = body.get("errcode", body.get("code", 0))
        if code not in (0, None, "0"):
            return False, f"接口返回错误码 {code}：{str(body)[:120]}"
        return True, ""
    except Exception as exc:  # noqa: BLE001 网络问题一律降级为"没推成功"
        return False, brief(exc, BRIEF_TIGHT)


def build_alert_text(alerts: Sequence[AlertSignal], *,
                     title: str = "主线挖掘 · 异动告警",
                     disclaimer: str = "") -> str:
    """把告警列表拼成 Markdown 文本（同时适用于钉钉与飞书）。"""
    lines = [f"### {title}", ""]
    for item in alerts[:MAX_ITEMS]:
        dims = "、".join(item.triggered_dims) or "—"
        lines.append(f"**{item.board_name}**　{item.level.label}　"
                     f"评分 {item.score:.1f}")
        lines.append(f"- 日期：{item.trade_date}　"
                     f"当日涨跌：{_pct(item.change_pct)}")
        lines.append(f"- 触发维度：{dims}")
        if item.gate_bonus:
            lines.append(f"- 龙头共振加分：+{item.gate_bonus:.1f}")
        if item.reasons:
            lines.append(f"- 理由：{'；'.join(item.reasons[:3])}")
        lines.append("")
    if len(alerts) > MAX_ITEMS:
        lines.append(f"（另有 {len(alerts) - MAX_ITEMS} 条未展示）")
        lines.append("")
    if disclaimer:
        lines.append(f"> {disclaimer}")
    return "\n".join(lines)


def build_futures_text(alerts: Sequence[FutureAlert], *,
                       disclaimer: str = "") -> str:
    lines = ["### 主线挖掘 · 期货先行信号", ""]
    for item in alerts[:MAX_ITEMS]:
        lines.append(f"**{item.name}**　{item.level.label}　{item.title}")
        lines.append(f"- 日期：{item.date}　对应板块："
                     f"{'、'.join(item.boards) or '—'}")
        if item.detail:
            lines.append(f"- 依据：{item.detail}")
        lines.append("")
    if len(alerts) > MAX_ITEMS:
        lines.append(f"（另有 {len(alerts) - MAX_ITEMS} 条未展示）")
        lines.append("")
    if disclaimer:
        lines.append(f"> {disclaimer}")
    return "\n".join(lines)


def push_text(text: str, config: MainlineConfig | None = None, *,
              title: str = "主线挖掘") -> list[PushResult]:
    """把一段 Markdown 推到所有已配置的渠道（未配置则只记日志）。"""
    cfg = config or load_config()
    notify = cfg.notify
    out: list[PushResult] = []
    if not notify.enabled:
        logger.info("主线挖掘推送已关闭；消息内容：\n%s", text[:1200])
        return [PushResult(sent=False, channel="none", reason="推送已关闭")]
    ding = notify.dingtalk_webhook
    feishu = notify.feishu_webhook
    if not ding and not feishu:
        # 没配 webhook 是**正常状态**，不是失败（见模块文档）
        logger.info("未配置主线挖掘 webhook，消息仅记日志：\n%s", text[:1200])
        return [PushResult(sent=False, channel="none", reason="未配置 webhook")]
    if ding:
        ok, reason = _post(ding, {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text}}, notify.timeout)
        out.append(PushResult(sent=ok, channel="钉钉",
                              reason="" if ok else reason))
    if feishu:
        ok, reason = _post(feishu, {
            "msg_type": "text", "content": {"text": text}}, notify.timeout)
        out.append(PushResult(sent=ok, channel="飞书",
                              reason="" if ok else reason))
    return out


def push_alerts(alerts: Iterable[AlertSignal],
                config: MainlineConfig | None = None) -> list[PushResult]:
    """推送主线告警（按 `notify.min_level` 过滤），并回填 `push_note`。"""
    cfg = config or load_config()
    items = [item for item in alerts
             if _levels(item.level) >= _levels(cfg.notify.min_level)]
    if not items:
        return [PushResult(sent=False, channel="none",
                           reason=f"没有 ≥ {cfg.notify.min_level} 的告警")]
    text = build_alert_text(items, disclaimer=cfg.disclaimer)
    results = push_text(text, cfg)
    note = "；".join(result.note for result in results)
    for item in items:
        item.pushed = any(result.sent for result in results)
        item.push_note = note
    return results


def push_futures(alerts: Iterable[FutureAlert],
                 config: MainlineConfig | None = None) -> list[PushResult]:
    """推送期货先行信号告警。"""
    cfg = config or load_config()
    items = [item for item in alerts
             if _levels(item.level) >= _levels(cfg.notify.min_level)]
    if not items:
        return [PushResult(sent=False, channel="none",
                           reason=f"没有 ≥ {cfg.notify.min_level} 的期货告警")]
    return push_text(build_futures_text(items, disclaimer=cfg.disclaimer), cfg,
                     title="期货先行信号")


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "无数据"


__all__ = [
    "MAX_ITEMS",
    "PushResult",
    "build_alert_text",
    "build_futures_text",
    "push_alerts",
    "push_futures",
    "push_text",
]
