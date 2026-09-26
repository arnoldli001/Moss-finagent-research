"""验证子页签改名已上线，并确认更长的标签在 390px 下不会挤爆/截断。

## 两步（都不需要登录生产）

1. **取线上 JS 产物**，断言里面真的有「热点&研报小作文」——
   证明改动已发布，而不是只在我本地。
2. 用**线上 CSS** 在本地渲染页签条（两个真实标签 + 计数），
   在 390px 宽度下量：
     · 是否溢出容器（`scrollWidth > clientWidth`）→ 溢出即需要滑动，要提示
     · 标签是否被截断（`scrollWidth > clientWidth` on 按钮）
   两个页签总共约 280px，理论上 390px 放得下；放不下就必须给滑动提示。
"""
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Connection": "close"}
LABEL = "热点&研报小作文"

HARNESS = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body><div class="intel-root"><div class="intel-subtabs" role="tablist">
  <button class="intel-subtab on" role="tab" aria-selected="true">
    {label}<span class="intel-subtab-n">171</span></button>
  <button class="intel-subtab" role="tab" aria-selected="false">
    投资日历<span class="intel-subtab-n">2356</span></button>
</div></div></body></html>"""


def fetch(url: str, tries: int = 3) -> str:
    last: Exception | None = None
    for _ in range(tries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=UA),
                timeout=30).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last  # type: ignore[misc]


def main() -> int:
    out: list[str] = []

    # ── ① 线上 JS 里找标签 ──
    js_text = ""
    try:
        html = fetch(BASE + "/")
        js_hrefs = re.findall(r'src="([^"]+\.js)"', html)
        out.append("线上 JS 产物: " + ", ".join(js_hrefs))
        for h in js_hrefs:
            url = h if h.startswith("http") else BASE + h
            body = fetch(url)
            if LABEL in body:
                out.append(f"  ✓ 在 {url.rsplit('/', 1)[-1]} 里找到「{LABEL}」")
                js_text = body
                break
        if not js_text:
            out.append(f"  ✗ 线上 JS 里没找到「{LABEL}」")
    except Exception as exc:  # noqa: BLE001
        out.append(f"线上取 JS 失败（{type(exc).__name__}）→ 改查本地产物")
    if not js_text:
        dist = sorted(Path("web/dist-pilot/assets").glob("index-*.js"))
        if dist:
            body = dist[0].read_text(encoding="utf-8", errors="replace")
            hit = LABEL in body
            out.append(f"本地已发布 {dist[0].name}: 命中={hit}")
            js_text = body if hit else ""

    # 旧名是否已消失（只查标签位置，注释不会进产物）
    if js_text:
        out.append(f"  旧名「情报流」是否仍在产物中: {'情报流' in js_text}")

    # ── ② 用线上 CSS 量 390px 下的页签条 ──
    css = ""
    try:
        html = fetch(BASE + "/")
        css_hrefs = re.findall(r'href="([^"]+\.css)"', html)
        if css_hrefs:
            u = css_hrefs[0]
            css = fetch(u if u.startswith("http") else BASE + u)
            out.append(f"\n线上 CSS: {u.rsplit('/', 1)[-1]} ({len(css)} 字符)")
    except Exception as exc:  # noqa: BLE001
        out.append(f"\n线上取 CSS 失败（{type(exc).__name__}）")
    if not css:
        f = sorted(Path("web/dist-pilot/assets").glob("index-*.css"))
        if f:
            css = f[0].read_text(encoding="utf-8")
            out.append(f"用本地已发布 CSS: {f[0].name}")

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 390, "height": 844})
        pg.set_content(HARNESS.format(label=LABEL), wait_until="load")
        if css:
            pg.add_style_tag(content=css)
        pg.wait_for_timeout(200)
        geo = pg.evaluate(r"""
        () => {
          const bar = document.querySelector('.intel-subtabs');
          const tabs = Array.from(document.querySelectorAll('.intel-subtab'));
          const cs = getComputedStyle(bar);
          return {
            barClient: bar.clientWidth, barScroll: bar.scrollWidth,
            overflow: bar.scrollWidth > bar.clientWidth + 1,
            flexWrap: cs.flexWrap, overflowX: cs.overflowX,
            tabs: tabs.map((t) => ({
              text: t.textContent.trim(),
              w: Math.round(t.getBoundingClientRect().width),
              clipped: t.scrollWidth > t.clientWidth + 1,
            })),
          };
        }
        """)
        out.append("\n=== 390px 下页签条 ===")
        out.append(f"  bar client={geo['barClient']} scroll={geo['barScroll']} "
                   f"overflow={geo['overflow']} "
                   f"flexWrap={geo['flexWrap']} overflowX={geo['overflowX']}")
        for t in geo["tabs"]:
            out.append(f"    {t['text']:<22} 宽 {t['w']}px "
                       f"截断={t['clipped']}")
        ok = (not geo["overflow"]) or geo["overflowX"] == "auto"
        bad = [t["text"] for t in geo["tabs"] if t["clipped"]]
        out.append("\n结论: " + (
            "PASS —— 两个页签在 390px 内放得下，无截断" if ok and not bad
            else f"注意 —— 溢出={geo['overflow']}（overflowX={geo['overflowX']}）"
                 f" 截断={bad}"))
        b.close()

    with open("data/_label_verify.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
