"""投资日历：把每条的"一行装完"落到 CSS 上。

用户口径（2026-09-25）："投资日历的每条信息内容尽可能不换行，写成1行
（详细展开的除外）"。

改法是两件事配合：
  1. JSX 侧把指标/覆盖范围/改期记录都收进 `.cal-line`（已改）；
  2. CSS 侧让 `.cal-line` 成为**不换行的行内容器**，各子元素
     `flex-shrink: 0` 保持自身宽度，只有标题可压缩（超长省略 + tooltip）。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
p = "web/src/intel.css"
s = open(p, encoding="utf-8").read()

anchor = ".cal-stock-code { color: var(--muted); font-size: 11.5px; }"
if anchor not in s:
    raise SystemExit("找不到锚点")

new = """/* ==================================================================
   投资日历：一条 = 一行
   ==================================================================
   用户口径（2026-09-25）："投资日历的每条信息内容尽可能不换行，
   写成1行（详细展开的除外）"。

   实现要点：`.cal-line` 是**不换行**的 flex 行，右侧的指标块
   （`.cal-metrics` / `.cal-scope` / `.cal-changes`）都是行内子元素，
   而不是各自占一行的块级元素。唯一允许被压缩的是标题 ——
   它超长时省略号收尾并挂 `title`，鼠标悬停看全文。

   ⚠️ 标题**不能**无限制压缩：`flex: 1 1 auto` + `min-width: 0` 让它
   成为唯一的弹性项，其余项 `flex: 0 0 auto` 保持可读宽度。
   ================================================================== */

.cal-line { flex-wrap: nowrap; min-width: 0; }
.cal-line > * { flex: 0 0 auto; }
.cal-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 指标区：原来是块级（第二行），现在与标题并排 */
.cal-metrics {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: nowrap;
  min-width: 0;
}
.cal-nums { display: flex; align-items: baseline; gap: 10px; flex-wrap: nowrap; }
.cal-num { display: flex; align-items: baseline; gap: 4px; }
.cal-srcdate, .cal-note {
  font-size: 11px;
  white-space: nowrap;
}
.cal-note { color: var(--muted); cursor: help; }

/* 覆盖范围 / 改期记录：同样行内 */
.cal-scope { flex-wrap: nowrap; white-space: nowrap; }
.cal-changes { display: flex; gap: 6px; flex-wrap: nowrap; white-space: nowrap; }

/* 窄屏：一行确实放不下时**允许**换行（"尽可能"而非"强制"）——
   强制 nowrap 会把内容挤出容器，那正是用户投诉过的"错位/重叠"。 */
@media (max-width: 720px) {
  .cal-line { flex-wrap: wrap; row-gap: 3px; }
  .cal-title { white-space: normal; }
}

"""
s = s.replace(anchor, new + anchor, 1)
open(p, "w", encoding="utf-8").write(s)
print("ok")
