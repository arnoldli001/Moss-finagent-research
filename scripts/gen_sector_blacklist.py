"""一次性脚本：生成板块黑名单冻结清单 configs/sector_blacklist.yaml。

⚠️ 生成后即为**冻结**产物，正常情况下不应重复运行：
黑名单的语义是"冻结时点被有意排除的那批板块"，重新生成会把
之后新增的板块也一并写进去，从而违背"新增概念照常同步"的规则。
只有在你明确要重置基线时才跑它。

用法：python scripts/gen_sector_blacklist.py
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ★ `CHG-0143`：拥挤度的**用户配置**表（`sector_crowding_list` 等）已从共享
#   遗留主库迁到**本环境应用库**（`app_db`：dev → `data/dev/moss_dev.db`、
#   pilot → `data/pilot/moss_pilot.db`）。所以路径**不能再写死**
#   `data/moss_finagent.db`，也不能用 `settings.sqlite_path`
#   （那在主实例上等于遗留主库、判据会随环境漂移）。
#   统一走 registry 解析（与 `sector_crowding/config.py` 同一个来源）：
#      `SECTOR_CROWDING_DB=<路径>` 可显式覆盖（离线演练/临时副本用）。
import os as _os  # noqa: E402

from src.sector_crowding.config import load_config as _load_crowding_config  # noqa: E402

_CROWDING_CONFIG_DB = _os.environ.get("SECTOR_CROWDING_DB") or str(
    _load_crowding_config().config_db_path)


CUTOFF = "2026-09-20"
OUT = Path("configs/sector_blacklist.yaml")

connection = sqlite3.connect(
    f"file:{Path(_CROWDING_CONFIG_DB).as_posix()}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT sector_code, sector_name, visible, source FROM sector_crowding_list"
).fetchall()
connection.close()

visible = [r for r in rows if int(r["visible"]) == 1]
hidden = [(str(r["sector_code"]), str(r["sector_name"]))
          for r in rows if int(r["visible"]) == 0 and r["sector_code"]]

groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
for code, name in hidden:
    groups[code.split(".")[0][:3]].append((code, name))

# 表头用普通字符串拼接，避免任何格式化转义把 visible/board 这类词改坏
head = (
    "# 板块黑名单（冻结清单）\n"
    "#\n"
    "# ## 规则\n"
    "#\n"
    "# 板块池 = `sector_crowding_list` 全部板块 **减去** 本清单（再减去名称规则排除项）。\n"
    f"# 今天 {len(rows)} - {len(hidden)} = {len(visible)}，与当前 `visible=1` 的配置一致。\n"
    "#\n"
    "# **本清单是冻结的**：只登记 " + CUTOFF + " 时点 `visible=0` 的 "
    f"{len(hidden)} 个板块。\n"
    "# 之后新出现的概念板块**不在清单里，因此会自动入池并同步数据** ——\n"
    "# 这是刻意的：新概念正是要跟踪的对象，不能因为「默认隐藏」就被漏掉。\n"
    "#\n"
    "# ## 为什么用显式清单而不是 `visible = 0` 判断\n"
    "#\n"
    "# 用 `WHERE visible = 1` 选池有两个隐患，换成显式黑名单后都消失：\n"
    "# 1. 将来新增板块若默认 `visible=0`，会被静默漏掉且**不报错**；\n"
    "# 2. 看不出「这个板块是被有意排除的」还是「它只是还没同步」——\n"
    "#    两者的排查方向完全相反。\n"
    "#\n"
    "# 另外：黑名单文件读不到时，`import_crowding_pool` 会**回退**到\n"
    "# `visible = 1` 并在同步 message 里写明，而不是把空集合当成\n"
    "# 「不排除任何板块」（那会让池子无声地从 445 涨回 933）。\n"
    "#\n"
    "# ## 关键构成（实测）\n"
    "#\n"
)

for prefix in sorted(groups):
    if prefix == "865":
        note = " —— **整套已淘汰的同花顺概念体系**"
    elif prefix == "871":
        note = " —— GICS 式港股/美股行业分类"
    else:
        note = ""
    head += f"#   `{prefix}xxx`  {len(groups[prefix]):>3} 个{note}\n"

head += (
    "#\n"
    "# ⚠️ `865xxx` 与 `875xxx`/`885xxx` 存在**同名不同码**的换代关系\n"
    "# （如 `风电`、`苹果概念`）。黑名单只挡同步，不做名称去重 ——\n"
    "# 池内仍有同名的不同代码板块存在。\n"
    "#\n"
    "# ## 怎么改\n"
    "#\n"
    "# 恢复跟踪某个板块：把它的代码从下面删掉，再跑一次 `board_crowding` 同步。\n"
    "# 新增排除项：把代码加进来。\n"
    "# **不要**手工改 `sector_crowding_list.visible` —— 那只影响拥挤度界面，\n"
    "# 本清单才是主线挖掘的判据。\n"
    "#\n"
    'version: 1\n'
    f'frozen_at: "{CUTOFF}"\n'
    "codes:\n"
)

lines = [head]
for prefix in sorted(groups):
    lines.append(f"  # ---- {prefix}xxx（{len(groups[prefix])} 个）----\n")
    for code, name in sorted(groups[prefix]):
        safe = name.replace('"', "'")
        lines.append(f'  - {{ code: "{code}", name: "{safe}" }}\n')

OUT.write_text("".join(lines), encoding="utf-8", newline="\n")

raw = OUT.read_bytes()
bad = [(i, hex(b)) for i, b in enumerate(raw)
       if b < 0x20 and b not in (0x09, 0x0A)]
print(f"已写 {OUT}：{len(hidden)} 个代码，{len(raw)} bytes")
print("控制字符:", bad[:5] if bad else "无")
