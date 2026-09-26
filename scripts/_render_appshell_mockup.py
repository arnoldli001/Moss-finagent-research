"""把 `docs/_mockup_appshell.html` 逐页渲染成 PNG。

跑法（Playwright 只装在 VeighNa 那个解释器里）：

    C:\\veighna_studio\\python.exe scripts\\_render_appshell_mockup.py

产出：
    docs/_mockup_appshell_<page>.png        桌面 1440×900，每个页签一张
    docs/_mockup_appshell_m_ui_portrait.png 手机竖屏（抽屉展开）
    docs/_mockup_appshell_m_ui_closed.png   手机竖屏（抽屉收起）
    docs/_mockup_appshell_m_landscape.png   手机横屏（图标轨道）
    docs/_mockup_appshell_m_tablet.png      平板（图标轨道）
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "docs" / "_mockup_appshell.html").as_uri()
OUT = ROOT / "docs"

#: 需要单独出图的页签（顺序即侧边栏顺序）
PAGES = [
    ("research", "AI 投研工作台"),
    ("mainline", "板块主线挖掘"),
    ("fundflow", "资金流监控"),
    ("etf", "ETF 份额监控"),
    ("factor", "因子与回测"),
    ("screen", "量化选股"),
    ("auction", "集合竞价选股"),
    ("tplus", "做 T 辅助"),
    ("intel", "情报中心"),
    ("brief", "盘前简报"),
    ("alerts", "事件告警中心"),
    ("pools", "我的自选池"),
    ("sched", "调度与任务"),
    ("audit", "审计与合规"),
    ("users", "用户管理"),
]

#: 三种手机/平板形态：(后缀, body class, viewport, 说明)
MOBILE = [
    ("m_ui_portrait", "m-portrait", {"width": 390, "height": 844},
     "手机竖屏 · 抽屉展开"),
    ("m_ui_closed", "m-portrait drawer-closed", {"width": 390, "height": 844},
     "手机竖屏 · 抽屉收起（默认态）"),
    ("m_landscape", "m-landscape", {"width": 874, "height": 402},
     "手机横屏 · 图标轨道"),
    ("m_tablet", "m-tablet", {"width": 834, "height": 1112},
     "平板竖屏 · 图标轨道"),
]


def main() -> int:
    done: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ── 桌面：每页一张 ──
        page = browser.new_page(viewport={"width": 1440, "height": 900},
                                device_scale_factor=1)
        page.goto(HTML, wait_until="load")
        page.wait_for_timeout(500)
        for pid, label in PAGES:
            page.evaluate(f"render('{pid}')")
            page.wait_for_timeout(220)
            name = f"_mockup_appshell_{pid}.png"
            page.screenshot(path=str(OUT / name), full_page=False)
            done.append(f"{name}  ({label})")

        # ── 手机/平板形态：都用"情报中心"页（内容最全，最能看出挤压）──
        page.evaluate("render('intel')")
        for suffix, cls, vp, label in MOBILE:
            page.set_viewport_size(vp)
            page.evaluate(
                "c => { document.body.className = c; render('intel'); }", cls)
            page.wait_for_timeout(320)
            name = f"_mockup_appshell_{suffix}.png"
            page.screenshot(path=str(OUT / name), full_page=False)
            done.append(f"{name}  ({label} {vp['width']}×{vp['height']})")

        # 长页也出一张整页图（桌面情报中心），方便一次看全
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("() => { document.body.className = ''; render('intel'); }")
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "_mockup_appshell_intel_full.png"),
                        full_page=True)
        done.append("_mockup_appshell_intel_full.png  (情报中心 整页)")

        browser.close()

    print(f"共 {len(done)} 张，输出到 {OUT}:")
    for d in done:
        print("  ", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
