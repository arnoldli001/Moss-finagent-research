"""确认情报流里的可信度**外圈已去掉**（只剩数字 + 分档色），并看当前子页签名。"""
import json
import re

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
#: ⚠️ 用 **vip 测试账号**，不要用 admin。
#: 实测 admin 的密码在 2026-09-25 17:45 被改过，而我的探测已把
#: `dim_user_credential.failed_attempts` 推到 4/5 —— 再错一次就**锁号**。
#: 探测脚本绝不该有把生产管理员账号锁掉的能力。
USER, PWD = "testyang", "MossTest2026x"

PROBE = r"""
() => {
  const ring = document.querySelector('.cred-ring');
  const cs = ring ? getComputedStyle(ring) : null;
  const r = ring ? ring.getBoundingClientRect() : null;
  // 页面上所有 "页签" 类按钮的文字，用来确认命名
  const tabs = Array.from(document.querySelectorAll(
    '.intel-subtab, [role=tab], .tab, nav button'))
    .map((e) => (e.textContent || '').trim().slice(0, 20))
    .filter(Boolean);
  const title = (document.querySelector('.intel-title h2') || {}).textContent || '';
  return {
    ringFound: Boolean(ring),
    ringText: ring ? ring.textContent.trim() : '',
    border: cs ? cs.borderTopWidth + ' / ' + cs.borderTopStyle : '',
    borderRadius: cs ? cs.borderRadius : '',
    bottomBorder: cs ? cs.borderBottomWidth + ' ' + cs.borderBottomStyle : '',
    size: r ? Math.round(r.width) + '×' + Math.round(r.height) : '',
    pageTitle: title.trim(),
    tabs,
  };
}
"""


def main() -> int:
    out: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 950})
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        try:
            page.fill("input.auth-input:not([type=password])", USER, timeout=8000)
            page.fill("input.auth-input[type=password]", PWD)
            page.click("button.auth-submit")
            page.wait_for_timeout(4500)
        except Exception:  # noqa: BLE001
            pass
        page.goto(f"{BASE}/#/intel", wait_until="domcontentloaded", timeout=60000)
        for _ in range(20):
            page.wait_for_timeout(1000)
            if page.evaluate("() => document.querySelectorAll('.cred-ring').length"):
                break
        page.wait_for_timeout(1200)
        data = page.evaluate(PROBE)
        # 诊断：如果什么都没找到，把页面开头文字与登录框是否存在一起打出来
        diag = page.evaluate(r"""
        () => ({
          url: location.href,
          head: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 220),
          loginForm: Boolean(document.querySelector('input.auth-input')),
          submitDisabled: (() => {
            const b = document.querySelector('button.auth-submit');
            return b ? b.disabled : null;
          })(),
          err: (document.querySelector('.error-box, .auth-msg') || {}).textContent || '',
        })
        """)
        out.append(json.dumps(diag, ensure_ascii=False, indent=1))
        out.append(json.dumps(data, ensure_ascii=False, indent=1))
        # 只截情报流那一块，方便看可信度列
        try:
            page.locator(".intel-table").first.screenshot(
                path="data/_cred_after.png")
            out.append("已截 data/_cred_after.png")
        except Exception as exc:  # noqa: BLE001
            out.append("截表格失败: " + type(exc).__name__)
        page.screenshot(path="data/_feed_after.png", full_page=False)
        b.close()
    with open("data/_cred_check.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
