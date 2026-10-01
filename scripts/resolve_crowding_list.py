"""把用户给的「拥挤度功能里删除」清单解析、并逐条核对到 `sector_crowding_list`。

## 为什么先核对再落库

用户这份清单是**拥挤度口径**（不是主线池口径），名字来自拥挤度看板，
里面混着 GICS 式行业名（航空货运与物流 / 客运航空公司 / 百货业…）与
同花顺指数名（同花顺果指数）。原样比对会踩三类坑：

1. **错别字**：`油门螺旋杆菌` —— 业界通用写法是「幽门螺杆菌」；
2. **同一概念两个名**：`新股与次新股` 与 `次新股`；
3. **名字记不全**：`226中报预增`（年份？）、`同花顺果指数`、`视频添加剂`。

**一律不猜**：精确匹配命中的才落库，其余原样报出来让用户确认。
按名字模糊匹配到错的那个，症状是"删错了板块而清单看上去执行成功"。

只读，不写库。用法：
    python scripts/resolve_crowding_list.py
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from difflib import get_close_matches
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ★ `CHG-0143`：拥挤度的**用户配置**表（`sector_crowding_list` 等）已从共享
#   遗留主库迁到**本环境应用库**（`app_db`：dev → `data/dev/moss_dev.db`、
#   pilot → `data/pilot/moss_pilot.db`）。所以路径**不能再写死**
#   `data/moss_finagent.db` —— 那会读到旧库那份、而写入静默落到没人看的地方。
#   统一走 registry 解析（与 `sector_crowding/config.py` 同一个来源）：
#      `SECTOR_CROWDING_DB=<路径>` 可显式覆盖（离线演练/临时副本用）。
import os as _os
from src.sector_crowding.config import load_config as _load_crowding_config
_CROWDING_CONFIG_DB = _os.environ.get("SECTOR_CROWDING_DB") or str(
    _load_crowding_config().config_db_path)



RAW = """
粤港澳大湾区、股权转让（并购重组）、京津冀一体化、中俄贸易概念、肝炎概念、POE胶膜、
国企改革、AIGC概念、摘帽、蓝筹地产股、生物医药B类、网红经济、自由贸易港、新股与次新股、
航空货运与物流、两会概念、海上运输、油门螺旋杆菌、次新股、互联网出海、紫光系、
客运航空公司、天津自贸区、雄安概念股、纺织服装与奢侈品、饮料、牙科医疗、苹果概念、
现代服务业、医疗保健技术、钛白粉概念、生物质能发电、举牌、特斯拉、信息安全、视频添加剂、
资本市场、SaaS概念、物联网、京东概念、数字经济、个人护理产品、证金持股、金融科技、烟草、
移动互联网、建筑材料、大数据、智慧城市、创投概念、腾讯概念、内地房地产、彩票概念股、
纸业股、商品服务与用品、百货业、同花顺果指数、226中报预增
"""


def parse(raw: str) -> list[str]:
    text = raw.replace("、", "\n").strip()
    return [line.strip() for line in text.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="核对拥挤度删除清单")
    parser.add_argument("--apply", action="store_true", help="（本脚本只读，保留参数位）")
    args = parser.parse_args()

    names = parse(RAW)
    conn = sqlite3.connect(f"file:{_CROWDING_CONFIG_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    listed = {str(r["sector_name"]): dict(r) for r in conn.execute(
        "SELECT sector_code, sector_name, visible FROM sector_crowding_list")}
    conn.close()

    print(f"清单 {len(names)} 项（原文有重复写法）；"
          f"`sector_crowding_list` 共 {len(listed)} 个板块\n")
    hit, miss = [], []
    for name in names:
        key = re.sub(r"[（(].*?[)）]", "", name).strip()
        if name in listed:
            hit.append((name, listed[name]))
        elif key in listed:
            hit.append((key, listed[key]))
        else:
            miss.append(name)

    print(f"=== 精确命中 {len(hit)} 项 ===")
    for name, row in hit:
        flag = "" if int(row["visible"]) == 1 else "（已隐藏）"
        print(f"  {row['sector_code']:<12}{name}{flag}")
    print()
    print(f"=== ⚠️ 没命中 {len(miss)} 项（不猜，请确认）===")
    for name in miss:
        near = get_close_matches(name, list(listed), n=3, cutoff=0.5)
        print(f"  「{name}」"
              + (f"  → 最像的：{near}" if near else "  → 没有相近的名字"))
    codes = sorted({str(row["sector_code"]) for _n, row in hit})
    print()
    print(f"命中的代码清单（{len(codes)} 个，可直接粘进配置）：")
    print("  " + "  ".join(f'"{c}"' for c in codes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
