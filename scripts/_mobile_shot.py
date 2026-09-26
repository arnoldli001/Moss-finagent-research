"""用真机尺寸（390×844）渲染公网页面并截图 —— 用来**看见**移动端布局问题。

## 为什么要这个脚本

用户报障是"手机界面没怎么适配，布局错位"。这类问题**不能靠读 CSS 判断**：
已经有好几处 `@media (max-width: 900px)` 了，看起来"适配过"，但手机是 390px，
落在从没被考虑过的区间。只有**按 390px 真渲染出来看一眼**，才知道哪里错位。

驱动的是本机已装的 Edge（`channel="msedge"`），不需要 `playwright install`
下载浏览器；用的是 C:\\veighna_studio 里那份 playwright（本项目的 .venv 没装）。

用法：
    C:\\veighna_studio\\python.exe scripts\\_mobile_shot.py
输出：`data/run/mobile/*.png`
"""

from __future__ import annotations

import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

# Windows 控制台默认 GBK：直接 print ✅/❌ 会 UnicodeEncodeError 把脚本打断
# （第一次跑就是这么挂的 —— 截图成功、死在一行日志上）。统一改成 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = sys.argv[1] if len(sys.argv) > 1 else \
    "https://excellence-module-films-whats.trycloudflare.com"
#: 视口尺寸（iPhone 17 Pro 逻辑分辨率 = 402×874）。
#: 第 2/3 个参数可覆盖，用来同时看**横屏**（874×402）：横竖屏的可用高度差一倍，
#: 只按竖屏调完再看横屏，多半会发现顶栏白占掉半屏。
VW = int(sys.argv[2]) if len(sys.argv) > 2 else 402
VH = int(sys.argv[3]) if len(sys.argv) > 3 else 874
TAG = f"{VW}x{VH}"
ACCOUNT = "admin"
PASSWORD = "MossPilot2026x"

OUT = pathlib.Path("data/run/mobile")
OUT.mkdir(parents=True, exist_ok=True)

SHOTS: list[tuple[str, str]] = []


def shot(page, name: str, *, full: bool = True) -> None:
    path = OUT / f"{TAG}-{name}.png"
    page.screenshot(path=str(path), full_page=full)
    SHOTS.append((name, str(path)))
    print(f"  截图 {name} -> {path}")


