"""成分股提纯的有效性验证：阈值敏感性 + 内部一致性 + 预测力对比。

## 这个脚本回答什么问题

提纯的产物是 `ml_member_pure`（逐 板块×股票 的 `corr` / `relevant`），
其中 `relevant` 是一个**并集规则**：

    relevant = (corr >= 阈值) OR (LLM 判定主营业务相关)

于是必然被追问两个问题，而这两个问题不能靠"看结果顺眼"回答：

    1. **阈值 0.55 是怎么定的？** —— 换一个阈值，结果会差多少？
    2. **提纯到底有没有用？** —— 剔掉成员之后，板块信号是变好了还是变差了？

本脚本用三个可复现的实验回答它们。

## 三个实验

### A. 阈值敏感性（`--stage threshold`）

把阈值从 0.40 扫到 0.75，每一档测量：

- **覆盖**：保留的对数、还有 ≥5 个成员的板块数、每板块成员数中位数
- **内部一致性**：板块内成员两两收益率相关系数的均值

预期是"一致性上升、覆盖下降"的权衡曲线。**阈值应落在曲线拐点（knee）附近，
而不是拍脑袋** —— 拐点之后一致性提升趋缓，但覆盖掉得很快。

### B. 原始 vs 提纯 的内部一致性（`--stage coherence`）

对同一批板块，比较"全部原始成员"与"提纯后成员"的内部一致性。

**这是提纯目标的直接检验**：提纯声称"去掉蹭概念的噪声股"，那么提纯后的成员
应当**更像在交易同一件事** —— 即两两相关性更高。若提纯后一致性没提升，
说明相关性判据没有起到提纯作用。

### C. 板块信号的预测力（`--stage ic`）

用"成员等权平均收益"作为板块信号，去预测**板块指数未来 10 日收益**，
分别用原始成员和提纯成员各算一遍：

    信号_t   = 成员过去 5 日等权收益的均值
    目标_t   = 板块指数 t+1 ~ t+10 的累计收益
    IC_t     = 截面上「信号」与「目标」的 Spearman 秩相关
    输出     = IC 均值 / IC 标准差 / ICIR = 均值÷标准差 / IC>0 占比

**⚠️ 必须声明的偏差**：`ml_member_pure` 是**当前成分股快照**，没有历史归属，
所以用今天的成员回溯过去的行情存在**幸存者偏差**，绝对 IC 会偏高。
但 raw 与 pure 用的是**同一份快照基线**，两者的**相对比较**仍然成立 ——
结论只能读作"提纯相对原始有没有改善"，不能读作"这套信号的绝对预测力"。

## 用法

    python scripts/mainline_purity_validate.py --stage threshold
    python scripts/mainline_purity_validate.py --stage coherence
    python scripts/mainline_purity_validate.py --stage ic
    python scripts/mainline_purity_validate.py --stage all
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.errors import BRIEF_DEFAULT, brief  # noqa: E402
from src.mainline.config import load_config  # noqa: E402

#: 阈值扫描档位（从"全保留"扫到 0.75，**必须含 0.55 以下**才能看出拐点）
THRESHOLDS = (0.0, 0.20, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65,
              0.70, 0.75)
#: 计算板块内部一致性时，成员数超过它就抽样（保证可复现：按代码排序取前 N）
COHERENCE_CAP = 20
#: 两两相关所需的最少共同观测
MIN_OBS = 60
#: 计算 IC 时使用的历史窗口（交易日）
LOOKBACK_DAYS = 421
#: 信号回看 / 目标前视（交易日）
SIGNAL_WINDOW = 5
FORWARD_WINDOW = 10


# ==================================================================
# 数据加载
# ==================================================================


def load_pure(ml_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{ml_path.as_posix()}?mode=ro", uri=True)
    try:
        return pd.read_sql(
            "SELECT board_code, code, corr, business_score, final_score,"
            " rank_in_board, relevant, source FROM ml_member_pure", conn)
    finally:
        conn.close()


def load_pairs(ml_path: Path, boards: set[str]) -> pd.DataFrame:
    """统一候选空间：**原始成员** × corr × LLM 判定。

    为什么不能直接用 `ml_member_pure` 做扫描：那张表**只存了已经过阈值筛选的
    corr 对**（`source='corr'` 的行 corr 必然 ≥ 0.55），所以把阈值调到 0.55 以下
    时**一行都不会增加** —— 扫描出来是一条水平线，结论完全错误（实测踩过）。

    完整的 corr 在 `ml_member_corr` 里（324 板块内 49,886 对，其中
    0.35~0.5 区间就有 13,633 对）。所以这里以 `ml_member` 的原始成员为基准，
    left join `ml_member_corr` 拿 corr、left join `ml_member_pure` 拿 LLM 判定，
    这样阈值**从上到下都能扫**。
    """
    conn = sqlite3.connect(f"file:{ml_path.as_posix()}?mode=ro", uri=True)
    try:
        raw = pd.read_sql("SELECT board_code, code FROM ml_member", conn)
        corr = pd.read_sql(
            "SELECT board_code, code, corr FROM ml_member_corr", conn)
        pure = pd.read_sql(
            "SELECT board_code, code, relevant, source FROM ml_member_pure",
            conn)
    finally:
        conn.close()
    for frame in (raw, corr, pure):
        frame["board_code"] = frame["board_code"].astype(str)
        frame["code"] = frame["code"].astype(str)

    base = raw[raw["board_code"].isin(boards)].drop_duplicates()
    base = base.merge(corr, on=["board_code", "code"], how="left")
    llm = pure[(pure["source"] == "llm") & (pure["relevant"] == 1)][
        ["board_code", "code"]].drop_duplicates()
    llm["llm_ok"] = True
    base = base.merge(llm, on=["board_code", "code"], how="left")
    base["llm_ok"] = base["llm_ok"].fillna(False).astype(bool)
    return base


def load_raw_members(ml_path: Path, boards: set[str]) -> dict[str, list[str]]:
    """原始成分股 `{board: [code]}`（只取要比较的板块）。"""
    conn = sqlite3.connect(f"file:{ml_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = pd.read_sql("SELECT board_code, code FROM ml_member", conn)
    finally:
        conn.close()
    rows = rows[rows["board_code"].isin(boards)]
    return {str(b): [str(x) for x in g["code"]]
            for b, g in rows.groupby("board_code")}


def load_returns(ml_path: Path, wh_path: Path, codes: set[str],
                 since: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 `(个股日对数收益率, 板块指数日对数收益率)`，都是 日期×代码 的宽表。"""
    wh = sqlite3.connect(f"file:{wh_path.as_posix()}?mode=ro", uri=True)
    try:
        px = pd.read_sql(
            "SELECT code, trade_date, close FROM quant_daily"
            " WHERE trade_date >= ? AND close > 0", wh, params=[since])
    finally:
        wh.close()
    px = px[px["code"].isin(codes)]
    wide = px.pivot(index="trade_date", columns="code", values="close").sort_index()
    stock = np.log(wide / wide.shift(1))

    ml = sqlite3.connect(f"file:{ml_path.as_posix()}?mode=ro", uri=True)
    try:
        bb = pd.read_sql(
            "SELECT board_code, trade_date, close FROM ml_board_bar"
            " WHERE trade_date >= ? AND close > 0", ml, params=[since])
    finally:
        ml.close()
    bwide = bb.pivot(index="trade_date", columns="board_code",
                     values="close").sort_index()
    board = np.log(bwide / bwide.shift(1))
    return stock, board


