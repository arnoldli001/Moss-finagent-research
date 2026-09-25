# -*- coding: utf-8 -*-
"""演练授权到期邮件提醒（dry-run，不真发）。

用法：
    python scripts/check_token_expiry.py            # 真发（到阈值才发）
    python scripts/check_token_expiry.py --dry-run  # 只打印
    python scripts/check_token_expiry.py --force    # 忽略去重，强制发
    python scripts/check_token_expiry.py --simulate 6   # 假装 6 天前刷新
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.infrastructure.credentials import (  # noqa: E402
    STALE_AFTER_DAYS, WARN_AFTER_DAYS, ZSXQ_FILE, load, save, status,
)
from src.domain.intel.token_alerts import check_and_notify  # noqa: E402

CRED = ROOT / "data" / "credentials" / ZSXQ_FILE
REFRESH_CMD = (f"{ROOT}\\.venv\\Scripts\\python.exe "
               f"{ROOT}\\scripts\\zsxq_authorize.py")


def main() -> int:
    ap = argparse.ArgumentParser(description="数据源授权到期检查与提醒")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发")
    ap.add_argument("--force", action="store_true", help="忽略去重强制发")
    ap.add_argument("--simulate", type=float, default=None,
                    help="假装 N 天前刷新（临时改凭证时间戳，跑完恢复）")
    args = ap.parse_args()

    backup: str | None = None
    if args.simulate is not None:
        cred = load(ZSXQ_FILE, root=ROOT)
        if cred is None:
            print("[x] 没有凭证可模拟，请先跑 zsxq_authorize.py")
            return 1
        backup = CRED.read_text(encoding="utf-8")
        fake = (datetime.now(timezone.utc) - timedelta(days=args.simulate)
                ).astimezone().isoformat(timespec="microseconds")
        CRED.write_text(json.dumps({"value": cred.value,
                                    "refreshed_at": fake,
                                    "note": "simulate"}), encoding="utf-8")
        print(f"[模拟] 假装 {args.simulate} 天前刷新\n")

    try:
        st = status(ZSXQ_FILE, root=ROOT)
        print("■ 当前状态")
        print(f"  state={st['state']}  age={st['age_days']}天  "
              f"来源={st['source'] or '(无)'}  刷新于={st['refreshed_at'] or '(未知)'}")
        print(f"  阈值：{WARN_AFTER_DAYS} 天起提醒，{STALE_AFTER_DAYS} 天起告警\n")

        res = asyncio.run(check_and_notify(
            root=ROOT, dry_run=args.dry_run, refresh_cmd=REFRESH_CMD,
            force=args.force))

        print("\n■ 本次结果")
        for k, v in res.items():
            print(f"  {k} = {v}")
    finally:
        if backup is not None:
            CRED.write_text(backup, encoding="utf-8")
            print("\n[恢复] 凭证时间戳已还原")

    return 0


if __name__ == "__main__":
    sys.exit(main())
