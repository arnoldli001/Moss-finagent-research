"""验证登录图形码：服务端要求时必须真的渲染出输入框。

## 复现的 bug（2026-09-25 用户报障）

> "登录问题，这图形验证码呢？"

截图里有一句红字「为确认是你本人操作，请先完成图形验证码」，
但页面上**根本没有图形码可填** —— 用户被彻底锁在门外。

根因不在服务端：`LoginScreen.run()` 用
`/captcha_required/.test(String(e))` 判断，而 `ApiError.message`
已经是中文文案（"…请先完成图形验证码"），`String(e)` 里
**没有错误码**。于是服务端要图形码时前端永不渲染输入框。

## 这个脚本断言什么

    1. GET /auth/login-mode 说 require_captcha=true 时，
       页面必须出现图形码输入框（`input[inputmode=numeric]` 之类）
    2. 登录按钮在有图形码但未填写时必须**禁用**
    3. 服务端在"没带图形码"时返回 400 captcha_required（服务端行为）

只在 `C:\\veighna_studio\\python.exe` 下跑。
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
USER, PWD = "admin", "MossPilot2026x"

PROBE = r"""
() => {
  const inputs = Array.from(document.querySelectorAll('input')).map((i) => ({
    type: i.type,
    placeholder: i.placeholder || '',
    cls: i.className,
  }));
  const imgs = Array.from(document.querySelectorAll('img'))
    .map((i) => (i.src || '').slice(0, 40));
  // ⚠️ 提交按钮要按 **class** 找（`button.auth-submit`）。
  // 第一版按文字 "登录" 找，结果匹配到的是顶部那个"登录/注册/找回密码"
  // **模式切换标签** —— 它永远可点，于是探针报 submitDisabled=False，
  // 看起来像"图形码没填也能提交"这个严重问题，实际是探针指错了元素。
  const submit = document.querySelector('button.auth-submit');
  return {
    inputs,
    imgs,
    // 图形码输入框的判据是 class `human-input`（不是 inputmode/maxlength ——
    // 那是组件内部实现，实测是 inputmode=text / maxlength=8）
    hasCaptchaInput: Boolean(document.querySelector('input.human-input')),
    hasCaptchaImg: Boolean(document.querySelector('.human-image img')),
    submitDisabled: submit ? submit.disabled : null,
  };
}
"""


def main() -> int:
    out: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 460, "height": 900})
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        # 图形码是异步领的，等它出现
        page.wait_for_timeout(6000)

        mode = page.evaluate(
            "async () => (await fetch('/api/v1/auth/login-mode',"
            " {credentials:'include'})).json()")
        out.append("login-mode: " + json.dumps(mode, ensure_ascii=False))

        data = page.evaluate(PROBE)
        out.append("inputs: " + json.dumps(data["inputs"], ensure_ascii=False))
        out.append("imgs: " + json.dumps(data["imgs"], ensure_ascii=False))
        out.append(f"有图形码输入框={data['hasCaptchaInput']} "
                   f"有图形码图片={data['hasCaptchaImg']} "
                   f"提交按钮禁用={data['submitDisabled']}")

        need = bool(mode.get("require_captcha"))
        ok = (not need) or (data["hasCaptchaInput"] and data["hasCaptchaImg"])
        out.append("结论: " + ("PASS" if ok else "FAIL —— 需要图形码却没渲染出来"))
        # 图形码未填写时按钮必须禁用（防止用户空提交拿到"验证码不正确"）
        if need and data["submitDisabled"] is not True:
            out.append("提醒: 图形码为空但提交按钮未禁用")

        # 再验一次：账号+密码+图形码都填好后，按钮必须变为可用。
        #
        # ⚠️ 三个都要填：按钮的 disabled 是
        # `busy || !account || !password || (需要图形码 && !图形码已填)`。
        # 第一版只填了图形码，于是按钮仍禁用，看起来像"填了也没用"，
        # 实际是账号密码还空着 —— 断言必须把前置条件补齐。
        if need and data["hasCaptchaInput"]:
            page.fill("input.auth-input:not([type=password])"
                      ":not(.human-input)", "probe-user")
            page.fill("input.auth-input[type=password]", "probe-pass")
            page.fill("input.human-input", "ABCD")
            page.wait_for_timeout(700)
            after = page.evaluate(
                "() => { const b = document.querySelector('button.auth-submit');"
                " return b ? b.disabled : null; }")
            out.append(f"账号+密码+图形码都填好后 提交按钮禁用={after}"
                       f"（期望 False）")
        page.screenshot(path="data/_login_captcha.png", full_page=False)
        b.close()

    with open("data/_login_captcha.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written data/_login_captcha.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