def load_flow_intensity(wh_path: Path, codes: set[str],
                        since: str) -> pd.DataFrame:
    """个股日**净流入强度** = 主力净流入 / 流通市值（日期×代码 宽表）。

    为什么要除以流通市值：同样 1 亿元净流入，对 30 亿市值的票和对 3000 亿市值的
    票含义完全不同。板块层面再对成员求和，得到"这个板块今天被净买入了多少
    个流通市值百分比" —— 这才是可跨板块比较的量纲。
    """
    wh = sqlite3.connect(f"file:{wh_path.as_posix()}?mode=ro", uri=True)
    try:
        flow = pd.read_sql(
            "SELECT code, trade_date, net_mf_amount FROM quant_moneyflow"
            " WHERE trade_date >= ?", wh, params=[since])
        cap = pd.read_sql(
            "SELECT code, trade_date, circ_mv FROM quant_daily_basic"
            " WHERE trade_date >= ? AND circ_mv > 0", wh, params=[since])
    finally:
        wh.close()
    flow = flow[flow["code"].isin(codes)]
    cap = cap[cap["code"].isin(codes)]
    fw = flow.pivot(index="trade_date", columns="code",
                    values="net_mf_amount").sort_index()
    cw = cap.pivot(index="trade_date", columns="code",
                   values="circ_mv").sort_index()
    common = fw.index.intersection(cw.index)
    return fw.loc[common] / cw.loc[common]


