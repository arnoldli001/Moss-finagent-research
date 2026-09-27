"""水位分母的**样本量偏差校正**标定（截断回测）。只读，不写库。

## 两个结论（都有数据）

**① 向同类中位数收缩（James-Stein 式）—— 一致变差，已放弃。**
`M_peer` 是横截面**中位数**，各板块的 `M_T` 与之相比有高有低。向中位数收缩
等于把高于中位的板块分母**调小** → 水位更高 → 偏差扩大
（k=0 系统性偏差 +0.047；k=250 时 +0.099）。

**② 偏差是"样本量"造成的 → 该做**尺度**校正，不是**水平**收缩。**
截断到 T 根时 `M_T` 系统性偏小、水位系统性偏高。校正量是一个标量：

    r(T)  = median( M_full / M_T )     在满窗板块（n >= 1460）上估计
    denom = M_T · r(T)

T → 1460 时 r → 1，退化回原口径，**没有 cliff**。
"""
import io
import json
import os
import statistics as st
import sys

sys.path.insert(0, r"D:\code\Moss-finagent-research")
os.chdir(r"D:\code\Moss-finagent-research")

from src.sector_crowding import db                       # noqa: E402
from src.sector_crowding.config import load_config       # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FULL = 1460
cfg = load_config()
conn = db.get_db_connection(cfg)
codes = [str(r["sector_code"]) for r in conn.execute(
    f"SELECT sector_code FROM {db.DAILY_TABLE} "
    f"GROUP BY sector_code HAVING COUNT(*) >= ?", (FULL,))]
marks = ",".join("?" * len(codes))
rows = conn.execute(
    f"SELECT sector_code, ma5_crowding FROM {db.DAILY_TABLE} "
    f"WHERE sector_code IN ({marks}) AND ma5_crowding IS NOT NULL "
    f"ORDER BY sector_code, trade_date", codes).fetchall()
conn.close()

series: dict[str, list[float]] = {}
for r in rows:
    series.setdefault(str(r["sector_code"]), []).append(float(r["ma5_crowding"]))

GRID = [60, 120, 250, 375, 500, 625, 750, 875, 1000, 1250, 1460]
L = [f"满窗板块（n >= {FULL}）: {len(series)} 个", "",
     f"{'T':>5}{'r(T)':>12}{'朴素偏差':>12}{'校正后偏差':>13}"
     f"{'朴素MAE':>11}{'校正后MAE':>12}{'MAE改善':>10}",
     "-" * 76]

table: dict[str, float] = {}
for T in GRID:
    ratios, naive_bias, corr_bias, naive_mae, corr_mae = [], [], [], [], []
    for vals in series.values():
        if len(vals) < FULL:
            continue
        v, m_full = vals[-1], max(vals)
        m_t = max(vals[-T:])
        if m_full <= 0 or m_t <= 0 or v <= 0:
            continue
        truth = v / m_full
        ratios.append(m_full / m_t)
        naive_bias.append(v / m_t - truth)
        naive_mae.append(abs(v / m_t - truth))
    if not ratios:
        continue
    r = st.median(ratios)
    table[str(T)] = round(r, 6)
    for vals in series.values():
        if len(vals) < FULL:
            continue
        v, m_full = vals[-1], max(vals)
        m_t = max(vals[-T:])
        if m_full <= 0 or m_t <= 0 or v <= 0:
            continue
        truth = v / m_full
        corr_bias.append(v / (m_t * r) - truth)
        corr_mae.append(abs(v / (m_t * r) - truth))
    gain = (st.mean(naive_mae) - st.mean(corr_mae)) / st.mean(naive_mae)
    L.append(f"{T:>5}{r:>12.4f}{st.mean(naive_bias):>+12.4f}"
             f"{st.mean(corr_bias):>+13.4f}{st.mean(naive_mae):>11.4f}"
             f"{st.mean(corr_mae):>12.4f}{gain:>10.1%}")

L += ["", "校正曲线（写进 configs）：", json.dumps(table, ensure_ascii=False)]

txt = "\n".join(L)
open(r"D:\code\Moss-finagent-research\docs\_water_bias_curve.txt", "w",
     encoding="utf-8").write(txt)
sys.stdout.buffer.write(txt.encode("utf-8", "replace"))