def audit_overflow(page, *, top: int = 8) -> None:
    """把**把页面撑宽的元素**直接列出来（class + 宽度 + 自身是否可滚动）。

    比"看一眼截图猜哪里宽"可靠得多：`scrollWidth` 只说"溢出了 474px"，
    而这里能说出"是 `.intraday-svg` 宽 864"这种可直接改的事实。
    """
    rows = page.evaluate(
        """(vw) => {
        const out = [];
        document.querySelectorAll('*').forEach((el) => {
          const r = el.getBoundingClientRect();
          // 只要**自身**比视口宽的元素 —— 祖先只是"内容溢出"（sw 大、w 被夹住），
          // 真正的元凶是那些自己就很宽的。按宽度倒序。
          if (r.width > vw + 2) {
            out.push({
              tag: el.tagName.toLowerCase(),
              cls: (el.className && el.className.toString
                    ? el.className.toString() : '').slice(0, 60),
              w: Math.round(r.width), sw: Math.round(el.scrollWidth),
              ox: getComputedStyle(el).overflowX,
              depth: (function (e) { let d = 0; while (e.parentElement) { d++; e = e.parentElement; } return d; })(el),
            });
          }
        });
        out.sort((a, b) => b.w - a.w);
        return out;
      }""", VW)
    seen: set[str] = set()
    print("   撑宽元素（去重后前几条）:")
    kept = 0
    for r in rows:
        key = f"{r['tag']}.{r['cls']}"
        if key in seen:
            continue
        seen.add(key)
        print(f"     {r['tag']:6s} w={r['w']:5d} sw={r['sw']:5d} "
              f"overflow-x={r['ox']:8s} .{r['cls']}")
        kept += 1
        if kept >= top:
            break


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        # 真机尺寸：390×844 = iPhone 14/15 逻辑分辨率；is_mobile + has_touch
        # 让页面走移动端分支（`<meta viewport>` 才会真正生效）。
        ctx = browser.new_context(
            viewport={"width": VW, "height": VH},
            device_scale_factor=2, is_mobile=True, has_touch=True,
            locale="zh-CN")
        page = ctx.new_page()
        page.set_default_timeout(45000)

        print("① 登录页")
        page.goto(BASE, wait_until="domcontentloaded")
        # ⚠️ 必须**等登录表单出现**再截图：应用启动先渲染"正在检查登录状态…"
        #    的过渡态（`useAuth.probe()` 要先问 /auth/me 再决定去登录页还是首页）。
        #    第一版固定 sleep 2.5s 就截，拿到的正是那个过渡态 ——
        #    于是"横向溢出检测"也变成了一句空话（页面上只有一个居中卡片）。
        page.wait_for_selector('input[placeholder="输入密码"]', timeout=30000)
        page.wait_for_timeout(1200)
        shot(page, "01-login")
        over = page.evaluate(
            "() => ({sw: document.documentElement.scrollWidth,"
            " cw: document.documentElement.clientWidth})")
        print(f"   横向溢出检测: scrollWidth={over['sw']} clientWidth={over['cw']}"
              f" {'❌ 溢出' if over['sw'] > over['cw'] + 1 else '✅ 未溢出'}")

        print("② 登录")
        page.fill('input[placeholder="you@example.com"]', ACCOUNT)
        page.fill('input[placeholder="输入密码"]', PASSWORD)
        page.click("button.auth-submit")
        # 等顶栏出现（登录成功、工作台渲染完成）
        page.wait_for_selector("header.header", timeout=30000)
        page.wait_for_timeout(2500)
        shot(page, "02-after-login")

        # ★ 管理员登录后**默认落在系统管理视图**（adminNav = isAdmin && !tenantView），
        #   业务页签根本不在 DOM 里 —— 所以要先去「业务视图」。
        #   第一版没做这一步，于是"找不到页签「量化交易」"，白白跑了一轮。
        if page.get_by_role("button", name="业务视图", exact=True).count():
            page.get_by_role("button", name="业务视图", exact=True).first.click()
            page.wait_for_timeout(3000)
            shot(page, "03-tenant-home")

        for label, name in (("量化交易", "04-quant"), ("资金流监控", "05-fundflow"),
                            ("事件告警", "06-alerts")):
            try:
                tab = page.get_by_role("button", name=label, exact=True)
                if tab.count() == 0:
                    print(f"   （找不到页签「{label}」，跳过）")
                    continue
                tab.first.scroll_into_view_if_needed()
                tab.first.click()
                page.wait_for_timeout(8000)
                # 做T面板的左侧自选栏默认是收起的；要核对的正是它，
                # 所以点一下「自选 N」把它展开（用户就是这么看的）。
                try:
                    btn = page.get_by_role("button", name=re.compile(r"^自选"))
                    if btn.count():
                        btn.first.click()
                        page.wait_for_timeout(2500)
                except Exception:
                    pass
                # 面板页**只截视口**：整页截图的像素高度会超过 8192（读图上限），
                # 而"手机上看到的就是这一屏"——视口截图才是要判断的东西。
                shot(page, name, full=False)
                o = page.evaluate(
                    "() => ({sw: document.documentElement.scrollWidth,"
                    " cw: document.documentElement.clientWidth})")
                flag = "❌ 溢出" if o["sw"] > o["cw"] + 1 else "✅ 未溢出"
                print(f"   {label}: scrollWidth={o['sw']} {flag}")
                if o["sw"] > o["cw"] + 1:
                    audit_overflow(page)
            except Exception as exc:  # noqa: BLE001 截图脚本不该因为一个页签挂掉
                print(f"   {label} 失败：{type(exc).__name__}: {str(exc)[:120]}")

        # 回管理台看「用户管理」（用户就是在这里看不到申请记录的）
        try:
            page.get_by_role("button", name="账户与设置").first.click()
            page.wait_for_timeout(800)
            # ★ 专门截"菜单已打开"这一张：账户菜单原是 `position:absolute; right:0`
            #   的浮层，手机上头像换行后会把它推出视口左边界（用户报障"左半边
            #   没显示出来，出界了"）。这类问题**只有把菜单打开再截图**才看得见 ——
            #   之前 7 张截图都没打开它，所以漏掉了。
            shot(page, "08-account-menu", full=False)
            box = page.evaluate(
                """() => { const el = document.querySelector('.account-menu');
                   if (!el) return null; const r = el.getBoundingClientRect();
                   return {left: Math.round(r.left), right: Math.round(r.right),
                           vw: window.innerWidth}; }""")
            if box:
                clipped = box["left"] < -1 or box["right"] > box["vw"] + 1
                print(f"   账户菜单: left={box['left']} right={box['right']} "
                      f"视口宽={box['vw']} "
                      f"{'❌ 出界' if clipped else '✅ 完整在视口内'}")
            item = page.get_by_role("button", name="系统管理 · 用户管理")
            if item.count():
                item.first.click()
                page.wait_for_timeout(4000)
                shot(page, "07-admin-users", full=False)
        except Exception as exc:  # noqa: BLE001
            print(f"   管理台截图失败：{type(exc).__name__}: {str(exc)[:120]}")

        ctx.close()
        browser.close()

    print("\n完成，共", len(SHOTS), "张：")
    for name, path in SHOTS:
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
