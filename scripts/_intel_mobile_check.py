#!/usr/bin/env python
"""情报中心的真机尺寸验证：三种视口 × 三个子页签，自动查错位/重叠。

## 为什么必须真渲染，不能靠读 CSS

手机上"错位/重叠"的成因几乎都是**某处比视口宽**，把整个文档撑开 ——
之后顶栏、浮层、`fixed` 元素全部跟着视口错位。光看 CSS 看不出来：
`.intel-table` 写了 `width: 100%`，但里面某个 `white-space: nowrap` 的单元格
会把它的**最小内容宽**顶到 600px 以上，`100%` 就此失效。

所以这里做**四类客观检测**，不靠人眼看截图：

  ① `documentElement.scrollWidth > clientWidth` → 整页横向溢出
  ② 逐个元素量 `getBoundingClientRect().width > 视口` → 列出**元凶**（class+宽度）
  ③ 兄弟元素矩形相交 → **重叠**（真实的重叠，不是视觉错觉）
  ④ 关键控件高度 < 32px → 触控目标过小（手机上点不准）

用法：
    .venv\\Scripts\\python.exe scripts\\_intel_mobile_check.py
    .venv\\Scripts\\python.exe scripts\\_intel_mobile_check.py http://127.0.0.1:8100
"""

from __future__ import annotations

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

# Windows 控制台默认 GBK：直接 print 中文以外的字符会 UnicodeEncodeError
# 把脚本打断（本项目已经踩过）。统一改成 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
ACCOUNT = os.environ.get("SHOT_ACCOUNT", "admin")
PASSWORD = os.environ.get("SHOT_PASSWORD", "MossDev2026x")

#: 要验的视口。前两个是**用户点名要覆盖**的机型。
VIEWPORTS: list[tuple[str, int, int]] = [
    ("iphone17pro", 402, 874),   # iPhone 17 Pro 逻辑分辨率
    ("ipad", 820, 1180),         # iPad Air 竖屏
    ("desktop", 1440, 900),
]

#: 情报中心的三个子页签
SUBTABS = ["情报流", "舆情热度监控", "投资日历"]

OUT = pathlib.Path("data/run/intel")
OUT.mkdir(parents=True, exist_ok=True)

FAILED: list[str] = []


def note(ok: bool, text: str) -> None:
    print(f"   {'OK  ' if ok else 'FAIL'} {text}")
    if not ok:
        FAILED.append(text)


def overflow_report(page, vw: int) -> None:
    """① 整页溢出 + ② 元凶清单。"""
    doc = page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth,"
        " cw: document.documentElement.clientWidth})")
    over = doc["sw"] > doc["cw"] + 1
    note(not over,
         f"整页横向溢出 scrollWidth={doc['sw']} clientWidth={doc['cw']}")
    if not over:
        return
    rows = page.evaluate(
        """(vw) => {
        const out = [];
        document.querySelectorAll('*').forEach((el) => {
          const r = el.getBoundingClientRect();
          if (r.width > vw + 2) {
            out.push({
              tag: el.tagName.toLowerCase(),
              cls: (el.className && el.className.toString
                    ? el.className.toString() : '').slice(0, 70),
              w: Math.round(r.width), right: Math.round(r.right),
              ox: getComputedStyle(el).overflowX,
            });
          }
        });
        out.sort((a, b) => b.w - a.w);
        return out.slice(0, 8);
      }""", vw)
    print("        撑宽元素：")
    for r in rows:
        print(f"          {r['tag']:6s} w={r['w']:5d} right={r['right']:5d} "
              f"overflow-x={r['ox']:8s} .{r['cls']}")


def overlap_report(page) -> None:
    """③ 兄弟元素矩形相交 —— 真实重叠。

    只比**兄弟**且**都在文档流内**的元素（排除浮层、绝对定位、以及
    `display: contents` 的容器），否则会把正常的浮层/下拉当成重叠。
    相交面积要**显著**（>4px 见方）才算，避免 1px 的圆角贴合误报。
    """
    bad = page.evaluate(
        """() => {
        const INTERESTING = ['.intel-card', '.intel-subtab', '.intel-filter',
          '.cal-row', '.heat-kpi', '.heat-card', '.intel-head-right',
          '.brief-sec', '.cal-sum-item', '.intel-compliance'];
        const els = [...document.querySelectorAll(INTERESTING.join(','))]
          .filter((el) => {
            const st = getComputedStyle(el);
            if (st.position === 'absolute' || st.position === 'fixed') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          });
        const hits = [];
        for (let i = 0; i < els.length; i++) {
          for (let j = i + 1; j < els.length; j++) {
            const a = els[i], b = els[j];
            // 只查"互不为祖先"的一对
            if (a.contains(b) || b.contains(a)) continue;
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
            const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
            if (ox > 4 && oy > 4) {
              hits.push({
                a: a.className.toString().slice(0, 40),
                b: b.className.toString().slice(0, 40),
                ox: Math.round(ox), oy: Math.round(oy),
              });
            }
          }
        }
        return hits.slice(0, 6);
      }""")
    note(not bad, f"关键元素重叠 {len(bad)} 处")
    for h in bad:
        print(f"        .{h['a']} × .{h['b']}  重叠 {h['ox']}×{h['oy']}px")


