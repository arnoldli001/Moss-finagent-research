"""抽取 mockup 的导航与各页区块标题 —— 这是"实现没实现"的比对清单。"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

src = open("docs/_mockup_intel_center.html", encoding="utf-8").read()
out: list[str] = []


def clean(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s).split())


out.append("=== 侧边导航项（.side .nav 里的元素）===")
side = re.search(r'<[^>]*class="side".*?</aside>|<div class="side".*?</div>\s*<div class="main"',
                 src, re.S)
seg = side.group(0) if side else ""
if not seg:
    i = src.find('class="side"')
    seg = src[i:i + 3000] if i >= 0 else ""
for m in re.finditer(r"<[^>]*class=\"[^\"]*ic[^\"]*\"[^>]*>(.*?)</", seg, re.S):
    t = clean(m.group(1))
    if t:
        out.append("  - " + t[:50])
# 兜底：直接扫 side 段里的短文本
if len(out) == 1:
    for t in re.findall(r">([^<>{}]{2,24})<", seg):
        t = t.strip()
        if t and not t.startswith(("&", "#")):
            out.append("  - " + t[:50])

out.append("\n=== page-title（页面大标题）===")
for m in re.finditer(r'class="[^"]*page-title[^"]*"[^>]*>(.*?)</', src, re.S):
    out.append("  # " + clean(m.group(1))[:70])

out.append("\n=== sec-title（区块小标题）===")
for m in re.finditer(r'class="[^"]*sec-title[^"]*"[^>]*>(.*?)</', src, re.S):
    out.append("  · " + clean(m.group(1))[:70])

out.append("\n=== 子页签（.tabs .tab / .tb）===")
for m in re.finditer(r'class="[^"]*\b(?:tab|tb)\b[^"]*"[^>]*>(.*?)</', src, re.S):
    t = clean(m.group(1))
    if t:
        out.append("  ▸ " + t[:60])

out.append("\n=== card-h（卡片标题）===")
for m in re.finditer(r'class="[^"]*card-h[^"]*"[^>]*>(.*?)</', src, re.S):
    out.append("  · " + clean(m.group(1))[:70])

out.append("\n=== 表头 <th> ===")
for m in re.finditer(r"<th[^>]*>(.*?)</th>", src, re.S):
    out.append("  | " + clean(m.group(1))[:40])

out.append("\n=== 弹窗（popup）===")
for m in re.finditer(r'class="[^"]*popup-h[^"]*"[^>]*>(.*?)</', src, re.S):
    out.append("  ◻ " + clean(m.group(1))[:70])

with open("data/_mockup_sections.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(out))
print("written", len(out), "lines")
