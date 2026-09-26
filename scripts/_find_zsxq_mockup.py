"""在 D:\\code 下找「知识星球情报台」设计稿的源文件。

为什么要在**整个 D:\\code** 找而不是只在本仓库：用户点名的是
`Moss-finagent-research/docs/_mockup_zsxq_intel.html`，但那个文件不存在；
而截图里的设计（知识星球情报台 / 情绪模型 qwen3:8b / 行业热度 Top 6 /
与平台指标交叉验证）很可能是**另一个项目**（moss-finance-assistant）
或更早会话产出的稿子。所以按"内容"找，不按"文件名"找。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOTS = [r"D:\code"]
KEYS = ["知识星球情报台", "行业热度 Top", "与平台指标交叉验证",
        "情绪模型", "个股导向图谱", "提及个股"]
SKIP = ("\\.venv\\", "\\node_modules\\", "\\.git\\", "\\site-packages\\",
        "\\__pycache__\\", "\\dist\\", "\\dist-pilot\\")
EXTS = (".html", ".htm", ".tsx", ".jsx", ".vue", ".md")

hits: list[tuple[str, list[str]]] = []
scanned = 0
for root in ROOTS:
    for dirpath, dirnames, filenames in os.walk(root):
        if any(s.strip("\\") in dirpath for s in SKIP):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in (".venv", "node_modules", ".git",
                                    "site-packages", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(EXTS):
                continue
            path = os.path.join(dirpath, fn)
            scanned += 1
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            found = [k for k in KEYS if k in text]
            if found:
                hits.append((path, found))

print(f"扫描 {scanned} 个文件，命中 {len(hits)} 个\n")
for path, found in sorted(hits, key=lambda x: -len(x[1])):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    print(f"[{len(found)}/{len(KEYS)}] {path}  ({size} bytes)")
    print(f"          命中: {'、'.join(found)}")
