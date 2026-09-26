"""删掉先前**前置**的那段 `.cal-line` 块 —— 它被后面的原始定义覆盖了，
留着只会让后来的人以为那里才是生效的规则（CSS 里"哪条生效"已经够难查了）。
同时把窄屏的换行规则并进既有媒体查询。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
p = "web/src/intel.css"
s = open(p, encoding="utf-8").read()

start = s.index("/* ==================================================================\n   投资日历：一条 = 一行")
end = s.index(".cal-stock-code { color: var(--muted); font-size: 11.5px; }")
removed = s[start:end]
s = s[:start] + s[end:]

# 窄屏允许换行：挂到既有的 ≤720px 段里（`.heat-stocks` 那一条后面）
anchor = "  .heat-stock-plats { max-width: 100%; }\n"
if anchor not in s:
    raise SystemExit("找不到 ≤720px 媒体查询锚点")
addition = anchor + """
  /* 日历：窄屏一行确实放不下时**允许**换行。
     "尽可能不换行"不等于"强制不换行" —— 强制会把内容挤出容器，
     那正是用户投诉过的"错位/重叠"。标题同时恢复正常换行（省略号
     在手机上会把"规模以上工业企业利润"截成"规模以上…"，那更没用）。 */
  .cal-line { flex-wrap: wrap; row-gap: 3px; }
  .cal-title { white-space: normal; overflow: visible; }
  .cal-scope { flex-wrap: wrap; white-space: normal; }
"""
s = s.replace(anchor, addition, 1)

open(p, "w", encoding="utf-8").write(s)
print("removed", len(removed), "chars of dead CSS")