# ==================================================================
# 指标
# ==================================================================


def mean_pairwise_corr(rets: pd.DataFrame, codes: list[str],
                       cap: int = COHERENCE_CAP) -> tuple[float, int]:
    """成员两两收益率相关系数的均值（成员数超 `cap` 时抽样）。

    抽样按**代码排序取前 N** —— 刻意不用随机抽样，否则每次跑出来的数字都不同，
    无法复现也就无法被别人验证。
    """
    cols = sorted(c for c in codes if c in rets.columns)[:cap]
    if len(cols) < 2:
        return float("nan"), len(cols)
    sub = rets[cols]
    corr = sub.corr(min_periods=MIN_OBS)
    mask = np.triu(np.ones(corr.shape, dtype=bool), k=1)
    values = corr.where(mask).stack()
    if values.empty:
        return float("nan"), len(cols)
    return float(values.mean()), len(cols)


def spearman(a: pd.Series, b: pd.Series) -> float:
    """两个序列的 Spearman 秩相关（先对齐、去掉缺失）。"""
    joined = pd.concat([a, b], axis=1, keys=["x", "y"]).dropna()
    if len(joined) < 5:
        return float("nan")
    return float(joined["x"].corr(joined["y"], method="spearman"))


# ==================================================================
# 实验 A：阈值敏感性
# ==================================================================


def stage_threshold(pairs: pd.DataFrame, stock: pd.DataFrame) -> None:
    print("=" * 78)
    print("实验 A：阈值全区间敏感性（覆盖面 vs 板块内部一致性）")
    print("=" * 78)
    print("规则：relevant = (corr >= 阈值) OR (LLM 判定主营业务相关)")
    print(f"内部一致性：板块内成员两两日收益率相关均值（超 {COHERENCE_CAP} 个抽样）")
    print("候选空间：324 个板块的**原始成分股**，corr 取自 ml_member_corr\n")
    print(f"  {'阈值':>6} {'保留对':>8} {'板块数':>7} {'≥5成员':>7}"
          f" {'成员中位':>8} {'一致性':>8} {'Δ一致性':>8}")

    has_corr = pairs["corr"].notna()
    print(f"  （候选 {len(pairs):,} 对，其中有 corr 的 {has_corr.sum():,} 对；"
          f"corr 缺失的按\"保留\"处理）")

    rows: list[tuple] = []
    for th in THRESHOLDS:
        keep = (pairs["corr"] >= th) | pairs["corr"].isna() | pairs["llm_ok"]
        sub = pairs[keep]
        by_board = sub.groupby("board_code")["code"].apply(list)
        sizes = by_board.map(len)
        coh = []
        for members in by_board:
            value, _ = mean_pairwise_corr(stock, members)
            if value == value:
                coh.append(value)
        rows.append((th, len(sub), len(by_board), int((sizes >= 5).sum()),
                     float(sizes.median()), float(np.mean(coh)) if coh else np.nan))

    baseline = rows[0][5] if rows else np.nan
    for th, n, nb, ge5, med, coh in rows:
        label = "全保留" if th <= 0 else f"{th:.2f}"
        print(f"  {label:>6} {n:>8,} {nb:>7} {ge5:>7} {med:>8.0f}"
              f" {coh:>8.3f} {coh - baseline:>+8.3f}")

    print("\n  读法（这张表就是\"阈值凭什么\"的答案）：")
    print("   - 一致性随阈值上升而上升，但要看**边际**：从下表挑\"每丢掉 10% 覆盖")
    print("     换到多少一致性\"，拐点之后收益递减。")
    print("   - 若某个阈值之后一致性不再上升（或反而下降），那是**成员太少导致")
    print("     统计不稳**，不是信号变好。")
    ordered = [(t, n, c) for t, n, _nb, _g, _m, c in rows if t > 0]
    for i in range(1, len(ordered)):
        t0, n0, c0 = ordered[i - 1]
        t1, n1, c1 = ordered[i]
        if n0 and n1 < n0:
            print(f"   {t0:.2f}→{t1:.2f}: 覆盖 {n0:,}→{n1:,}"
                  f"（丢 {1 - n1 / n0:.0%}）  一致性 {c0:.3f}→{c1:.3f}"
                  f"（{c1 - c0:+.3f}）")


