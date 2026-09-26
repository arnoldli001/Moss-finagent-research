"""把告警相关的展示词从「风险/机会」统一改成「利空/利多」。

底层枚举值（`risk` / `opportunity`）**不动** —— 数据库、筛选参数、
历史告警都用它，改枚举会牵动全部存量数据与链接。只改给人看的文案。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

EDITS = [
    ("web/src/components/AlertDetail.tsx",
     [("<label>风险分</label>", "<label>利空分</label>"),
      ("<label>机会分</label>", "<label>利多分</label>")]),
    ("web/src/components/AlertsPanel.tsx",
     [('<option value="risk">风险</option>',
       '<option value="risk">利空</option>'),
      ('<option value="opportunity">机会</option>',
       '<option value="opportunity">利多</option>'),
      ("当前仅站内实时推送；配置后风险分&gt;{settings.email.risk_min_score}\n"
       "          或机会分≥{settings.email.opp_min_score} 的告警将发送至",
       "当前仅站内实时推送；配置后利空分&gt;{settings.email.risk_min_score}\n"
       "          或利多分≥{settings.email.opp_min_score} 的告警将发送至"),
      ('{" · "}风险{Math.round(a.risk_score)}\n'
       '                  /机会{Math.round(a.opportunity_score)}',
       '{" · "}利空{Math.round(a.risk_score)}\n'
       '                  /利多{Math.round(a.opportunity_score)}')]),
]

for path, pairs in EDITS:
    s = open(path, encoding="utf-8").read()
    for old, new in pairs:
        if old not in s:
            print("MISS", path, "|", old[:40].replace("\n", " "))
            continue
        s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("ok", path)
