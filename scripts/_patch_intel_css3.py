"""给「无明确倾向」收容组补样式（追加到 intel.css 的响应式段之前）。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
p = "web/src/intel.css"
s = open(p, encoding="utf-8").read()

anchor = "/* ==================================================================\n   响应式"
if anchor not in s:
    raise SystemExit("找不到响应式段的锚点")

block = """/* ==================================================================
   「无明确倾向」收容组
   ==================================================================
   用户口径（2026-09-25）："原文倾向未定的，可以聚合成一条，点开可查看。"

   折叠时**只占一行** —— 这是整个改动的意义所在：不折叠的话它是
   几十上百行宏观快讯，真正有方向的那几条会被淹掉。
   ================================================================== */

.intel-kind.group { border-color: rgba(139,152,165,0.45); color: var(--muted); }
.intel-kind.small { font-size: 10.5px; padding: 0 5px; }

.intel-group-row { background: rgba(255,255,255,0.012); }
.intel-group-row.open { background: rgba(255,255,255,0.028); }

.intel-group-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  background: none;
  border: 0;
  color: var(--text);
  padding: 0;
  cursor: pointer;
  text-align: left;
  font: inherit;
  min-height: 28px;
}
.intel-group-title { font-size: 13.5px; }
.intel-group-caret { color: var(--muted); font-size: 12px; }
.intel-group-n { font-variant-numeric: tabular-nums; }

.intel-group-expand > td {
  padding: 6px 10px 12px;
  background: rgba(255,255,255,0.012);
  border-top: 1px dashed var(--border);
}
.intel-group-empty { font-size: 12px; padding: 6px 0; }

.intel-group-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 460px;
  overflow-y: auto;
}
.intel-group-list > li {
  padding: 7px 2px;
  border-bottom: 1px dashed var(--border);
}
.intel-group-list > li:last-child { border-bottom: 0; }
.intel-group-li-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
  margin-bottom: 2px;
}
.intel-group-plat { color: var(--muted); }
.intel-group-cred {
  color: var(--accent);
  font-variant-numeric: tabular-nums;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0 5px;
}
.intel-group-tone { font-size: 11px; }
.intel-group-li-title { font-size: 12.5px; color: var(--text); line-height: 1.6; }
.intel-group-li-sum {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.65;
  margin-top: 2px;
}

/* 手机卡片形态：整卡可点，与普通卡片的观感区分开（虚线边） */
.intel-group-card { border-style: dashed; }
.intel-group-card .intel-group-toggle { width: 100%; }
.intel-group-card .intel-group-list { max-height: 380px; margin-top: 6px; }

"""
open(p, "w", encoding="utf-8").write(s.replace(anchor, block + anchor, 1))
print("ok")
