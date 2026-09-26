"""同步 `configs/platform_tiers.json` 的 feature/pricing 键。

改动（按用户口径 2026-09-25）：
  · `intel.radar` → `intel.hot`（改名，权限矩阵里叫「热点&研报小作文」）
  · 删除 `intel.brief`（「盘前简报」下线，FEATURES 里也没有了）
  · `scheduler` / `metrics` **保留**（用户明确说"可以不删除"）——
    它们已不被任何判据读取，留着只是历史痕迹

⚠️ 用 json 读写而不是正则/字符串替换：JSON 里少一个逗号就整份配置解析失败，
而那份配置是**鉴权判据**（解析失败会回退出厂默认 → 所有等级权限重置）。
json.loads 成功 + 键集合断言，比"看着对"可靠。
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
p = "configs/platform_tiers.json"
d = json.load(open(p, encoding="utf-8"))

RENAMES = {"intel.radar": "intel.hot"}
DROP = {"intel.brief"}

for tier, plan in d["plans"].items():
    for block in ("features", "pricing"):
        src = plan.get(block)
        if not isinstance(src, dict):
            continue
        out: dict = {}
        for k, v in src.items():
            if k in DROP:
                continue
            out[RENAMES.get(k, k)] = v
        plan[block] = out

json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
open(p, "a", encoding="utf-8").write("\n")

# 复查
d2 = json.load(open(p, encoding="utf-8"))
print("=== 各等级 features ===")
for tier, plan in d2["plans"].items():
    print(f"  {tier:<6} {list(plan['features'].keys())}")
    print(f"          pricing 键数 {len(plan['pricing'])}")
raw = open(p, encoding="utf-8").read()
print()
print("残留 intel.brief:", raw.count("intel.brief"),
      "| 残留 intel.radar:", raw.count("intel.radar"),
      "| intel.hot 出现:", raw.count("intel.hot"))
print("scheduler/metrics 是否保留:",
      all(k in d2["plans"]["admin"]["features"] for k in ("scheduler", "metrics")))
