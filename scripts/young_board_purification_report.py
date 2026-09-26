"""年轻板块（行情 <60 日）的提纯：**现在等于没过滤，放低样本门槛会怎样？**

## 背景

提纯的相关性要求「股票与板块至少 60 个共同交易日」（`MIN_CORR_SAMPLES`）。
板块自己行情不足 60 日时，该概念的**每一只**成分股都算不出相关性，
`corr=None` → 按"新股"口径**全量放行** —— 整块概念一次过滤都没做。

实测只有两个这样的板块：

    886111.TI 玻璃基板   板块行情 58 日   56/56 只 corr=NULL
    886112.TI MLCC概念   板块行情 36 日   33/33 只 corr=NULL

（其余 `corr=NULL` 的行是真·个股新股，全表只占 0.4%。）

本轮已经把这三种情况在表里**分开标注**（`decision="board_young"`），
但**没有**改过滤口径。本脚本回答"改了会怎样"，供决策：

- 用板块现有的全部重叠交易日算相关性（下限 `--min-samples`，默认 20）；
- 按现行规则推演：全部成员按 corr 排序 → 只留前 `keep_ratio`(0.7)
  → `corr ≥ 0.55` 直接归属 → 其余要靠主营分（这两个板块**没有**主营分，
  所以推演结果就是 `corr ≥ 0.55` 的那批）；
- 给出保留只数、corr 分布、以及被放行/被剔除的名单。

⚠️ 这是**推演**，不写库。要不要真的放宽门槛是产品决策：
58 天的相关性标准误大约 ±0.13，用 0.55 卡它是一个**很强**的条件
（p 值约 1e-5）；但同一批股票里没有第二个独立证据（无主营分），
所以"严格按 corr 砍"等于让一次短窗口测量单独决定归属。

只读、不写库。

用法：
    .venv\\Scripts\\python.exe scripts/young_board_purification_report.py \\
        --out docs/MAINLINE_YOUNG_BOARD_PURE.md
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.member_pure import (  # noqa: E402
    CORR_DIRECT,
    KEEP_RATIO,
    keep_bounds,
)
from src.mainline.warehouse import open_warehouse  # noqa: E402

CACHE_DB = ROOT / "data" / "mainline_cache.db"


def log_returns(series: list[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for index in range(1, len(series)):
        day, close = series[index]
        _prev, prev = series[index - 1]
        if close and prev and prev > 0 and close > 0:
            out[day] = math.log(close / prev)
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy)


def main() -> int:
    parser = argparse.ArgumentParser(description="年轻板块提纯推演")
    parser.add_argument("--min-samples", type=int, default=20,
                        help="放宽后的最少共同交易日")
    parser.add_argument("--corr-direct", type=float, default=CORR_DIRECT)
    parser.add_argument("--keep-ratio", type=float, default=KEEP_RATIO)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    young = [(str(r["code"]), str(r["name"]), int(r["bars"])) for r in cache.execute(
        "SELECT b.code, b.name, (SELECT COUNT(*) FROM ml_board_bar x"
        " WHERE x.board_code = b.code) bars FROM ml_board b ORDER BY bars")
        if int(r["bars"]) < 60]
    houses: dict[str, list[tuple[str, float]]] = {}
    for code, _name, _bars in young:
        rows = cache.execute(
            "SELECT trade_date, close FROM ml_board_bar WHERE board_code = ?"
            " ORDER BY trade_date", (code,)).fetchall()
        houses[code] = [(str(r["trade_date"]), float(r["close"] or 0.0))
                        for r in rows]

    emit("# 年轻板块的提纯：现状与放低样本门槛的推演")
    emit()
    emit(f"> 由 `scripts/young_board_purification_report.py` 生成（只读推演）。")
    emit(f"> 现行门槛 `MIN_CORR_SAMPLES=60`；推演门槛 `--min-samples "
         f"{args.min_samples}`，`corr ≥ {args.corr_direct:g}` 直接归属，"
         f"保留上限 {args.keep_ratio:.0%}。")
    emit()
    emit(f"板块池里行情不足 60 个交易日的概念共 **{len(young)}** 个："
         + "、".join(f"{name}（{code}，{bars} 日）"
                     for code, name, bars in young))
    emit()

    wh = open_warehouse(str(ROOT / "data" / "quant" / "warehouse.db"))
    for code, name, bars in young:
        members = [str(r["code"]) for r in cache.execute(
            "SELECT code FROM ml_member WHERE board_code = ? ORDER BY code",
            (code,))]
        bret = log_returns(houses[code])
        scored: list[tuple[str, float, int]] = []
        short: list[str] = []
        for member in members:
            rows = wh.execute(
                "SELECT trade_date, close FROM quant_daily WHERE code = ?"
                " ORDER BY trade_date", (member,)).fetchall()
            sret = log_returns([(str(r["trade_date"]), float(r["close"] or 0.0))
                                for r in rows])
            common = sorted(set(sret) & set(bret))
            if len(common) < args.min_samples:
                short.append(member)
                continue
            value = pearson([sret[d] for d in common], [bret[d] for d in common])
            if value is None:
                short.append(member)
                continue
            scored.append((member, value, len(common)))
        scored.sort(key=lambda item: -item[1])
        low, high = keep_bounds(len(members), ratio=args.keep_ratio)
        head = {member for member, _v, _n in scored[:max(high, 0)]}
        direct = [item for item in scored
                  if item[0] in head and item[1] >= args.corr_direct]
        emit(f"## {name}（{code}）")
        emit()
        emit(f"- 板块行情 {bars} 个交易日 / 成分股 {len(members)} 只")
        emit(f"- 能算出相关性（≥{args.min_samples} 个共同交易日）："
             f"**{len(scored)}** 只；不足 {args.min_samples} 日的 {len(short)} 只")
        if scored:
            values = [item[1] for item in scored]
            emit(f"- corr 分布：最大 {max(values):.3f} / 中位 "
                 f"{values[len(values) // 2]:.3f} / 最小 {min(values):.3f}")
            emit(f"- 保留上限 = floor({len(members)}×{args.keep_ratio:.0%})"
                 f" = {high} 只（下限 {low} 只）")
            emit(f"- 走「corr ≥ {args.corr_direct:g} 直接归属」能留下的："
                 f"**{len(direct)}** 只"
                 f"（占成分股 {len(direct) / max(len(members), 1):.0%}）")
            emit()
            emit("| 股票 | corr | 共同交易日 | 是否 ≥ 门槛 |")
            emit("|---|---:|---:|---|")
            for member, value, count in scored[:30]:
                mark = "✅" if value >= args.corr_direct else ""
                emit(f"| {member} | {value:+.3f} | {count} | {mark} |")
            if len(scored) > 30:
                emit(f"| …共 {len(scored)} 只 | | | |")
        else:
            emit("- ⚠️ 放宽到 20 日仍然一只都算不出 —— 板块与成分股没有重叠交易日")
        emit()
        emit("⚠️ 这两个概念的成分股**都没有主营分**（LLM 未判定），所以落实"
             "「corr 不过门槛就剔除」等于让一次短窗口测量单独决定归属。"
             "上面的保留只数就是那个决策的全部依据。")
        emit()
    wh.close()
    cache.close()

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"记录 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