# ==================================================================
# 实验 B：原始 vs 提纯
# ==================================================================


def stage_coherence(pure: pd.DataFrame, stock: pd.DataFrame,
                    raw: dict[str, list[str]]) -> None:
    print("=" * 78)
    print("实验 B：原始 vs 提纯 的板块内部一致性（提纯目标的直接检验）")
    print("=" * 78)

    pure_ok = pure[pure["relevant"] == 1]
    pure_map = {str(b): [str(x) for x in g["code"]]
                for b, g in pure_ok.groupby("board_code")}
    rows = []
    for board, raw_members in raw.items():
        members = pure_map.get(board)
        if not members:
            continue
        c_raw, n_raw = mean_pairwise_corr(stock, raw_members)
        c_pure, n_pure = mean_pairwise_corr(stock, members)
        if c_raw != c_raw or c_pure != c_pure:
            continue
        rows.append((board, len(raw_members), n_raw, c_raw, n_pure, c_pure))
    df = pd.DataFrame(rows, columns=["board", "raw_n", "raw_used", "raw_coh",
                                     "pure_n", "pure_coh"])
    if df.empty:
        print("  没有可比较的板块")
        return
    df["delta"] = df["pure_coh"] - df["raw_coh"]
    better = int((df["delta"] > 0).sum())
    print(f"  可比板块 {len(df)} 个\n")
    print(f"  原始成员一致性均值   {df['raw_coh'].mean():.3f}"
          f"   （每板块成员数中位 {df['raw_n'].median():.0f}）")
    print(f"  提纯成员一致性均值   {df['pure_coh'].mean():.3f}"
          f"   （每板块成员数中位 {df['pure_n'].median():.0f}）")
    print(f"  平均提升             {df['delta'].mean():+.3f}")
    print(f"  一致性变好的板块     {better}/{len(df)} ({better / len(df):.0%})")
    print(f"\n  提升最大的 5 个板块：")
    for _, r in df.nlargest(5, "delta").iterrows():
        print(f"    {r['board']:12} {r['raw_n']:>4}→{r['pure_n']:<4}只"
              f"  一致性 {r['raw_coh']:.3f} → {r['pure_coh']:.3f}"
              f"  ({r['delta']:+.3f})")
    print(f"  变差的 5 个板块：")
    for _, r in df.nsmallest(5, "delta").iterrows():
        print(f"    {r['board']:12} {r['raw_n']:>4}→{r['pure_n']:<4}只"
              f"  一致性 {r['raw_coh']:.3f} → {r['pure_coh']:.3f}"
              f"  ({r['delta']:+.3f})")


# ==================================================================
# 实验 C：预测力（IC / ICIR）
# ==================================================================


