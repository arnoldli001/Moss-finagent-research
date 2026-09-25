"""截取"投资日历 · 限售解禁展开明细"这一屏，验证三视口不错位。"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
ACC, PWD = "admin", "MossDev2026x"
OUT = pathlib.Path("data/run/intel")
OUT.mkdir(parents=True, exist_ok=True)

FAILED: list[str] = []


def note(ok: bool, text: str) -> None:
    print(f"   {'OK  ' if ok else 'FAIL'} {text}")
    if not ok:
        FAILED.append(text)


def run(p, tag: str, vw: int, vh: int) -> None:
    print(f"\n=== {tag} {vw}x{vh} ===")
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(viewport={"width": vw, "height": vh},
                        device_scale_factor=2, is_mobile=vw < 768,
                        has_touch=vw < 768, locale="zh-CN")
    pg = ctx.new_page()
    pg.set_default_timeout(45000)
    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_selector('input[placeholder="输入密码"]', timeout=30000)
    pg.fill('input[placeholder="you@example.com"]', ACC)
    pg.fill('input[placeholder="输入密码"]', PWD)
    pg.click("button.auth-submit")
    pg.wait_for_selector("header.header", timeout=30000)
    pg.wait_for_timeout(2000)
    if pg.get_by_role("button", name="业务视图", exact=True).count():
        pg.get_by_role("button", name="业务视图", exact=True).first.click()
        pg.wait_for_timeout(2500)
    tab = pg.get_by_role("button", name="情报中心", exact=True)
    tab.first.click()
    pg.wait_for_selector(".intel-root", timeout=30000)
    pg.wait_for_function("() => !document.querySelector('.intel-loading')",
                         timeout=90000)
    pg.wait_for_timeout(1500)

    # 切到投资日历
    cal = pg.get_by_role("tab", name="投资日历", exact=False)
    (cal.first if cal.count() else
     pg.get_by_role("button", name="投资日历", exact=False).first).click()
    pg.wait_for_timeout(3000)

    # 找到第一个"限售解禁"批量行，展开
    rows = pg.locator(".cal-row.bulk")
    n = rows.count()
    print(f"   批量行 {n} 个")
    opened = False
    for i in range(n):
        txt = rows.nth(i).inner_text()
        if "限售解禁" in txt:
            rows.nth(i).locator(".cal-bulk-toggle").click()
            pg.wait_for_timeout(1200)
            opened = True
            print(f"   已展开第 {i} 行（限售解禁）")
            break
    note(opened, "找到并展开了限售解禁行")

    stocks = pg.locator(".cal-stocks li")
    cnt = stocks.count()
    note(cnt > 0, f"个股明细渲染了 {cnt} 行")
    if cnt:
        first = stocks.first.inner_text().replace("\n", " | ")
        print(f"   第 1 行: {first[:90]}")
        # 断言确实有股票名与占比（用户报障的两项）
        note(bool(stocks.first.locator(".cal-stock-name").inner_text().strip()),
             "明细含股票名称")
        note("%" in stocks.first.locator(".cal-stock-pct").inner_text(),
             "明细含占流通股比例")

    # 把展开的明细滚进视口再截图 —— 否则截图停在页首，
    # 看不到刚验证过的那些个股行（用户报障的正是"点开什么都没有"）
    if cnt:
        stocks.first.scroll_into_view_if_needed()
        pg.wait_for_timeout(600)

    ov = pg.evaluate("() => ({sw: document.documentElement.scrollWidth,"
                     " cw: document.documentElement.clientWidth})")
    note(ov["sw"] <= ov["cw"] + 1,
         f"展开后无横向溢出 ({ov['sw']} vs {ov['cw']})")

    pg.screenshot(path=str(OUT / f"{tag}-unlock-open.png"))
    print(f"   截图 -> {OUT / f'{tag}-unlock-open.png'}")
    ctx.close()
    b.close()


def main() -> int:
    with sync_playwright() as p:
        for tag, vw, vh in (("iphone17pro", 402, 874), ("ipad", 820, 1180),
                            ("desktop", 1440, 900)):
            try:
                run(p, tag, vw, vh)
            except Exception as e:  # noqa: BLE001
                note(False, f"{tag} 失败：{type(e).__name__}: {str(e)[:120]}")
    print("\n" + "=" * 56)
    if FAILED:
        print(f"发现 {len(FAILED)} 个问题：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过：解禁明细在三个视口都正确渲染且不溢出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
