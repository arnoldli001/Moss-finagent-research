"""清掉响应式段里给旧统计块（`.heat-kpis` / `.heat-cols` / `.heat-val` /
`.heat-bars` / `.heat-fo`）留的死规则，换成新「平台热议」区块的响应式规则。

死规则不会报错，只会让后来的人以为那些统计块还在。
"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
p = "web/src/intel.css"
s = open(p, encoding="utf-8").read()

REPLACEMENTS = [
    # 平板 / 手机横屏段
    (
        "  .heat-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }\n"
        "  .heat-cols { grid-template-columns: minmax(0, 1fr); }\n",
        "  /* 热议个股：平板段收到两列，卡片才不至于窄到把来源平台截掉 */\n"
        "  .heat-stocks { grid-template-columns: repeat(2, minmax(0, 1fr)); }\n",
    ),
    # 手机竖屏段
    (
        "  .heat-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }\n",
        "  /* 热议个股：手机竖屏单列 —— 两列会把\"名称+提及+平台\"挤成两行 */\n"
        "  .heat-stocks { grid-template-columns: minmax(0, 1fr); }\n",
    ),
    (
        "  .heat-val { font-size: 18px; }\n",
        "  .intel-heat-body { padding: 9px 10px 10px; }\n",
    ),
    (
        "  .heat-bars li { grid-template-columns: 74px minmax(0, 1fr) 36px; font-size: 11.5px; }\n",
        "  .heat-stock-plats { max-width: 100%; }\n",
    ),
    # 更窄的手机段
    (
        "  .heat-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }\n",
        "  .heat-rank-row { gap: 6px; }\n",
    ),
    (
        "  .heat-val { font-size: 17px; }\n",
        "  .heat-stock-name { font-size: 12.5px; }\n",
    ),
    (
        "  .heat-fo { display: none; }\n",
        "  /* 极窄屏：来源平台换行显示，不要截断成\"东方财…\" */\n"
        "  .heat-stock-plats { display: none; }\n",
    ),
]

for old, new in REPLACEMENTS:
    if old not in s:
        print("MISS:", old.strip().split("\n")[0])
        continue
    s = s.replace(old, new, 1)

open(p, "w", encoding="utf-8").write(s)

dead = [l for l in s.split("\n")
        if re.search(r"\.heat-(kpis|cols|val|bars|fo|kpi|lab|card|hint|tags|todo|bar-|tone-sum)", l)]
print("剩余死规则：", len(dead))
for l in dead:
    print("   ", l.strip())
