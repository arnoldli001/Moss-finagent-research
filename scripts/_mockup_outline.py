"""把 `docs/_mockup_intel_center.html` 的结构抽成大纲，用于和当前实现比对。

为什么要这个脚本：mockup 是 88KB 的单文件 HTML，直接读会占满上下文；
而这里真正需要的只是"它定义了哪些区块、哪些导航项、哪些字段"。
"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

src = open("docs/_mockup_intel_center.html", encoding="utf-8").read()
out: list[str] = []
out.append(f"行数 {src.count(chr(10)) + 1} | 字符数 {len(src)}")

m = re.search(r"<title>(.*?)</title>", src, re.S)
out.append("\n=== title ===")
out.append("  " + (m.group(1).strip() if m else "(无)"))

out.append("\n=== 类名里带 nav/sidebar/tab 的容器内文本 ===")
seen: set[str] = set()
for m in re.finditer(
        r'class="[^"]*(?:nav|sidebar|tab)[^"]*"[^>]*>(.{0,400}?)</', src, re.S):
    for t in re.findall(r">([^<>{}]{2,30})<", m.group(1)):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append("  - " + t[:60])

out.append("\n=== 所有标题 h1~h4 ===")
for tag in ("h1", "h2", "h3", "h4"):
    for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", src, re.S):
        t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if t:
            out.append(f"  [{tag}] {t[:80]}")

out.append("\n=== 所有 class 定义（去重，前 120 个）===")
classes: list[str] = []
for m in re.finditer(r'class="([^"]+)"', src):
    for c in m.group(1).split():
        if c not in classes:
            classes.append(c)
out.append("  " + " ".join(classes[:120]))
out.append(f"  （共 {len(classes)} 个类名）")

out.append("\n=== 内联注释里的说明（<!-- ... -->，前 40 条）===")
for i, m in enumerate(re.finditer(r"<!--(.*?)-->", src, re.S)):
    if i >= 40:
        break
    t = " ".join(m.group(1).split())
    if t:
        out.append("  · " + t[:120])

with open("data/_mockup_outline.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(out))
print("written data/_mockup_outline.txt", len(out), "lines")
