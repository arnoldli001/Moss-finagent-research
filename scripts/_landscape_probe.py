"""测量手机横屏（874x402）下情报中心的垂直空间占用。"""
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
VW = int(sys.argv[2]) if len(sys.argv) > 2 else 874
VH = int(sys.argv[3]) if len(sys.argv) > 3 else 402

PROBE = """() => {
  const vis = (s) => { const e = document.querySelector(s);
    if (!e) return null; const r = e.getBoundingClientRect();
    return {top: Math.round(r.top), h: Math.round(r.height)}; };
  const disp = (s) => { const e = document.querySelector(s);
    return e ? getComputedStyle(e).display !== 'none' : null; };
  return {
    vh: window.innerHeight, vw: window.innerWidth,
    tableShown: disp('.intel-tablewrap'), cardsShown: disp('.intel-cards'),
    header: vis('header.header'),
    banner: vis('.server-banner'),
    compliance: vis('.intel-compliance'),
    subtabs: vis('.intel-subtabs'),
    controls: vis('.intel-controls'),
    firstCard: vis('.intel-card'),
    firstRow: vis('.intel-table tbody tr'),
    overflowX: document.documentElement.scrollWidth
               > document.documentElement.clientWidth + 1,
    docH: document.documentElement.scrollHeight,
  };
}"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(viewport={"width": VW, "height": VH},
                        device_scale_factor=2, is_mobile=True, has_touch=True,
                        locale="zh-CN")
    pg = ctx.new_page()
    pg.set_default_timeout(45000)
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_selector('input[placeholder="输入密码"]', timeout=30000)
    pg.fill('input[placeholder="you@example.com"]', "admin")
    pg.fill('input[placeholder="输入密码"]', "MossDev2026x")
    pg.click("button.auth-submit")
    pg.wait_for_selector("header.header", timeout=30000)
    pg.wait_for_timeout(2000)
    if pg.get_by_role("button", name="业务视图", exact=True).count():
        pg.get_by_role("button", name="业务视图", exact=True).first.click()
        pg.wait_for_timeout(2500)
    pg.get_by_role("button", name="情报中心", exact=True).first.click()
    pg.wait_for_selector(".intel-root", timeout=30000)
    pg.wait_for_function("() => !document.querySelector('.intel-loading')",
                         timeout=90000)
    pg.wait_for_timeout(1500)

    m = pg.evaluate(PROBE)
    print(f"--- {VW}x{VH} ---")
    for k, v in m.items():
        print(f"  {k:14s} {v}")
    anchor = m.get("firstCard") or m.get("firstRow")
    if anchor:
        used = anchor["top"]
        print(f"  => 首条内容距顶部 {used}px，占视口 "
              f"{round(100 * used / max(1, m['vh']))}%  "
              f"（可容纳 {round((m['vh'] - used) / max(1, anchor['h']))} 条）")
    pg.screenshot(path=f"data/run/intel/land-{VW}x{VH}.png")
    b.close()
