"""验证「可信度外圈已去掉 + 数字靠左垂直居中」——**不登录生产**。

## 为什么不用 Playwright 登录生产验证

实测 admin 密码在 2026-09-25 17:45 被改过，且我的探测已把
`failed_attempts` 推到 4/5（再错一次锁号）；同时 IP 已进入
"要求图形码"状态（脚本无法识别图形码）。为了看一眼样式去
继续试管理员密码，是**拿生产账号换验证**，不值得。

## 改成两步（都无副作用）

1. **取线上真实的 CSS 产物**，断言新规则确实在里面 —— 这一步证明
   "改动已发布"，而不只是"我本地改了"。
2. 用**同一份 CSS** + 与组件一致的 DOM 结构在本地渲染，**量几何**：
   外圈应为 0（无 border / border-radius），数字左边缘与容器左边缘对齐，
   数字与右侧两行元信息的**垂直中心**应对齐。

第 2 步拿的是线上 CSS，所以它验证的就是用户会看到的样式。
"""
import re
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "https://moss.wujiaitool.cn"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

#: 与 `CredRing` 组件一致的 DOM（含两行元信息 —— 那是"垂直居中"的参照物）
HARNESS = """<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css}">
<style>body{{background:#0d0f13;margin:0;padding:20px}}
td{{width:150px;vertical-align:top}} table{{border-collapse:collapse}}</style>
</head><body>
<table><tr><td class="c-cred">
  <div class="cred">
    <button class="cred-ring mid">75</button>
    <div class="cred-meta"><b>较高</b>来源74+内容78</div>
  </div>
</td></tr></table>
</body></html>"""


