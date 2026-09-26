"""真发一封测试邮件，验证 SMTP 授权码配对了没有。

## 为什么需要这个脚本（而不是"注册一次试试"）

验证码邮件的失败是**静默**的：用户看到的是"没收到邮件"，
而日志里可能只是一行 warning。要区分下面几种情况，必须直接打 SMTP：

| 情况 | QQ 返回 | 说明 |
|---|---|---|
| 授权码错了 / 用的还是登录密码 | `535 Authentication failed` | 最常见。**必须是授权码，不是 QQ 密码** |
| 授权码对了但没开 SMTP 服务 | `550` / `503` | 要在 QQ 邮箱设置里开启 SMTP |
| 端口被封 | 连接超时 | 465（SSL）应可达；公司网络可能封 25 |
| 邮箱地址写错 | `553` | `ALERT_SMTP_USER` 必须是**发件邮箱全称** |

脚本**不打印授权码**，只打印"已配置/长度"这类可安全贴进聊天室的信息。

用法：
    .venv\\Scripts\\python.exe scripts\\send_test_mail.py
    .venv\\Scripts\\python.exe scripts\\send_test_mail.py --to someone@example.com
"""

from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.core.config import get_settings  # noqa: E402
from src.infrastructure.notify import build_notifier  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="发送一封真实的测试邮件")
    parser.add_argument("--to", default="", help="收件人（默认取 ALERT_EMAIL_TO）")
    args = parser.parse_args()

    settings = get_settings()
    user = str(getattr(settings, "alert_smtp_user", "") or "")
    code = str(getattr(settings, "alert_smtp_auth_code", "") or "")
    host = str(getattr(settings, "alert_smtp_host", "") or "")
    port = int(getattr(settings, "alert_smtp_port", 465) or 465)
    target = args.to or str(getattr(settings, "alert_email_to", "") or "")

    print("=" * 68)
    print("SMTP 配置自检")
    print("=" * 68)
    print(f"  环境            : {settings.env}")
    # ⚠️ 只报"有没有 / 多长"，不报内容 —— 授权码等同于密码
    print(f"  发件邮箱        : {user or '(未配置)'}")
    print(f"  授权码          : {'已配置（' + str(len(code)) + ' 位）' if code else '(未配置)'}")
    print(f"  SMTP 服务器     : {host}:{port}")
    print(f"  收件人          : {target or '(未配置)'}")

    notifier = build_notifier(settings)
    print(f"  实际选用通道    : {type(notifier).__name__}"
          f"（provider={getattr(notifier, 'channel', '?')}）")

    problems: list[str] = []
    if not user:
        problems.append("ALERT_SMTP_USER 为空")
    if not code:
        problems.append("ALERT_SMTP_AUTH_CODE 为空 —— 这正是「验证码没有真实发出」的原因")
    if not target:
        problems.append("ALERT_EMAIL_TO 为空（或 --to 未指定）")
    if problems:
        print()
        print("❌ 配置不完整：")
        for p in problems:
            print(f"   · {p}")
        print()
        print("在项目根目录的 .env 里补上（注意：授权码不是 QQ 登录密码）：")
        print("   ALERT_SMTP_USER=你的QQ邮箱@qq.com")
        print("   ALERT_SMTP_AUTH_CODE=16位授权码")
        print("   ALERT_EMAIL_TO=接收验证码的邮箱@qq.com")
        return 2

    if "Console" in type(notifier).__name__:
        print()
        print("⚠️ 当前选中 ConsoleNotifier —— 邮件只会打进日志，不会真发出。")
        print("   检查 MOSS_NOTIFY_CHANNEL 是否被显式设成了 console。")

    print()
    print("正在连接并发信…")
    msg = EmailMessage()
    msg["From"] = f"Moss 投研 <{user}>"
    msg["To"] = target
    msg["Subject"] = "【Moss-FinAgent-Research】SMTP 配置测试"
    msg.set_content(
        "这是一封 SMTP 配置测试邮件。\n"
        "你能看到它，说明发件通道已经打通 —— 注册/找回密码的验证码将真实送达。\n\n"
        "（如果没有收到，请先检查垃圾邮件；QQ 邮箱对同域自发信件通常不会拦截。）")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
            server.login(user, code)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        print()
        print(f"❌ 认证失败：{exc.smtp_code} {exc.smtp_error!r}")
        print("   最常见原因：ALERT_SMTP_AUTH_CODE 填的是 QQ **登录密码**，")
        print("   或授权码已失效（在 QQ 邮箱设置里重新生成一个）。")
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        print()
        print(f"❌ 发送失败：{type(exc).__name__}: {exc}")
        return 1

    print()
    print(f"✅ 已发出（{host}:{port}）。请到 {target} 收信；")
    print("   没看到就先查垃圾邮件箱。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