def _board_signal(rets: pd.DataFrame, members: list[str]) -> pd.Series:
    """板块信号 A：成员日收益等权平均的 `SIGNAL_WINDOW` 日累计。"""
    cols = [c for c in members if c in rets.columns]
    if not cols:
        return pd.Series(dtype=float)
    return rets[cols].mean(axis=1).rolling(SIGNAL_WINDOW).sum()


def _board_flow_signal(flow: pd.DataFrame, members: list[str]) -> pd.Series:
    """板块信号 B：成员净流入强度的 `SIGNAL_WINDOW` 日累计。

    为什么值得单独验一遍：主线模块**第一层就有资金流维度**，板块资金流是
    "成分股聚合"得来的 —— 提纯若真有价值，最该体现在这个信号上。
    只用价格收益做验证，等于没测到模块实际依赖的那条链路。
    """
    cols = [c for c in members if c in flow.columns]
    if not cols:
        return pd.Series(dtype=float)
    return flow[cols].sum(axis=1, min_count=1).rolling(SIGNAL_WINDOW).sum()


def _ic_series(signals: dict[str, pd.Series], forward: pd.DataFrame,
               dates: list[str]) -> pd.Series:
    out: dict[str, float] = {}
    for date in dates:
        s = pd.Series({b: series.get(date, np.nan)
                       for b, series in signals.items()})
        if date not in forward.index:
            continue
        value = spearman(s, forward.loc[date])
        if value == value:
            out[date] = value
    return pd.Series(out)


def stage_ic(pure: pd.DataFrame, stock: pd.DataFrame, board_rets: pd.DataFrame,
             flow: pd.DataFrame | None, raw: dict[str, list[str]]) -> None:
    print("=" * 78)
    print("实验 C：板块信号的预测力（原始 vs 提纯）")
    print("=" * 78)
    print(f"  目标 = 板块指数未来 {FORWARD_WINDOW} 日收益；IC = 截面 Spearman")
    print(f"  信号A = 成员过去 {SIGNAL_WINDOW} 日等权收益")
    print(f"  信号B = 成员过去 {SIGNAL_WINDOW} 日净流入强度"
          f"（主力净流入÷流通市值）\n")

    pure_map = {str(b): [str(x) for x in g["code"]]
                for b, g in pure[pure["relevant"] == 1].groupby("board_code")}
    boards = sorted(set(raw) & set(pure_map))
    if not boards:
        print("  没有可比板块")
        return

    forward = board_rets.rolling(FORWARD_WINDOW).sum().shift(-FORWARD_WINDOW)
    dates = [str(d) for d in board_rets.index[::SIGNAL_WINDOW]
             if d in forward.index]
    print(f"  可比板块 {len(boards)} 个，评估时点 {len(dates)} 个")
    print("  计算中（每个板块要聚合成员）…")
    started = time.perf_counter()

    def run(label: str, matrix: pd.DataFrame, builder) -> str:
        """对同一组成员，用给定信号矩阵算 raw / pure 两组 IC 并对比。"""
        sig_raw = {b: builder(matrix, raw[b]) for b in boards}
        sig_pure = {b: builder(matrix, pure_map[b]) for b in boards}
        empty = sum(1 for b in boards if sig_pure[b].empty)
        ic_raw = _ic_series(sig_raw, forward, dates)
        ic_pure = _ic_series(sig_pure, forward, dates)
        if ic_raw.empty or ic_pure.empty:
            return f"  {label:22} 无有效 IC（信号为空 {empty} 个板块）"
        ir_raw = ic_raw.mean() / ic_raw.std() if ic_raw.std() else float("nan")
        ir_pure = (ic_pure.mean() / ic_pure.std()
                   if ic_pure.std() else float("nan"))
        verdict = ("提纯更好" if ir_pure > ir_raw
                   else "原始更好" if ir_pure < ir_raw else "持平")
        return (f"  {label:22} IC {ic_raw.mean():+.4f} → {ic_pure.mean():+.4f}"
                f"   ICIR {ir_raw:+.3f} → {ir_pure:+.3f}"
                f"   ΔICIR {ir_pure - ir_raw:+.3f}   [{verdict}]")

    lines = [run("信号A 价格动量", stock, _board_signal)]
    if flow is not None and not flow.empty:
        lines.append(run("信号B 资金流强度", flow, _board_flow_signal))
    print(f"  用时 {time.perf_counter() - started:.0f}s\n")
    for line in lines:
        print(line)

    print("\n  ⚠️ 幸存者偏差声明：成员是**当前快照**，用今天的成分股回溯过去行情，")
    print("     绝对 IC 会偏高。raw 与 pure 共用同一快照基线，所以**只读相对差异**。")
    print("  ⚠️ 信号本身很粗糙（单一 5 日窗口、等权、无行业/市值中性化），")
    print("     它的作用是**比较两种成员集**，不是证明这套信号能赚钱。")


