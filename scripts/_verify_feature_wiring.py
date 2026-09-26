"""验证功能权限改动后的**三处一致性**。

## 为什么必须有这个校验

同一个功能 key 写在三个地方，任一处漏改都会产生**静默故障**：

    platform/config.py  FEATURES      权限矩阵渲染什么、能不能勾
    my_features.py      VIEW_FEATURE  页签可见性
    intel.py            FEATURE_*     接口 403 判据

漏改表现各不相同，而且都不报错：
  · 只改 FEATURES 不改 intel.py → 情报流/日历/热榜**全体 403**（含管理员）
  · 只改 intel.py 不改 FEATURES → 矩阵里勾了也没用
  · 漏改 my_features.py → 页签不出现，用户以为功能没了
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.api.routes.intel import FEATURE_ALERTS, FEATURE_HOT
from src.api.routes.my_features import (
    ADMIN_ONLY_VIEWS,
    VIEW_FEATURE,
    feature_enabled_for_tier,
)
from src.domain.platform.config import FEATURES, get_platform_config

fails: list[str] = []

print("=== FEATURES（权限矩阵里的可售卖项）===")
for k, v in FEATURES.items():
    print(f"  {k:<16} {v}")
for gone in ("scheduler", "metrics", "intel.radar", "intel.brief"):
    if gone in FEATURES:
        fails.append(f"{gone} 仍在 FEATURES 里（不该显示在权限矩阵）")
print("  已移出:", [k for k in ("scheduler", "metrics", "intel.radar",
                              "intel.brief") if k not in FEATURES])
print("  新增:", [k for k in FEATURES if k.startswith("intel.")])

print("\n=== ADMIN_ONLY_VIEWS（写死管理员专属）===")
print("  ", sorted(ADMIN_ONLY_VIEWS))
for need in ("scheduler", "metrics"):
    if need not in ADMIN_ONLY_VIEWS:
        fails.append(f"{need} 不在 ADMIN_ONLY_VIEWS（非管理员会拿到页签）")

print("\n=== VIEW_FEATURE（页签 → 功能）===")
for view, feat in VIEW_FEATURE.items():
    mark = "（管理员专属）" if view in ADMIN_ONLY_VIEWS else ""
    ok = feat in FEATURES or view in ADMIN_ONLY_VIEWS
    print(f"  {view:<16} → {feat:<14}{mark}" + ("" if ok else "   ← 指向不存在的 feature"))
    if not ok:
        fails.append(f"VIEW_FEATURE[{view}] = {feat} 不在 FEATURES 里")
for need in ("intel-hot", "intel-calendar"):
    if need not in VIEW_FEATURE:
        fails.append(f"缺页签 {need}")
if "intel" in VIEW_FEATURE:
    fails.append("旧页签 `intel` 仍在 VIEW_FEATURE 里（一级目录已删除）")

print("\n=== intel.py 的接口判据 ===")
print("  FEATURE_HOT   =", FEATURE_HOT)
print("  FEATURE_ALERTS=", FEATURE_ALERTS)
if FEATURE_HOT not in FEATURES:
    fails.append("FEATURE_HOT 不在 FEATURES → 情报接口会对所有人 403")
if FEATURE_ALERTS not in FEATURES:
    fails.append("FEATURE_ALERTS 不在 FEATURES")

print("\n=== 各等级实际生效 ===")
cfg = get_platform_config()
for tier in ("admin", "vip", "trial"):
    p = cfg.plan(tier)
    en = {k: bool(p.features.get(k, False)) for k in FEATURES}
    print(f"  {tier:<6} intel.hot={en.get('intel.hot')} "
          f"intel.alerts={en.get('intel.alerts')} "
          f"research={en.get('research')}")
    for f in ("scheduler", "metrics"):
        if feature_enabled_for_tier(tier, f) and tier != "admin":
            # JSON 里 vip/trial 是 false，这里应为 False；若为 True 说明有人改了默认值
            print(f"         ⚠️ {tier} 的 {f} 在 JSON 里是 true（页签仍不下发，"
                  "因为 ADMIN_ONLY_VIEWS 是规则）")

print()
if fails:
    print("结论: FAIL")
    for f in fails:
        print("   ✗", f)
    sys.exit(1)
print("结论: PASS —— 三处一致，旧 key 已清理，管理员专属已写死")
