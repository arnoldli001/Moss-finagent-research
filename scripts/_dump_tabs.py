"""先看清 pilot 页面上到底有哪些可点的页签（排查定位器）。"""
import sys

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
USER = "admin"
PWD = "MossPilot2026x"

DUMP = r"""
() => {
  const t = (sel) => Array.from(document.querySelectorAll(sel))
    .map((e) => (e.textContent || '').trim().slice(0, 20))
    .filter((s) => s);
  return {
    buttons: t('button'),
    tabs: t('[role=tab]'),
    navs: t('nav button'),
    url: location.href,
  };
}
"""


def main() -> int:
    lines: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 950})
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        try:
            page.fill("input.auth-input:not([type=password])", USER, timeout=8000)
            page.fill("input.auth-input[type=password]", PWD)
            page.click("button.auth-submit")
            page.wait_for_timeout(5000)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"login: {type(exc).__name__}")
        data = page.evaluate(DUMP)
        for k, v in data.items():
            lines.append(f"--- {k} ---")
            if isinstance(v, list):
                lines.extend(f"    {x}" for x in v)
            else:
                lines.append(f"    {v}")
        page.screenshot(path="data/_pilot_home.png", full_page=False)
        b.close()
    # 显式写 UTF-8：Windows 控制台是 GBK，emoji 会让 print 直接抛异常
    with open("data/_tabs_dump.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("written data/_tabs_dump.txt", len(lines), "lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