# ==================================================================
# 主流程
# ==================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="成分股提纯有效性验证")
    parser.add_argument("--stage", default="all",
                        help="threshold / coherence / ic / all")
    parser.add_argument("--since", default="20250102",
                        help="行情回看起点（YYYYMMDD）")
    args = parser.parse_args()

    cfg = load_config()
    ml_path = Path(cfg.cache_file)
    wh_path = Path("data/quant/warehouse.db")
    for p in (ml_path, wh_path):
        if not p.exists():
            print(f"缺少数据文件：{p}")
            return 1

    print(f"主线仓：{ml_path}")
    print(f"行情仓：{wh_path}\n")

    pure = load_pure(ml_path)
    if pure.empty:
        print("ml_member_pure 为空 —— 先跑概念→股票映射")
        return 1
    pure["board_code"] = pure["board_code"].astype(str)
    pure["code"] = pure["code"].astype(str)
    boards = set(pure["board_code"])
    raw = load_raw_members(ml_path, boards)
    pairs = load_pairs(ml_path, boards)
    codes = set(pure["code"]) | {c for v in raw.values() for c in v}
    print(f"ml_member_pure {len(pure):,} 行 / {len(boards)} 个板块"
          f"（其中 relevant=1 有 {int((pure['relevant'] == 1).sum()):,} 行）")
    print(f"原始成分股覆盖 {len(raw)} 个可比板块 / 涉及 {len(codes):,} 只股票")
    print(f"阈值扫描候选空间 {len(pairs):,} 对"
          f"（有 corr 的 {int(pairs['corr'].notna().sum()):,} 对，"
          f"LLM 判定相关 {int(pairs['llm_ok'].sum()):,} 对）\n")

    print("加载行情（这一步最慢）…")
    started = time.perf_counter()
    stock, board_rets = load_returns(ml_path, wh_path, codes, args.since)
    print(f"  个股收益率矩阵 {stock.shape[0]} 日 × {stock.shape[1]} 只")
    print(f"  板块指数矩阵   {board_rets.shape[0]} 日 × {board_rets.shape[1]} 个")
    print(f"  用时 {time.perf_counter() - started:.0f}s\n")

    stages = (["threshold", "coherence", "ic"] if args.stage == "all"
              else [s.strip() for s in args.stage.split(",") if s.strip()])
    flow = None
    if "ic" in stages:
        print("加载资金流强度（主力净流入 ÷ 流通市值）…")
        started = time.perf_counter()
        flow = load_flow_intensity(wh_path, codes, args.since)
        print(f"  矩阵 {flow.shape[0]} 日 × {flow.shape[1]} 只"
              f"  用时 {time.perf_counter() - started:.0f}s\n")
    for stage in stages:
        try:
            if stage == "threshold":
                stage_threshold(pairs, stock)
            elif stage == "coherence":
                stage_coherence(pure, stock, raw)
            elif stage == "ic":
                stage_ic(pure, stock, board_rets, flow, raw)
            else:
                print(f"未知阶段：{stage}")
                return 2
        except Exception as exc:  # noqa: BLE001
            print(f"\n阶段 {stage} 失败：{brief(exc, BRIEF_DEFAULT)}")
            return 1
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