def touch_report(page) -> None:
    """④ 触控目标高度：手机上 <32px 基本点不准。"""
    small = page.evaluate(
        """() => {
        const els = [...document.querySelectorAll(
          '.intel-subtab, .intel-filter, .intel-refresh, .intel-card')];
        return els.filter((el) => {
          const r = el.getBoundingClientRect();
          return r.height > 0 && r.height < 32;
        }).map((el) => ({
          cls: el.className.toString().slice(0, 40),
          h: Math.round(el.getBoundingClientRect().height),
        })).slice(0, 6);
      }""")
    note(not small, f"触控目标过小（<32px）{len(small)} 个")
    for s in small:
        print(f"        .{s['cls']}  height={s['h']}px")


def shot(page, tag: str, name: str, *, full: bool = False) -> pathlib.Path:
    path = OUT / f"{tag}-{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print(f"   截图 -> {path}")
    return path


def run_viewport(p, tag: str, vw: int, vh: int) -> None:
    print(f"\n=== {tag}  {vw}×{vh} ===")
    browser = p.chromium.launch(channel="msedge", headless=True)
    ctx = browser.new_context(
        viewport={"width": vw, "height": vh},
        device_scale_factor=2, is_mobile=vw < 768, has_touch=vw < 768,
        locale="zh-CN")
    page = ctx.new_page()
    page.set_default_timeout(45000)

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector('input[placeholder="输入密码"]', timeout=30000)
    page.fill('input[placeholder="you@example.com"]', ACCOUNT)
    page.fill('input[placeholder="输入密码"]', PASSWORD)
    page.click("button.auth-submit")
    page.wait_for_selector("header.header", timeout=30000)
    page.wait_for_timeout(2000)

    # 管理员默认落在系统管理视图 → 先去业务视图
    if page.get_by_role("button", name="业务视图", exact=True).count():
        page.get_by_role("button", name="业务视图", exact=True).first.click()
        page.wait_for_timeout(2500)

    tab = page.get_by_role("button", name="情报中心", exact=True)
    if tab.count() == 0:
        note(False, "找不到页签「情报中心」—— 套餐未开通或页签未接入")
        ctx.close()
        browser.close()
        return
    tab.first.scroll_into_view_if_needed()
    tab.first.click()
    # 情报流要真去聚合六个源（实测 2~6s），再加上知识星球的增量拉取。
    # 固定 sleep 是不够的 —— 第一版等 9s，截到的是"正在聚合公开信息…"
    # 那条加载态（截图里一个字的数据都没有，白跑一轮）。
    # 所以**等加载态消失**，而不是等一个猜出来的秒数。
    page.wait_for_selector(".intel-root", timeout=30000)
    try:
        page.wait_for_function(
            "() => !document.querySelector('.intel-loading')", timeout=60000)
    except Exception:  # noqa: BLE001 超时就照实截（顺便暴露"加载不出来"）
        note(False, "情报流加载超过 60s 仍未完成")
    page.wait_for_timeout(1500)

    for name in SUBTABS:
        btn = page.get_by_role("tab", name=name, exact=False)
        if btn.count() == 0:
            # role=tab 可能没被识别，退回到文本匹配
            btn = page.get_by_role("button", name=name, exact=False)
        if btn.count() == 0:
            note(False, f"找不到子页签「{name}」")
            continue
        btn.first.click()
        page.wait_for_timeout(3500)
        print(f"  -- 子页签「{name}」--")
        shot(page, tag, f"{SUBTABS.index(name) + 1:02d}-{name}")
        overflow_report(page, vw)
        overlap_report(page)
        if vw < 768:
            touch_report(page)

    # 整页长图（看纵向堆叠）—— 只在手机档截，桌面整页太高
    if vw < 768:
        page.get_by_role("tab", name="情报流", exact=False).first.click()
        page.wait_for_timeout(2500)
        shot(page, tag, "99-full", full=True)

    ctx.close()
    browser.close()


def main() -> int:
    print(f"目标：{BASE}")
    with sync_playwright() as p:
        for tag, vw, vh in VIEWPORTS:
            try:
                run_viewport(p, tag, vw, vh)
            except Exception as exc:  # noqa: BLE001 一个视口失败不该中断全部
                note(False, f"{tag} 渲染失败：{type(exc).__name__}: {str(exc)[:140]}")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"发现 {len(FAILED)} 个问题：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过：无横向溢出、无重叠、触控目标合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
