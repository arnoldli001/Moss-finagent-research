"""给解禁明细的表头补样式，并让它与数据行**共用同一套 grid 列宽**。

关键点：表头不能自己写一套 `grid-template-columns` —— 那样窄屏媒体查询
改了行、忘了改表头，两者就会错位（用户投诉的正是"错位 重叠"那一类）。
所以表头复用 `.cal-stocks li` 的列定义，只覆盖观感（字号/颜色/下边框）。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
p = "web/src/intel.css"
s = open(p, encoding="utf-8").read()

anchor = ".cal-stock-code { color: var(--muted); font-size: 11.5px; }"
if anchor not in s:
    raise SystemExit("找不到锚点")

new = """/* ── 解禁明细表头 ──
   用户口径（2026-09-25）："解禁股的展开明细当前点开没有表头，
   应该表头加上：编码 股票 解禁市值 占流通股比例 解禁类型"。

   ⚠️ 列宽**不在这里定义**：表头与数据行共用 `.cal-stocks li` 的
   `grid-template-columns`（下面用 `:extend` 式的同一条规则），
   否则窄屏媒体查询改了行、忘了改表头 → 两行错位。
   `.cal-stocks-head` 只是把同一个网格"长成表头的样子"。 */
.cal-stocks-wrap { min-width: 0; }
.cal-stocks-head {
  list-style: none;
  margin: 6px 0 0;
  padding: 6px 0 4px;
  border-top: 1px dashed var(--table-line);
  border-bottom: 1px solid var(--table-line);
  display: grid;
  grid-template-columns: 52px minmax(64px, 1fr) 76px 60px minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
  font-size: 11px;
  color: var(--muted);
  position: sticky;
  top: 0;
  /* 明细区自己滚动，表头必须**跟着滚** —— 用 sticky 钉在顶部，
     否则滚到第 20 行时又不知道哪列是什么了（那就白加表头了）。
     背景要实色：透明的话下面的行会从字缝里透出来。 */
  background: var(--panel, #14161a);
  z-index: 1;
}
.cal-stocks-head .cal-stock-cap,
.cal-stocks-head .cal-stock-pct { text-align: right; color: var(--muted); }
.cal-stocks-head .cal-stock-name,
.cal-stocks-head .cal-stock-type { color: var(--muted); font-size: 11px; }
/* 表头在滚动容器内：容器本身的 max-height 只作用于列表，
   所以这里把滚动交给外层包裹，让表头与列表一起滚。 */
.cal-stocks-wrap .cal-stocks { max-height: 250px; }

"""

s = s.replace(anchor, new + anchor, 1)
open(p, "w", encoding="utf-8").write(s)
print("ok")
