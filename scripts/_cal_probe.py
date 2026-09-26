"""查 `.cal-row.bulk` 的 `.cal-body` 里到底是哪个子元素多占了高度。"""
import re

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
USER, PWD = "admin", "MossPilot2026x"

PROBE = r"""
() => {
  const row = document.querySelector('.cal-row.bulk');
  if (!row) return { err: '没有 bulk 行' };
  const body = row.querySelector('.cal-body');
  const kids = Array.from(body.children).map((c) => ({
    tag: c.tagName,
    cls: c.className,
    h: Math.round(c.getBoundingClientRect().height),
    txt: (c.textContent || '').trim().slice(0, 24),
  }));
  const line = row.querySelector('.cal-line');
  const lineKids = line ? Array.from(line.children).map((c) => ({
    tag: c.tagName,
    cls: String(c.className).slice(0, 28),
    h: Math.round(c.getBoundingClientRect().height),
    w: Math.round(c.getBoundingClientRect().width),
    txt: (c.textContent || '').trim().slice(0, 18),
  })) : [];
  return {
    rowH: Math.round(row.getBoundingClientRect().height),
    bodyH: Math.round(body.getBoundingClientRect().height),
    bodyKids: kids,
    lineH: line ? Math.round(line.getBoundingClientRect().height) : 0,
    lineOverflow: line ? line.scrollWidth > line.clientWidth + 1 : null,
    lineKids,
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
            page.wait_for_timeout(4000)
        except Exception:  # noqa: BLE001
            pass
        page.goto(f"{BASE}/#/intel", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        page.get_by_role("tab", name=re.compile("投资日历")).first.click(timeout=20000)
        for _ in range(30):
            page.wait_for_timeout(1000)
            if page.evaluate("() => document.querySelectorAll('.cal-row').length") > 0:
                break
        page.wait_for_timeout(1500)
        import json
        out.append(json.dumps(page.evaluate(PROBE), ensure_ascii=False, indent=1))
        b.close()
    with open("data/_cal_probe.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
