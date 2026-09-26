"""验证：V2.2 的"反向维度"真的落到了落库分数上（实现正确性检查）。

离线实验预测新权重的 IC 会从 −0.0375 变成 +0.0800（H=20）。但离线实验是
**我自己写的合成逻辑**，与生产代码是两份实现 —— 必须验证生产代码算出来的
分数确实等于"反转后的加权平均"，而不是只挂了个 `reversed` 标记没生效。

判据（逐板块，全部必须成立）：
    落库的 six_dim ≈ Σ(w_i × (100 - s_i) for 反向维度) + Σ(w_i × s_i) 归一化
且 payload 里 trading/technical 的 `reversed` 必须是 true。

用法：.venv\\Scripts\\python.exe scripts\\verify_reverse_dims.py [日期]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN_DB = ROOT / "data" / "moss_finagent.db"


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else ""
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if not day:
        row = conn.execute(
            "SELECT MAX(trade_date) d FROM mainline_score").fetchone()
        day = str(row["d"])
    rows = conn.execute(
        "SELECT board_code, six_dim, payload FROM mainline_score"
        " WHERE trade_date = ? ORDER BY board_code LIMIT 40", (day,)).fetchall()
    print(f"日期 {day}，检查 {len(rows)} 个板块")

    bad = 0
    flagged = 0
    for row in rows:
        payload = json.loads(str(row["payload"] or "{}"))
        layer = next((item for item in (payload.get("layers") or [])
                      if item.get("key") == "six_dim"), None)
        if layer is None:
            continue
        total = 0.0
        usable = 0.0
        reverse_keys: list[str] = []
        for dim in (layer.get("dimensions") or []):
            if not dim.get("available"):
                continue
            weight = float(dim.get("weight") or 0.0)
            score = float(dim.get("score") or 0.0)
            if dim.get("reversed"):
                score = 100.0 - score
                reverse_keys.append(str(dim.get("key")))
            total += score * weight
            usable += weight
        if usable <= 0:
            continue
        expected = total / usable
        actual = float(row["six_dim"] or 0.0)
        if abs(expected - actual) > 0.05:
            bad += 1
            if bad <= 5:
                print(f"  ❌ {row['board_code']} 期望 {expected:.2f} "
                      f"实际 {actual:.2f}（差 {expected - actual:+.2f}）")
        if reverse_keys:
            flagged += 1
            if flagged == 1:
                print(f"  样本 {row['board_code']}：被标记为反向的维度 "
                      f"{reverse_keys}")
                for dim in (layer.get("dimensions") or []):
                    if dim.get("key") in reverse_keys:
                        print(f"     {dim['key']}: 自然分 {dim['score']} "
                              f"→ 有效分 {100 - float(dim['score']):.2f} "
                              f"权重 {dim['weight']}")

    print()
    if bad:
        print(f"❌ {bad}/{len(rows)} 个板块的 six_dim 与"
              "「反转后加权平均」不符 —— 实现有问题")
        return 1
    print(f"✅ 全部 {len(rows)} 个板块的 six_dim 都等于反转后的加权平均"
          f"（其中 {flagged} 个板块标了反向维度）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
