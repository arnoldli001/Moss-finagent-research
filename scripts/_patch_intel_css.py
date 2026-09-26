"""把 intel.css 里旧的 `.heat-*` 统计块样式换成新的「平台热议」区块样式。

旧样式是给三个统计块（来源类型结构 / 按日分布 / 纯数字倾向分布）用的，
那些块已被用户点名删除，样式必须一起换掉 —— 否则后来的人会以为块还在。
"""
p = "web/src/intel.css"
s = open(p, encoding="utf-8").read()
start = s.index("/* ==================================================================\n   舆情热度监控")
end = s.index("/* ==================================================================\n   投资日历")

new = """/* ==================================================================
   平台热议（原「舆情热度监控」子页签，已并入情报流顶部）

   ⚠️ 这一段的样式是**重写**的。原来那些 `.heat-kpi` / `.heat-bar-*` /
   `.heat-tags` 是给三个统计块（来源类型结构 / 按日分布 / 纯数字倾向分布）
   用的 —— 那三个块已被用户点名删除："这没有意义，删除吧"、
   "需求是具体的内容，而不是统计数量"。
   留着它们的样式只会让后来的人以为那些块还在。
   ================================================================== */

.intel-heat {
  border: 1px solid var(--border);
  border-radius: var(--intel-radius);
  background: rgba(255,255,255,0.02);
  margin-bottom: var(--intel-gap);
  overflow: hidden;
}
.intel-heat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.intel-heat-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  background: none;
  border: 0;
  color: var(--text);
  font-size: 13.5px;
  cursor: pointer;
  padding: 2px 0;
  text-align: left;
  min-height: 32px;
}
.intel-heat-counts { display: flex; gap: 8px; flex-wrap: wrap; }
.intel-heat-counts span {
  font-size: 11.5px;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 8px;
  font-variant-numeric: tabular-nums;
}
.intel-heat-caret { color: var(--muted); font-size: 12px; }
.intel-heat-src { font-size: 11.5px; }
.intel-heat-body { padding: 10px 12px 12px; }
.intel-heat-none {
  font-size: 12.5px;
  color: var(--muted);
  line-height: 1.7;
  padding: 6px 0;
}
.intel-heat-sec { margin-bottom: 14px; }
.intel-heat-sec:last-of-type { margin-bottom: 8px; }
.intel-heat-sec h4 {
  margin: 0 0 7px;
  font-size: 12.5px;
  color: var(--text);
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.intel-heat-sec h4 .muted-text { font-size: 11px; font-weight: 400; }

/* ── 热议个股 ── */
.heat-stocks {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 6px;
}
.heat-stock {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: rgba(255,255,255,0.015);
  min-width: 0;
}
.heat-stock-main {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  background: none;
  border: 0;
  color: var(--text);
  padding: 7px 9px;
  cursor: pointer;
  text-align: left;
  min-width: 0;
  /* 触控目标：手机上不小于 36px（苹果 HIG 建议 44，但这里是密集列表，
     36 是"可点"与"放得下"的折中 —— 实测 44 会让一屏只剩 5 行） */
  min-height: 36px;
}
.heat-stock-name { font-size: 13px; font-weight: 600; white-space: nowrap; }
.heat-stock-code {
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.heat-stock-n {
  font-size: 11px;
  color: var(--accent);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0 6px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.heat-stock-plats {
  font-size: 11px;
  color: var(--muted);
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 46%;
}
.heat-stock-ev {
  border-top: 1px dashed var(--border);
  padding: 7px 9px 8px;
  font-size: 12px;
  line-height: 1.7;
}
.heat-stock-ev ul { margin: 0; padding-left: 16px; }
.heat-stock-ev li { color: var(--text); margin-bottom: 3px; }
.heat-stock-time { margin-top: 4px; font-size: 11px; }

/* ── 热议事件 ── */
.heat-topics { list-style: none; margin: 0; padding: 0; }
.heat-topic {
  border-left: 2px solid var(--border);
  padding: 5px 0 5px 10px;
  margin-bottom: 7px;
}
.heat-topic-title {
  font-size: 13px;
  color: var(--text);
  display: flex;
  align-items: baseline;
  gap: 7px;
  flex-wrap: wrap;
}
.heat-topic-detail { font-size: 12px; color: var(--muted); line-height: 1.65; margin-top: 2px; }
.heat-topic-rel { font-size: 11.5px; color: var(--accent); margin-top: 2px; }

/* ── 平台人气榜 ── */
.heat-rank { display: flex; flex-direction: column; gap: 3px; }
.heat-rank-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: rgba(255,255,255,0.012);
  min-width: 0;
  flex-wrap: wrap;
}
.heat-rank-name { font-weight: 600; white-space: nowrap; }
.heat-rank-code { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
/* 涨跌配色：**红涨绿跌**（A 股约定）—— 与 `.tone-tag` / `--intel-up` 同口径 */
.heat-rank-pct { font-variant-numeric: tabular-nums; }
.heat-rank-pct.up, .heat-rank-chg.up { color: var(--intel-up); }
.heat-rank-pct.down, .heat-rank-chg.down { color: var(--intel-down); }
.heat-rank-focus, .heat-rank-heat { color: var(--muted); font-variant-numeric: tabular-nums; }
.heat-rank-chg { font-variant-numeric: tabular-nums; font-size: 11px; }
.heat-rank-plat {
  margin-left: auto;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.intel-heat-note {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.65;
  border-top: 1px dashed var(--border);
  padding-top: 7px;
}
.intel-heat-gaps {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 11.5px;
  color: var(--muted);
  line-height: 1.7;
}

"""
open(p, "w", encoding="utf-8").write(s[:start] + new + s[end:])
print("replaced", end - start, "chars with", len(new))
