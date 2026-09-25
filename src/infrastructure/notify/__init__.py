"""通知通道抽象：邮箱（默认）/ 控制台（仅非生产）/ 内存（单测）。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.8。

## 本轮口径（🔒 2026-09 已定）

**只用邮箱，短信能力后置。** 所以：
- `channel` 只有 `email` 会被真实发出；`sms` 的选择逻辑**保留但不可用**
  （将来接入时不改表、不改流程，见 `build_notifier` 的说明）；
- 手机号当期是"可选联系方式"，**不参与验证与找回**。

## 为什么必须是"可切换的策略"而不是硬编码 SMTP

三条理由，都是实测/合规驱动：
1. **dev 要能不开 SMTP 就跑通**：本地没有授权码时，验证码直接打日志
   （`ConsoleNotifier`）。**但它绝不允许出现在生产** ——
   生产把验证码打进日志 = 直接泄露。`§8.7.2` 的启动自检会拦住它。
2. **单测要能断言"发了什么"**：`MemoryNotifier` 把验证码留在内存里，
   测试不必去解析日志文本（那种断言既脆又难读）。
3. **将来加短信要"零改动"**：所有调用方只认 `Notifier.channel` 与
   `send(target, template, params)`，加一个实现类即可。

## 与中国大陆短信的现实

国内短信**没有免费路径**：阿里云现行文档明确"仅企业资质才可以进行报备和发送短信"，
且**签名实名报备要 5~10 个工作日**（运营商未承诺时效）——
这是"上线阻塞"级别的事情，所以本轮先用邮箱把关键路径让开。
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: 场景名（与 `fact_verification_code.scene` 同口径）
SCENE_REGISTER = "register"
SCENE_RESET = "reset_password"
SCENE_BIND = "bind_contact"
SCENE_CHANGE_PHONE = "change_phone"
SCENE_LOGIN_ALERT = "login_alert"

#: 场景 → 邮件主题与文案模板。
#: 用**固定模板**而不是自由拼接：验证码邮件是钓鱼仿冒的重灾区，
#: 模板固定能让用户一眼分辨"这是官方邮件"。
_TEMPLATES: dict[str, tuple[str, str]] = {
    SCENE_REGISTER: (
        "【{app}】注册验证码",
        "你的注册验证码是 {code}，{minutes} 分钟内有效。\n"
        "如非本人操作请忽略本邮件。"),
    SCENE_RESET: (
        "【{app}】密码重置验证码",
        "你正在重置登录密码，验证码 {code}，{minutes} 分钟内有效。\n"
        "\n"
        "重置令牌：{reset_token}\n"
        "\n"
        "请在重置页面**同时填入验证码与重置令牌**（两个都要，缺一不可）。\n"
        "如非本人操作，请立即修改密码并联系管理员。"),
    SCENE_BIND: (
        "【{app}】联系方式绑定验证码",
        "绑定验证码 {code}，{minutes} 分钟内有效。"),
    SCENE_CHANGE_PHONE: (
        "【{app}】联系方式变更验证码",
        "换绑验证码 {code}，{minutes} 分钟内有效。\n"
        "如非本人操作，请立即联系管理员——这意味着有人在尝试接管你的账号。"),
    SCENE_LOGIN_ALERT: (
        "【{app}】安全提醒：你的密码已重置",
        "你的账号密码刚刚被重置，所有设备已退出登录。\n"
        "如非本人操作，请立即联系管理员。时间：{when}"),
}


@dataclass
class NotifyResult:
    """一次外发的结果。`ok=False` 时 **必须** 带上可给用户看的原因。"""

    ok: bool
    channel: str
    provider: str = ""
    provider_msg_id: str = ""
    status: str = "sent"          # sent | failed | quota_exceeded | rate_limited
    error_code: str = ""
    error_msg: str = ""


@runtime_checkable
class Notifier(Protocol):
    """通道契约。所有实现只做"发出去"这一件事，**不做配额/限流判定**。

    配额与限流属于业务策略（在服务层），放进来会让每个实现都要重复一遍。
    """

    channel: str

    async def send(self, target: str, scene: str, params: dict[str, Any]) -> NotifyResult:
        ...


def render(scene: str, params: dict[str, Any], *, app_name: str = "Moss 投研") -> tuple[str, str]:
    """渲染 `(主题, 正文)`。未知场景返回通用模板（**不抛**：宁可发一封泛化邮件，
    也不能因为模板缺失让用户拿不到验证码）。"""
    subject, body = _TEMPLATES.get(
        scene, ("【{app}】验证码", "你的验证码是 {code}，{minutes} 分钟内有效。"))
    payload = {"app": app_name,
               "minutes": int(params.get("ttl_seconds", 300) // 60) or 5,
               **params}
    try:
        return subject.format(**payload), body.format(**payload)
    except KeyError as exc:  # 模板变量缺失：补空而不是 500
        logger.warning("通知模板缺变量 %s（scene=%s），按空值渲染", exc, scene)
        # ★ 白名单必须与 `_TEMPLATES` 里实际用到的占位符**保持同步**。
        # 漏一个的后果不是"少一句话"，而是：这里补空之后会**再 format 一次**，
        # 那个仍缺失的 key 会二次抛 KeyError 并逃出本函数 —— 于是所有落到
        # 这条兜底路径的场景**整封邮件发不出去**。
        # （实测：给 SCENE_RESET 加 `{reset_token}` 时，若不同步这里，
        #  注册/绑定等场景的模板渲染会连带失败。）
        safe = {k: "" for k in ("code", "app", "minutes", "when", "reset_token")}
        safe.update({k: str(v) for k, v in payload.items()})
        return subject.format(**safe), body.format(**safe)


class ConsoleNotifier:
    """仅非生产：把验证码打到日志（**生产禁用，自检会拦**）。

    为什么需要它：本地没配 SMTP 授权码时，注册/找回仍要能端到端跑通。
    """

    channel = "email"

    def __init__(self, *, app_name: str = "Moss 投研") -> None:
        self._app_name = app_name

    async def send(self, target: str, scene: str, params: dict[str, Any]) -> NotifyResult:
        subject, body = render(scene, params, app_name=self._app_name)
        # 只打验证码本身，不打完整正文 —— 减少日志里的敏感面
        logger.warning("[DEV 通知] to=%s scene=%s subject=%s code=%s",
                       target, scene, subject, params.get("code", "-"))
        print(f"\n===== DEV 邮件（未真实发送）=====\n"
              f"To: {target}\nSubject: {subject}\n{body}\n"
              f"================================\n", flush=True)
        return NotifyResult(ok=True, channel=self.channel, provider="console",
                            provider_msg_id=f"console-{scene}")


class MemoryNotifier:
    """单测用：把外发内容留在内存里供断言，**不发送、不阻塞**。"""

    channel = "email"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self._fail = fail

    async def send(self, target: str, scene: str, params: dict[str, Any]) -> NotifyResult:
        record = {"target": target, "scene": scene, "params": dict(params)}
        self.sent.append(record)
        if self._fail:
            return NotifyResult(ok=False, channel=self.channel, provider="memory",
                                status="failed", error_code="MEMORY_FAIL",
                                error_msg="MemoryNotifier 被要求失败（测试用）")
        return NotifyResult(ok=True, channel=self.channel, provider="memory",
                            provider_msg_id=f"mem-{len(self.sent)}")

    # ---- 断言便利 ----
    def last_code(self) -> str:
        return str(self.sent[-1]["params"].get("code", "")) if self.sent else ""

    def count(self) -> int:
        return len(self.sent)


class SmtpEmailNotifier:
    """真实邮件通道（默认生产通道）。

    复用项目既有的 SMTP 配置（`alert_smtp_*`）—— 不新增一套凭据：
    **少一个要维护的秘密就少一处泄漏面**。

    ⚠️ 唯一的坑不是代码，是**可达性**：必须配好 SPF / DKIM / DMARC，
    否则验证码会进垃圾箱。短信收不到用户会重试；**邮件进了垃圾箱用户根本不知道**。
    所以前端要提示"若未收到请检查垃圾邮件"（§8.6.5⑥）。
    """

    channel = "email"

    def __init__(
        self, *, host: str, port: int, user: str, auth_code: str,
        sender_name: str = "Moss 投研", app_name: str = "Moss 投研",
        timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._user = user
        self._auth_code = auth_code
        self._sender_name = sender_name
        self._app_name = app_name
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._host and self._user and self._auth_code)

    def _send_sync(self, target: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = f"{self._sender_name} <{self._user}>"
        message["To"] = target
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout,
                              context=context) as server:
            server.login(self._user, self._auth_code)
            server.send_message(message)

    async def send(self, target: str, scene: str, params: dict[str, Any]) -> NotifyResult:
        if not self.configured:
            # 明确失败，**不假装成功**：用户会一直等一封永远不来的邮件
            return NotifyResult(
                ok=False, channel=self.channel, provider="smtp", status="failed",
                error_code="SMTP_NOT_CONFIGURED",
                error_msg="邮件通道未配置（缺少 SMTP 账号或授权码），请联系管理员")
        subject, body = render(scene, params, app_name=self._app_name)
        try:
            await asyncio.to_thread(self._send_sync, target, subject, body)
        except Exception as exc:  # noqa: BLE001 外发失败绝不冒泡成 500
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("邮件发送失败 to=%s scene=%s：%s", target, scene, detail[:200])
            return NotifyResult(ok=False, channel=self.channel, provider="smtp",
                                status="failed", error_code="SMTP_ERROR",
                                error_msg="邮件发送失败，请稍后重试或联系管理员")
        return NotifyResult(ok=True, channel=self.channel, provider="smtp",
                            provider_msg_id=f"smtp-{scene}")


def build_notifier(settings: Any, *, explicitly: str = "") -> Notifier:
    """按配置造通道。

    选择顺序（`MOSS_NOTIFY_CHANNEL` 可显式指定）：
      1. 显式 `console`  → ConsoleNotifier（**仅非生产可用**，生产被自检拦）
      2. 有 SMTP 凭据     → SmtpEmailNotifier（默认路径）
      3. 都没有           → ConsoleNotifier（dev 便利），但在生产会**自检失败**

    ⚠️ **将来加短信只改这一个函数**：加一个 `sms` 分支即可，
    调用方（服务层）不需要知道通道细节。这正是"可插拔"的落点。
    """
    want = str(explicitly or os.environ.get("MOSS_NOTIFY_CHANNEL", "")).strip().lower()
    if want in {"console", "stdout", "print"}:
        return ConsoleNotifier(app_name=getattr(settings, "app_name", "Moss 投研"))

    host = str(getattr(settings, "alert_smtp_host", "") or "")
    port = int(getattr(settings, "alert_smtp_port", 465) or 465)
    user = str(getattr(settings, "alert_smtp_user", "") or "")
    code = str(getattr(settings, "alert_smtp_auth_code", "") or "")
    if host and user and code:
        return SmtpEmailNotifier(
            host=host, port=port, user=user, auth_code=code,
            app_name=getattr(settings, "app_name", "Moss 投研"))
    if want in {"smtp", "email"}:
        # 显式要邮件却没凭据：**不要静默退回 console**，否则生产会悄悄把验证码打日志
        return SmtpEmailNotifier(host=host, port=port, user=user, auth_code=code)
    logger.info("未配置 SMTP，且未显式指定通道 → 使用 ConsoleNotifier（仅适合 dev）")
    return ConsoleNotifier(app_name=getattr(settings, "app_name", "Moss 投研"))


__all__ = [
    "SCENE_BIND",
    "SCENE_CHANGE_PHONE",
    "SCENE_LOGIN_ALERT",
    "SCENE_REGISTER",
    "SCENE_RESET",
    "ConsoleNotifier",
    "MemoryNotifier",
    "NotifyResult",
    "Notifier",
    "SmtpEmailNotifier",
    "build_notifier",
    "render",
]
