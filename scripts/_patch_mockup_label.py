"""效果图里的子页签名同步改成「热点&研报小作文」。

为什么用脚本而不是手改：`&` 在 HTML 里要写成 `&amp;`，在 JS 字符串里
要写 `&`，两处规则不同，手改容易漏掉一处导致页面上显示 `&amp;`。
"""
p = "docs/_mockup_appshell.html"
s = open(p, encoding="utf-8").read()

REPS = [
    # JS 字符串（sub_tabs 渲染进 HTML，所以这里也要用实体）
    ('sub_tabs: ["情报流 171", "投资日历 2356"],',
     'sub_tabs: ["热点&amp;研报小作文 171", "投资日历 2356"],'),
    # JSX 风格模板里的标题（同样是拼进 innerHTML，用实体）
    ('<div class="card-h">情报流 <span class="muted">最近 7 天',
     '<div class="card-h">热点&amp;研报小作文 <span class="muted">最近 7 天'),
    # 审计日志里的操作名（纯文本，直接写 &）
    ('["16:58", "testyang", "查看情报流", "filter=high", "ok"],',
     '["16:58", "testyang", "查看热点&研报小作文", "filter=high", "ok"],'),
]

for old, new in REPS:
    if old in s:
        s = s.replace(old, new, 1)
        print("ok  :", old[:46])
    else:
        print("MISS:", old[:46])

open(p, "w", encoding="utf-8").write(s)

# 复查：不该再有裸露的「情报流」标签（注释里的保留）
import re
left = [l for l in s.split("\n")
        if "情报流" in l and not l.strip().startswith(("*", "//", "/*"))]
print("\n仍含「情报流」的非注释行:", len(left))
for l in left:
    print("   ", l.strip()[:90])
