"""把拥挤度范围收窄到「262 个概念板块」，其余全部拉黑。

## 用户口径（2026-09-27）

> 数据源里存在的全部板块 1846 个……如果没有用就删除非 262 个概念板块的其他
> 概念板块，并且拉入黑名单，或者数据源获取时直接跳过，不再更新和计算这些。
> **注意后续用户手动新增的概念不在这 1846 个里面的，还是允许加入和跟随刷新的。**
> P2 全量刷新收窄，只需要 262 个概念板块即可，那个 554 板块都多余，全部拉黑。

## 安全性（动手前已核实）

* `sources.list_boards()`（1846 那个全量）**只被 `refresh.py` 内部调用 2 次**，
  模块外零消费 —— 收窄它没有副作用。
* `sector_crowding_list` 会被主线挖掘读（`mainline/datastore.py:1801`），
  但它用的是 **`sector_blacklist.yaml`**（第 1836 行写死），
  **不是**本脚本写的 `crowding_exclusions.yaml` —— 所以拉黑不会踢出主线池。
* `sector_crowding_daily` 模块外只有 `core/config.py` 与 `retention_passes.py`，
  都是保留策略，无业务消费。

## 「后续手动新增」怎么保证还能用

拉黑是**冻结快照**，不是动态规则：

* 新出现的概念板块**不在快照里** → `seed_list` 照常种进清单 → 照常刷新 ✅
* 用户显式新增一个**已在快照里**的板块 → `config_list_add` 会调
  `config.remove_crowding_exclusions()` 把它摘出去 ✅

用法：
    python scripts/narrow_crowding_scope.py            # 只报告
    python scripts/narrow_crowding_scope.py --apply    # 写进剔除清单
"""
from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
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


CONFIG = ROOT / "configs" / "crowding_exclusions.yaml"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENTRY_RE = re.compile(r'^\s*-\s*\{.*?code:\s*"([^"]+)".*?\}\s*$')


def parse_yaml(text: str) -> tuple[list[str], dict[str, str], list[str]]:
    """返回 (行列表, {code: 原始行}, 头部行)。"""
    lines = text.splitlines()
    entries: dict[str, str] = {}
    for line in lines:
        m = ENTRY_RE.match(line)
        if m:
            entries[m.group(1)] = line
    head = [line for line in lines if not ENTRY_RE.match(line)]
    return lines, entries, head


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(_CROWDING_CONFIG_DB), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    # 保留集 = 清单里「可见 + 概念」
    keep = {str(r["sector_code"]) for r in conn.execute(
        "SELECT l.sector_code FROM sector_crowding_list l "
        "LEFT JOIN sector_meta m ON m.sector_code=l.sector_code "
        "WHERE l.visible=1 AND COALESCE(m.is_concept,1)=1")}
    # 全部已知板块（数据源 + 清单的并集）
    allc = {str(r["sector_code"]): str(r["sector_name"] or "")
            for r in conn.execute("SELECT sector_code, sector_name FROM sector_meta")}
    for r in conn.execute("SELECT sector_code, sector_name FROM sector_crowding_list"):
        allc.setdefault(str(r["sector_code"]), str(r["sector_name"] or ""))
    conn.close()

    target = {c: n for c, n in allc.items() if c not in keep}
    L = [f"已知板块（sector_meta ∪ 清单）: {len(allc)}",
         f"保留集（可见 + 概念）        : {len(keep)}",
         f"拟拉黑                      : {len(target)}", ""]

    if not args.apply:
        L.append("（未加 --apply，只报告。抽样 10 个拟拉黑项：）")
        for c, n in list(target.items())[:10]:
            L.append(f"  {c:<12}{n}")
        print("\n".join(L))
        return 0

    text = CONFIG.read_text(encoding="utf-8")
    _, existing, _ = parse_yaml(text)
    added = 0
    body = text
    if not body.endswith("\n"):
        body += "\n"
    for code in sorted(target):
        if code in existing:
            continue
        body += (f'  - {{ code: "{code}", '
                 f'name: "{target[code]}", reason: "范围收窄" }}\n')
        added += 1
    # 同步 count
    total = len(ENTRY_RE.findall(body)) if False else sum(
        1 for line in body.splitlines() if ENTRY_RE.match(line))
    body = re.sub(r"^count: \d+$", f"count: {total}", body, flags=re.M)
    CONFIG.write_text(body, encoding="utf-8")

    L += [f"新增 {added} 条（原有 {len(existing)} 条）",
          f"剔除清单现在共 {total} 条",
          f"⚠️ 保留集里的 {len(keep)} 个板块**必须**没被拉黑"]
    leaked = sorted(keep & set(ENTRY_RE.findall(body)))
    L.append(f"   实际泄漏: {len(leaked)} 个" + (f" {leaked[:5]}" if leaked else " ✅"))
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