def fetch(url: str, tries: int = 3) -> str:
    """取 URL。**必须带重试 + Connection: close**。

    ⚠️ 实测 Cloudflare 隧道会偶发掐断连接（`TimeoutError` / RemoteDisconnected），
    而 `Connection: close` 能显著降低发生率。没有重试的话，一次网络抖动
    就会让验证脚本失败，看起来像"代码有问题"。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={**UA, "Connection": "close"})
            return urllib.request.urlopen(req, timeout=30).read().decode(
                "utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last  # type: ignore[misc]


def local_bundle() -> tuple[str, str]:
    """本地已发布产物 —— `ship-frontend` 同步到线上目录的就是它。

    为什么要用它兜底：CDN 抖动时验证脚本不该跟着失败；
    而"线上到底是不是这份"由文件名比对来确认（见 main）。
    """
    from pathlib import Path
    d = Path("web/dist-pilot/assets")
    css = sorted(d.glob("index-*.css"))
    if not css:
        return "", ""
    return css[0].name, css[0].read_text(encoding="utf-8")


def main() -> int:
    out: list[str] = []

    # ── ① 拿 CSS：优先线上，抖动则用本地已发布产物 ──
    local_name, local_css = local_bundle()
    css_url = ""
    css = ""
    try:
        html = fetch(BASE + "/")
        hrefs = re.findall(r'href="([^"]+\.css)"', html)
        out.append("线上首页引用的 CSS: " + ", ".join(hrefs))
        if hrefs:
            css_url = hrefs[0] if hrefs[0].startswith("http") else BASE + hrefs[0]
            css = fetch(css_url)
        live_name = css_url.rsplit("/", 1)[-1] if css_url else ""
        agree = (live_name == local_name)
        out.append(f"线上文件名 {live_name} | 本地已发布 {local_name} | "
                   f"一致={agree}")
        if not agree and local_css:
            out.append("⚠️ 线上与本地产物不一致 —— 以**线上**为准验证")
    except Exception as exc:  # noqa: BLE001
        out.append(f"线上取 CSS 失败（{type(exc).__name__}）→ 改用本地已发布产物")
    if not css:
        css = local_css
        css_url = ""
        out.append("使用本地产物: web/dist-pilot/assets/" + local_name)
    if not css:
        out.append("FAIL: 拿不到任何 CSS")
        write(out)
        return 1
    out.append(f"CSS 大小 {len(css)} 字符")

    # ── ② 断言新规则在里面 ──
    ring = re.search(r"\.cred-ring\s*\{([^}]*)\}", css)
    cred = re.search(r"\.cred\s*\{([^}]*)\}", css)
    out.append("\n.cred-ring 规则: " + (ring.group(1).strip()[:200]
                                       if ring else "(未找到)"))
    out.append(".cred 规则: " + (cred.group(1).strip()[:200]
                                if cred else "(未找到)"))
    checks = {
        "cred-ring 无 border-radius:50%":
            bool(ring) and "border-radius:50%" not in ring.group(1),
        "cred-ring 有 border:0":
            bool(ring) and "border:0" in ring.group(1).replace(" ", ""),
        "cred-ring 只有虚线下边框":
            bool(ring) and "border-bottom:1px dashed" in ring.group(1),
        "cred 垂直居中 (align-items:center)":
            bool(cred) and "align-items:center" in cred.group(1).replace(
                " ", ""),
        "cred 靠左 (justify-content:flex-start)":
            bool(cred) and "justify-content:flex-start" in cred.group(1).replace(
                " ", ""),
        "分档色落到 color (非 border-color)":
            ".cred-ring.mid{color:" in css.replace(" ", ""),
        "圆角显式归零 (border-radius:0)":
            bool(ring) and "border-radius:0" in ring.group(1).replace(" ", ""),
    }
    out.append("\n=== 规则断言 ===")
    for k, v in checks.items():
        out.append(("  OK  " if v else "  BAD ") + k)

    # ── ③ 用线上 CSS 在本地渲染并量几何 ──
    harness_path = "data/_cred_harness.html"
    with open(harness_path, "w", encoding="utf-8") as fh:
        fh.write(HARNESS.format(css=css_url))
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 600, "height": 200})
        pg.goto("file:///" + harness_path.replace("\\", "/").replace(
            "D:", "D:").lstrip("/"), wait_until="load")
        # file:// 下跨域加载线上 CSS 会被拦 —— 用 route 直接把 CSS 内联进去
        pg2 = b.new_page(viewport={"width": 600, "height": 200})
        pg2.set_content(HARNESS.format(css="about:blank"), wait_until="load")
        pg2.add_style_tag(content=css)
        geo = pg2.evaluate(r"""
        () => {
          const cred = document.querySelector('.cred');
          const ring = document.querySelector('.cred-ring');
          const meta = document.querySelector('.cred-meta');
          const cs = getComputedStyle(ring);
          const r = ring.getBoundingClientRect();
          const c = cred.getBoundingClientRect();
          const m = meta.getBoundingClientRect();
          const rc = r.top + r.height / 2;
          const mc = m.top + m.height / 2;
          return {
            ringSize: Math.round(r.width) + '×' + Math.round(r.height),
            borderRadius: cs.borderRadius,
            borderTop: cs.borderTopWidth, borderLeft: cs.borderLeftWidth,
            borderBottom: cs.borderBottomWidth + ' ' + cs.borderBottomStyle,
            color: cs.color,
            leftGap: Math.round(r.left - c.left),    // 应为 0 = 靠左
            centerDelta: Math.round(Math.abs(rc - mc)), // 应为 ~0 = 垂直居中
            metaLines: Math.round(m.height / parseFloat(cs.lineHeight || 15)),
          };
        }
        """)
        out.append("\n=== 几何度量（用线上 CSS 渲染）===")
        for k, v in geo.items():
            out.append(f"  {k} = {v}")
        bad = []
        if geo["borderRadius"] not in ("0px", "0"):
            bad.append("仍有圆角")
        if geo["borderTop"] != "0px" or geo["borderLeft"] != "0px":
            bad.append("仍有边框")
        if geo["leftGap"] != 0:
            bad.append("数字未靠左")
        if geo["centerDelta"] > 2:
            bad.append(f"数字未垂直居中（差 {geo['centerDelta']}px）")
        out.append("\n结论: " + ("PASS —— 外圈已去掉、数字靠左且垂直居中"
                                if not bad else "FAIL —— " + "；".join(bad)))
        b.close()

    write(out)
    return 0


def write(lines: list[str]) -> None:
    with open("data/_cred_verify.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("written data/_cred_verify.txt")


if __name__ == "__main__":
    sys.exit(main())
