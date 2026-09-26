"""一轮「改池子/改成分股 → 重打分 → 对照 → 决定是否上线」的**离线编排**。

## 为什么需要它

主线挖掘的每一次改动（删板块、提纯成分股、调权重）都会**改变全部历史评分**
（分数里有横截面分位）。所以每轮都必须走同一条路，而且**不能压生产库**：

| 步骤 | 为什么必须这么做 |
|---|---|
| 1. 备份 | 用户的要求是「不要影响我原有的主线挖掘效果」。只有能一键还原，这句话才有意义 |
| 2. 复制库 | `--force` 会**先清空再重算**；压在生产库上 = ① 90 分钟写锁把用户的
手动刷新挤掉（实测已发生两次）② 期间界面是空的 |
| 3. 副本上重打分 | 生产库全程可读可写 |
| 4. 对照指标 | 拿**同一个副本**比，否则是跨库比，结论无效 |
| 5. 达标才换入 | 不达标就什么都不做 —— 生产库根本没被碰过 |

## 用法

    # 只跑到"对照"为止，不动生产库（默认，最安全）
    python scripts/offline_rescore_round.py --tag task1

    # 对照达标后再显式换入
    python scripts/offline_rescore_round.py --tag task1 --promote

⚠️ 本脚本**不做判断**：它把 before/after 两组指标打出来，是否达标由人（或调用方）
决定。自动化"效果更好就上线"需要先定义清楚"更好"，而这件事本身还在迭代中。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
MAIN_DB = ROOT / "data" / "moss_finagent.db"
COPY_DB = ROOT / "data" / "moss_finagent_rescore.db"


def run(label: str, argv: list[str], env: dict[str, str]) -> int:
    print(f"\n{'=' * 78}\n▶ {label}\n  {' '.join(argv)}\n{'=' * 78}", flush=True)
    started = time.monotonic()
    code = subprocess.call(argv, cwd=str(ROOT), env=env)
    print(f"◀ {label} 退出码 {code}（{time.monotonic() - started:.1f}s）", flush=True)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="离线重打分一轮（备份→副本→对照→换入）")
    parser.add_argument("--tag", required=True, help="还原点标签，如 task1")
    parser.add_argument("--start", default="", help="重打分起点（缺省用脚本默认 20221201）")
    parser.add_argument("--end", default="", help="重打分终点（缺省=本地最新交易日）")
    parser.add_argument("--promote", action="store_true",
                        help="对照之后**换入**生产库（缺省只对照，不动生产库）")
    parser.add_argument("--skip-backup", action="store_true",
                        help="跳过备份（仅当该 tag 的还原点已存在时）")
    parser.add_argument("--skip-rescore", action="store_true",
                        help="跳过重打分（副本里已有结果，只做对照/换入）")
    args = parser.parse_args()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MOSS_SQLITE_PATH"] = str(COPY_DB.relative_to(ROOT)).replace("\\", "/")

    # ---- 1. 备份生产库（结果表 + 打分的输入表）----
    if args.skip_backup:
        print("⏭ 跳过备份（--skip-backup）")
    else:
        code = run("备份生产库", [PY, "scripts/backup_mainline_results.py",
                                 "--tag", args.tag], env)
        if code != 0:
            print("❌ 备份失败 —— 中止（没有还原点就不动任何东西）")
            return code

    # ---- 2. 复制生产库 ----
    print(f"\n▶ 复制 {MAIN_DB.name} → {COPY_DB.name}（{MAIN_DB.stat().st_size / 2**20:.0f} MB）")
    shutil.copy2(MAIN_DB, COPY_DB)
    print("  完成")

    # ---- 3. 在副本上全量重打分 ----
    if args.skip_rescore:
        print("⏭ 跳过重打分（--skip-rescore）")
    else:
        argv = [PY, "scripts/rescore_mainline.py", "--force"]
        if args.start:
            argv += ["--start", args.start]
        if args.end:
            argv += ["--end", args.end]
        code = run("副本上全量重打分", argv, env)
        if code != 0:
            print("❌ 重打分失败 —— 生产库未被修改，可直接重跑")
            return code

    # ---- 4. 对照（必须读同一个副本）----
    argv = [PY, "scripts/compare_floor_rescore.py",
            "--before", f"mainline_score_bak_{args.tag}",
            "--after", "mainline_score",
            "--alert-before", f"mainline_alert_bak_{args.tag}",
            "--alert-after", "mainline_alert",
            "--allow-pool-change"]
    code = run("对照 before/after", argv, env)
    if code != 0:
        print("⚠️ 对照脚本非 0 退出，请先看它的输出再决定")

    if not args.promote:
        print(f"\n{'=' * 78}")
        print("ℹ️ 只跑到对照为止 —— **生产库没有被修改**。")
        print(f"   确认达标后换入：python scripts/offline_rescore_round.py "
              f"--tag {args.tag} --skip-backup --skip-rescore --promote")
        print(f"   不达标就什么都不用做（或还原 ml_member_pure："
              f"backup_mainline_results.py --restore {args.tag}）")
        print("=" * 78)
        return 0

    # ---- 5. 换入生产库（会先自动打 pre_promote 还原点）----
    return run("换入生产库", [PY, "scripts/backup_mainline_results.py",
                              "--promote", str(COPY_DB)], env)


if __name__ == "__main__":
    raise SystemExit(main())
