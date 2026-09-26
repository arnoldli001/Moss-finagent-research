"""验证 `.app` 左右留白从 5% 改成 3% —— 用**线上 CSS** 实测内容宽度。

## 为什么量"实际像素"而不是只看代码

`padding` 写的是 `%`，但这个类带 `zoom`，而 `zoom` 会参与布局计算
（`%` 相对包含块解析、`zoom` 再等比放大结果）。所以"改了 3% 到底多出多少像素"
**必须实测**，算不出来 —— 本项目在 `zoom` 上已经踩过一次
（`5vw` 被放大成 5.4%，与写 `5%` 的实测宽度差几十像素）。

判据：内容占宽应从 90% 提到 94%（各边多 2 个百分点）。
"""
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Connection": "close"}
VIEWPORTS = [1280, 1440, 1920, 2048]


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
    css_url = ""
    css = ""
    try:
        html = fetch(BASE + "/")
        hrefs = re.findall(r'href="([^"]+\.css)"', html)
        if hrefs:
            css_url = hrefs[0] if hrefs[0].startswith("http") else BASE + hrefs[0]
            css = fetch(css_url)
            out.append(f"线上 CSS: {css_url.rsplit('/', 1)[-1]} ({len(css)} 字符)")
    except Exception as exc:  # noqa: BLE001
        out.append(f"线上取 CSS 失败（{type(exc).__name__}）")
    if not css:
        f = sorted(Path("web/dist-pilot/assets").glob("index-*.css"))
        css = f[0].read_text(encoding="utf-8")
        out.append(f"用本地已发布 CSS: {f[0].name}")

    m = re.search(r"\.app\s*\{([^}]*)\}", css)
    body = m.group(1) if m else ""
    out.append(".app 规则: " + body[:200])
    pad = re.search(r"padding:([^;}]+)", body)
    out.append(f"解析到 padding = {pad.group(1).strip() if pad else '(无)'}")

    with sync_playwright() as p:
        b = p.chromium.launch()
        out.append("\n=== 各视口下的内容宽（含 zoom 后的实际像素）===")
        out.append(f"{'视口':>6} {'zoom':>6} {'内容宽':>8} {'左右留白':>10} "
                   f"{'占宽':>7}")
        for w in VIEWPORTS:
            pg = b.new_page(viewport={"width": w, "height": 900})
            pg.set_content('<!DOCTYPE html><html><body style="margin:0">'
                           '<div class="app" id="a"><div style="height:20px">'
                           '</div></div></body></html>')
            pg.add_style_tag(content=css)
            pg.wait_for_timeout(120)
            g = pg.evaluate(r"""
            () => {
              const a = document.getElementById('a');
              const cs = getComputedStyle(a);
              const r = a.getBoundingClientRect();
              const padL = parseFloat(cs.paddingLeft);
              const padR = parseFloat(cs.paddingRight);
              return {
                zoom: cs.zoom,
                // 内容区 = 盒子宽 − 左右内边距
                content: Math.round(r.width - padL - padR),
                padL: Math.round(padL), padR: Math.round(padR),
                boxW: Math.round(r.width),
                vw: window.innerWidth,
              };
            }
            """)
            pct = g["content"] / g["vw"] * 100
            out.append(f"{w:>6} {g['zoom']:>6} {g['content']:>8} "
                       f"{g['padL']:>4}+{g['padR']:<4} {pct:>6.1f}%")
            pg.close()
        b.close()

    out.append("\n期望：占宽 ≈94%（各边留 3%）。改前是 ≈90%（各边 5%）。")
    with open("data/_width_verify.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
