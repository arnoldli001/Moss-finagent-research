"""生成冻结名单 = **基准池(138) − 官方门槛清单(16) = 122**。

## 为什么不用自己重算门槛

第一版脚本自己去算 `win_rate_20d <= 0.40`，只剔掉 7 个 —— 而项目里**已经有**
一份审过的门槛产物 `configs/mainline_theme_exclusions.yaml`（16 条），
由 `scripts/build_theme_exclusions.py` 按 `theme_gate.py` 的规则生成：

    剔除 = 20 日胜率 <= 40%  **且**  已走满的 20 日窗口 >= 3 个

自己重算漏掉了后半条（窗口数条件），所以少剔了 9 个。**复用官方清单，不重新推导。**

## 出处（用户说的 122）

`configs/mainline_theme_exclusions.yaml` 头部：

    # 本次：池内 138 个 → 剔除 16 个、留 122 个（其中 18 个在观察名单）。

本脚本就是把这句话固化成一份**冻结名单**。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"D:\code\Moss-finagent-research")
os.chdir(r"D:\code\Moss-finagent-research")

from src.mainline.config import load_theme_exclusions          # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path("data/_pool_base_B.json")      # 138（不叠拥挤度剔除清单）
OUT = Path("configs/mainline_frozen_pool.yaml")

base = set(json.loads(BASE.read_text(encoding="utf-8")))
gated = set(load_theme_exclusions("mainline_theme_exclusions.yaml") or ())
retained = sorted(base - gated)

conn = sqlite3.connect("file:data/moss_finagent.db?mode=ro", uri=True)
names = {str(r[0]): str(r[1] or "") for r in conn.execute(
    "SELECT sector_code, sector_name FROM sector_meta")}
conn.close()

today = datetime.now().astimezone().date().isoformat()
body = [
    "# 主线挖掘 · **冻结概念池**",
    "#",
    "# ⚠️ 本文件由 `scripts/build_frozen_pool_from_gate.py` **生成**，不要手改。",
    "#",
    "# ## 为什么冻结（用户口径 2026-09-27）",
    "#",
    "#   「主线板块所需计算的概念板块数量只有 122 个，不是 262 个，",
    "#     只是这 262 个里包含了这 122 个。」",
    "#   「根据一个板块历史告警的 20 日胜率 > 45% 还是 40% 我忘了，筛选出来的，",
    "#     你就把这当签的 122 个概念板块，**写死在主线挖掘的概念池里**，",
    "#     后续只关注这 122 个，**不会再有变动**。」",
    "#",
    "# ## 口径（= 复用官方门槛产物，不重新推导）",
    "#",
    "#   基准池     `import_crowding_pool` 的动态口径 → 138 个",
    "#   剔除清单   `configs/mainline_theme_exclusions.yaml` → 16 个",
    "#              规则：20 日胜率 <= 40% **且** 已走满窗口 >= 3 个",
    f"#   冻结名单   138 - 16 = {len(retained)} 个",
    "#",
    "# 出处：`mainline_theme_exclusions.yaml` 头部原文",
    "#     「本次：池内 138 个 → 剔除 16 个、留 122 个（其中 18 个在观察名单）」",
    "#",
    "# ⚠️ 第一版脚本自己重算门槛，只剔 7 个 → 算出 123。错在漏了",
    "#    「已走满窗口 >= 3 个」这个条件。**不要再自己重算。**",
    "#",
    f"generated_at: \"{today}\"",
    "version: 1",
    f"base_pool: {len(base)}",
    f"gated_out: {len(gated)}",
    f"count: {len(retained)}",
    "boards:",
]
for code in retained:
    body.append(f'  - {{ code: "{code}", name: "{names.get(code, "")}" }}')
OUT.write_text("\n".join(body) + "\n", encoding="utf-8")

print(f"基准池      : {len(base)}")
print(f"门槛剔除    : {len(gated)}（全部都在基准池内: {len(gated & base) == len(gated)}）")
print(f"冻结名单    : {len(retained)}")
print(f"已写入      : {OUT}")
