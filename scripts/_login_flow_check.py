"""确定性复现用户路径：连续登录失败 → 服务端要求图形码 → 前端必须渲染出来。

## 为什么用「不存在的账号」

`AuthService.login` 有**按账号锁定**。用一个不存在的账号名反复试，
只会累计 **IP 级失败数**（触发图形码），不会锁掉任何真实账号 ——
拿 admin 去试会有把管理员账号锁住的风险。

## 这条路径正是用户报障的场景

用户看到的红字「为确认是你本人操作，请先完成图形验证码」就来自
`/auth/login` 的 400 `captcha_required`。修复前前端**永远不渲染**
图形码输入框（判据写成了 `/captcha_required/.test(String(e))`，
而 `ApiError` 的中文 message 里根本没有这个码）。

只跑在 `C:\\veighna_studio\\python.exe` 下。
"""
import json

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
#: 试验账号：**故意不存在**，避免锁掉真实账号
PROBE_USER = "probe-captcha-nobody"
PROBE_PWD = "definitely-wrong-password"

MAX_TRIES = 12
#: 服务端阈值是 8 次失败（见 ratelimit.DEFAULT_MAX_FAILURES）
EXPECT_CAPTCHA_BY = 8


def snapshot(page) -> dict:
    return page.evaluate(r"""
    () => ({
      captchaInput: Boolean(document.querySelector('input.human-input')),
      captchaImg: Boolean(document.querySelector('.human-image img')),
      error: (document.querySelector('.error-box') || {}).textContent || '',
      submitDisabled: (() => {
        const b = document.querySelector('button.auth-submit');
        return b ? b.disabled : null;
      })(),
    })
    """)


def main() -> int:
    out: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 460, "height": 900})
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        before = page.evaluate(
            "async () => (await fetch('/api/v1/auth/login-mode',"
            " {credentials:'include'})).json()")
        out.append("起始 login-mode: " + json.dumps(before, ensure_ascii=False))

        page.fill("input.auth-input:not([type=password])", PROBE_USER)
        page.fill("input.auth-input[type=password]", PROBE_PWD)

        appeared_at = None
        for i in range(1, MAX_TRIES + 1):
            # ⚠️ 先看再点：图形码一出现，提交按钮就变成 **disabled**
            # （图形码没填不让提交），此时 `click()` 会一直等到超时 ——
            # 第一版就是这么挂的，而超时信息看起来像"页面坏了"，
            # 实际是"图形码已经出来了"这个**成功**信号。
            pre = snapshot(page)
            if pre["captchaInput"] and pre["captchaImg"]:
                appeared_at = i - 1
                out.append(f"  第 {i} 轮开始前已见图形码 → 停止尝试")
                break
            try:
                page.click("button.auth-submit", timeout=4000)
            except Exception as exc:  # noqa: BLE001 按钮转禁用 / 重渲染
                out.append(f"  第 {i:>2} 次点击未完成（{type(exc).__name__}）"
                           "—— 多为按钮转禁用，即图形码已出现")
            page.wait_for_timeout(1400)
            s = snapshot(page)
            out.append(f"  第 {i:>2} 次: 图形码输入框={s['captchaInput']} "
                       f"图片={s['captchaImg']} 提交禁用={s['submitDisabled']} "
                       f"报错={(s['error'] or '')[:30]}")
            if s["captchaInput"] and s["captchaImg"]:
                appeared_at = i
                break
            # 输入框可能被 React 重渲染清空，补填
            page.fill("input.auth-input:not([type=password])", PROBE_USER)
            page.fill("input.auth-input[type=password]", PROBE_PWD)

        after = page.evaluate(
            "async () => (await fetch('/api/v1/auth/login-mode',"
            " {credentials:'include'})).json()")
        out.append("结束 login-mode: " + json.dumps(after, ensure_ascii=False))

        if appeared_at is None:
            out.append(f"结论: FAIL —— 试了 {MAX_TRIES} 次都没渲染图形码")
        else:
            s = snapshot(page)
            out.append(f"结论: PASS —— 第 {appeared_at} 次失败后图形码出现")
            out.append(f"       图形码为空时提交按钮禁用={s['submitDisabled']}"
                       f"（期望 True）")
            # 三项填齐后按钮应可用
            page.fill("input.auth-input:not([type=password])", PROBE_USER)
            page.fill("input.auth-input[type=password]", PROBE_PWD)
            page.fill("input.human-input", "ABCD")
            page.wait_for_timeout(700)
            s2 = snapshot(page)
            out.append(f"       三项填齐后提交按钮禁用={s2['submitDisabled']}"
                       f"（期望 False）")
            page.screenshot(path="data/_login_captcha_shown.png",
                            full_page=False)
        b.close()

    with open("data/_login_flow.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written data/_login_flow.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
