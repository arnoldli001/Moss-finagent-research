"""收尾 IntelPanel：删掉只为子页签存在的 `SubTab` / `SUB_TABS`，
并把剩下的 `tab === ...` 分支改成 `section === ...`。

⚠️ 为什么要脚本：`SUB_TABS` 是一段多行常量，用 edit 工具要贴很长一段
原文；而这里只需按结构删除，用正则在**锚点唯一**的前提下更稳。
脚本末尾自带断言 —— 改完仍有残留就直接报出来，不会静默留半截。
"""
import re

p = "web/src/components/intel/IntelPanel.tsx"
s = open(p, encoding="utf-8").read()
before = s

# 1) 类型别名（只服务子页签）
s = re.sub(r'type SubTab = "feed" \| "calendar";\n\n?', "", s, count=1)

# 2) SUB_TABS 常量（含其上方注释块，直到 `];` 与一个空行）
s = re.sub(r"(?:/\*\*[\s\S]*?\*/\n)?const SUB_TABS:[\s\S]*?\n\];\n\n?", "",
           s, count=1)

# 3) 剩余分支
s = s.replace('{tab === "feed" && (', '{section === "hot" && (')
s = s.replace('{tab === "calendar" && (', '{section === "calendar" && (')

open(p, "w", encoding="utf-8").write(s)

left = {
    "SubTab": s.count("SubTab"),
    "SUB_TABS": s.count("SUB_TABS"),
    "tab ===": len(re.findall(r"\btab ===", s)),
    "setTab": s.count("setTab"),
    "tab,": len(re.findall(r"\btab,", s)),
}
print("长度", len(before), "→", len(s))
for k, v in left.items():
    print(f"  残留 {k:<10} = {v}" + ("   ← 需处理" if v else "   OK"))
