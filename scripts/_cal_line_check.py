"""用 Playwright 打开 pilot 的投资日历，截图并审计"每条是否一行"。

只跑在 `C:\\veighna_studio\\python.exe`（Playwright 只装在那里）。

    C:\\veighna_studio\\python.exe scripts\\_cal_line_check.py
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
USER = "admin"
PWD = "MossPilot2026x"

AUDIT_JS = r"""
() => {
  const out = { rows: [], unlockHead: null };
  document.querySelectorAll('.cal-row').forEach((row) => {
    const line = row.querySelector('.cal-line');
    const body = row.querySelector('.cal-body');
    if (!line || !body) return;
    const lr = line.getBoundingClientRect();
    const br = body.getBoundingClientRect();
    const title = (line.querySelector('.cal-title') || {}).textContent || '';
    out.rows.push({
      title: title.trim().slice(0, 26),
      lineH: Math.round(lr.height),
      bodyH: Math.round(br.height),
      stacked: br.height > lr.height * 1.8,
    });
  });
  const head = document.querySelector('.cal-stocks-head');
  if (head) out.unlockHead = Array.from(head.children).map(
    (c) => c.textContent.trim());
  return out;
}
"""


def login(page) -> None:
    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    try:
        page.fill("input.auth-input:not([type=password])", USER, timeout=10000)
        page.fill("input.auth-input[type=password]", PWD)
        page.click("button.auth-submit")
        page.wait_for_timeout(4000)
    except Exception as exc:  # noqa: BLE001
        print("登录步骤异常（可能已登录）:", type(exc).__name__)


def main() -> int:
    out: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        login(page)

        # 直接走 hash 路由到情报中心 —— 登录后默认落在别的视图
        # （实测 admin 落在 `#/intraday`），点页签定位器很脆。
        page.goto(f"{BASE}/#/intel", wait_until="domcontentloaded",
                  timeout=60000)
        page.wait_for_timeout(6000)
        # 子页签：投资日历
        page.get_by_role("tab", name=re.compile("投资日历")).first.click(
            timeout=20000)
        # ⚠️ 必须**等真实行出现**再断言：日历要并发拉几个上游源，
        # 实测 6 秒时页面还停在"正在读取日程…"，此时 `.cal-row` 数量是 0，
        # 审计会报"堆叠 0 条"—— 看起来像全绿，其实是**什么都没测**。
        for _ in range(30):
            page.wait_for_timeout(1000)
            if page.evaluate(
                    "() => document.querySelectorAll('.cal-row').length") > 0:
                break
        page.wait_for_timeout(1500)
        page.screenshot(path="data/_cal_wide.png", full_page=False)

        data = page.evaluate(AUDIT_JS)
        stacked = [r for r in data["rows"] if r["stacked"]]
        out.append(f"桌面 1440px：行数 {len(data['rows'])} | 仍堆叠 {len(stacked)}")
        for r in data["rows"][:12]:
            flag = "X" if r["stacked"] else "OK"
            out.append(f"  [{flag}] {r['title']:<26} line={r['lineH']} "
                       f"body={r['bodyH']}")

        # 展开解禁明细看表头
        try:
            page.get_by_role("button", name=re.compile("展开明细")).first.click(
                timeout=8000)
            page.wait_for_timeout(1500)
            head = page.evaluate(
                "() => { const h = document.querySelector('.cal-stocks-head');"
                " return h ? Array.from(h.children).map(c => "
                "c.textContent.trim()) : null; }")
            out.append(f"\n解禁表头: {head}")
            page.screenshot(path="data/_cal_unlock.png", full_page=False)
        except Exception as exc:  # noqa: BLE001
            out.append(f"\n展开解禁失败: {type(exc).__name__}")

        # 手机视口
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(2500)
        page.screenshot(path="data/_cal_mobile.png", full_page=True)
        m = page.evaluate(AUDIT_JS)
        out.append(f"\n手机 390px：行数 {len(m['rows'])} | 堆叠 "
                   f"{sum(1 for r in m['rows'] if r['stacked'])}")

        browser.close()
    with open("data/_cal_audit.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written data/_cal_audit.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
