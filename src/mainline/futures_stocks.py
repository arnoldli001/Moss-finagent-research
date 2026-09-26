"""主线挖掘：期货品种 ↔ A股个股 的**价格联动**（相关系数前 N）。

## 为什么不用 LLM 去"查"相关性

需求原话是"通过 deepseek 去查询各个期货与之价格联动最相关的股票"。
**这一步不能用 LLM 做**：语言模型没有价格相关系数的记忆，它只会给出
"听起来合理"的名字（螺纹钢 → 宝钢、鞍钢…），而这些名字：

- 无法复现（同一个 prompt 两次结果可能不同）；
- 无法验证（凭什么是这三只而不是另外三只）；
- 会随模型版本漂移，而 `mappings` 是写进仓库的配置。

而算这个相关系数所需的**全部数据已经在本地**：`ml_future` 有期货主力连续
日线，`data/quant/warehouse.db` 有 15.4M 行个股日线。因此这里**算**而不是"查"，
并把方法、窗口、样本数一并写进输出文件 —— 换个人、换台机器跑出来的结果一致。

LLM 可以做的是**事后复核**（"这三只票和螺纹钢的传导逻辑成立吗"），
那是锦上添花，不该参与生成。

## 三个口径细节（都会让结果静默失真）

**1. 用收益率，不用价格。** 价格序列几乎都是同阶趋势，两只不相关的股票
也能算出 0.9 的"相关"。收益率才是联动。

**2. 期货用 `pre_close` 算日收益，不用 `close[i]/close[i-1]`。**
主力连续在换月那天会把新旧合约拼在一起，产生一个**不存在的跳空**；
用它算收益会在每年 12 次换月日各注入一个 3%~8% 的假收益，
足以把相关性结构整个改掉。`fut_daily` 给了 `pre_close`，必须用。

**3. 样本不足宁可不给。** 少于 `min_samples` 个重叠交易日直接跳过。
120 天里只有 30 天重叠（停牌、次新股）算出来的相关系数标准误约 ±0.18，
排出来的"第一名"与"第十名"没有区别。

## 相关性 ≠ 因果，也不等于"该期货决定这只股票"

高相关可能只是因为两者同受一个宏观因子驱动（铜价与铜股都受美元与实际利率
影响）。因此输出里同时记录 `in_board`（该股是否落在映射板块内）——
一只**不在映射板块里**的高相关股，恰恰是最值得人工看一眼的（可能是
映射表漏了它，也可能是伪相关）。这个字段是给人判断用的，不参与筛选。

## 输出到独立文件，不覆盖手工映射表

写 `configs/mainline_futures_stocks.yaml`（自动生成，带 `generated_at`），
由 `futures.py` 在加载时合并进 `FutureMapping.stocks`。
**不直接改 `mainline_futures_mapping.yaml`**：那份文件的逐条 `logic`
是人工写的研究判断（"螺纹钢价格决定钢厂吨钢利润"），
让脚本重写它等于把人工成果覆盖掉，而且下次生成又会覆盖回来。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.config import PROJECT_ROOT, load_config

logger = logging.getLogger(__name__)

#: 输出文件（自动生成；与手工维护的 `mainline_futures_mapping.yaml` 分开）
LINKS_FILE = "mainline_futures_stocks.yaml"

#: 默认回看窗口（交易日）。250 ≈ 一年，够覆盖一轮商品周期的小波段。
DEFAULT_WINDOW = 250
#: 最少重叠交易日（不足则跳过该品种）
DEFAULT_MIN_SAMPLES = 120
#: 个股流动性下限：窗口内日均成交额（元）。太小的票相关性全是噪声。
DEFAULT_MIN_AMOUNT = 5.0e7
#: 每个品种保留几只
DEFAULT_TOP = 3


@dataclass
class StockLink:
    """一只个股与某期货品种的联动记录。"""

    code: str
    name: str = ""
    correlation: float = 0.0
    samples: int = 0
    #: 该股是否落在映射表给出的板块内（辅助人工判断，不参与筛选）
    in_board: bool = False
    #: 窗口内日均成交额（元）
    avg_amount: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name,
                "correlation": round(self.correlation, 4),
                "samples": self.samples, "in_board": self.in_board,
                "avg_amount": round(self.avg_amount, 2)}


@dataclass
class LinkTable:
    """一次联动计算的结果。"""

    links: dict[str, list[StockLink]] = field(default_factory=dict)
    window_days: int = DEFAULT_WINDOW
    min_samples: int = DEFAULT_MIN_SAMPLES
    top: int = DEFAULT_TOP
    start: str = ""
    end: str = ""
    generated_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "generated_at": self.generated_at,
                "window_days": self.window_days,
                "min_samples": self.min_samples, "top": self.top,
                "range": [self.start, self.end],
                "method": ("日收益率（期货用 pre_close 规避主力连续换月跳空）"
                           "的皮尔逊相关系数"),
                "note": self.note,
                "links": {code: [item.to_dict() for item in items]
                          for code, items in self.links.items()}}


# ==================================================================
# 计算
# ==================================================================


def _returns(closes: Sequence[float], pres: Sequence[float | None]
             ) -> list[float | None]:
    """由收盘价 + 前收盘价算日收益率（`pre_close` 缺失时回落上一日收盘）。"""
    out: list[float | None] = [None]
    for index in range(1, len(closes)):
        base = pres[index] if pres[index] else closes[index - 1]
        now = closes[index]
        if base and now and base > 0:
            value = now / base - 1.0
            out.append(value if math.isfinite(value) else None)
        else:
            out.append(None)
    return out


def _load_futures(store: Any, start: str, end: str
                  ) -> tuple[list[str], list[str], dict[str, list[float | None]]]:
    """读期货主力连续日线 → `(品种代码, 交易日, {code: 收益率序列})`。

    交易日以**所有品种日期的并集**升序为准；每个品种按该日历对齐
    （缺失处为 None）。不做对齐会让不同品种的收益率错位一格，
    相关系数整体偏移而看不出异常。
    """
    rows = store._read(  # noqa: SLF001 批量只读
        "SELECT code, trade_date, close, pre_close FROM ml_future"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
        (start, end))
    per_code: dict[str, list[tuple[str, float, float | None]]] = {}
    days: set[str] = set()
    for row in rows:
        code = str(row["code"])
        day = str(row["trade_date"])
        days.add(day)
        # ⚠️ 必须 setdefault：直接 `per_code[code].append` 会在第一行就
        # KeyError（实测踩到）。这类错误在"表里只有一个品种"时反而不触发，
        # 所以不能靠小样本试跑发现。
        per_code.setdefault(code, []).append(
            (day, float(row["close"] or 0), row["pre_close"]))
    calendar = sorted(days)
    index_of = {day: index for index, day in enumerate(calendar)}
    series: dict[str, list[float | None]] = {}
    for code, items in per_code.items():
        if len(items) < 10:
            continue
        closes = [item[1] for item in items]
        pres = [item[2] for item in items]
        rets = _returns(closes, pres)
        aligned: list[float | None] = [None] * len(calendar)
        for (day, _close, _pre), value in zip(items, rets, strict=True):
            aligned[index_of[day]] = value
        series[code] = aligned
    return sorted(series), calendar, series


def _load_stocks(warehouse: Any, calendar: Sequence[str], *,
                 min_amount: float) -> tuple[list[str], dict[str, list[float | None]]]:
    """读全市场个股日线 → `{code: 对齐到 calendar 的收益率序列}`。

    只保留窗口内**日均成交额达标**的股票：几百万成交额的票，
    其"价格联动"基本是噪声，但它们数量多，总能在某个品种上凑出高相关。
    """
    if not calendar:
        return [], {}
    start, end = calendar[0], calendar[-1]
    rows = warehouse.query(
        "SELECT code, trade_date, close, pre_close, amount FROM quant_daily"
        " WHERE trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
        (start, end))
    index_of = {day: index for index, day in enumerate(calendar)}
    buckets: dict[str, list[tuple[str, float, float | None, float]]] = {}
    for row in rows:
        code = str(row["code"]).zfill(6)
        day = str(row["trade_date"])
        if day not in index_of:
            continue
        buckets.setdefault(code, []).append(
            (day, float(row["close"] or 0), row["pre_close"],
             float(row["amount"] or 0)))
    out: dict[str, list[float | None]] = {}
    for code, items in buckets.items():
        if len(items) < 60:
            continue
        amounts = [item[3] for item in items]
        avg_amount = sum(amounts) / len(amounts)
        if avg_amount < min_amount:
            continue
        closes = [item[1] for item in items]
        pres = [item[2] for item in items]
        rets = _returns(closes, pres)
        aligned: list[float | None] = [None] * len(calendar)
        for (day, _c, _p, _a), value in zip(items, rets, strict=True):
            aligned[index_of[day]] = value
        out[code] = aligned
    return sorted(out), out


def _correlate(left: Sequence[float | None], right: Sequence[float | None],
               *, min_samples: int) -> tuple[float | None, int]:
    """两个收益率序列的皮尔逊相关（按位置配对，任一侧缺失则丢弃该对）。"""
    import numpy as np

    a = np.array([np.nan if v is None else v for v in left], dtype=float)
    b = np.array([np.nan if v is None else v for v in right], dtype=float)
    mask = ~(np.isnan(a) | np.isnan(b))
    count = int(mask.sum())
    if count < min_samples:
        return None, count
    x, y = a[mask], b[mask]
    sx, sy = x.std(), y.std()
    if sx <= 1e-12 or sy <= 1e-12:
        return None, count
    value = float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy))
    return (value if math.isfinite(value) else None), count


def build_links(*, window: int = DEFAULT_WINDOW, top: int = DEFAULT_TOP,
                min_samples: int = DEFAULT_MIN_SAMPLES,
                min_amount: float = DEFAULT_MIN_AMOUNT,
                codes: Sequence[str] | None = None,
                store: Any = None, config: Any = None,
                progress: Any = None) -> LinkTable:
    """算每个期货品种与之价格联动最强的 `top` 只个股。

    `codes` 为空时算 `ml_future` 里的全部品种。
    """
    import numpy as np

    config = config or load_config()
    if store is None:
        from src.mainline.datastore import MainlineDataStore

        store = MainlineDataStore(config=config)
    warehouse = getattr(store, "warehouse", None)
    if warehouse is None or not warehouse.available():
        return LinkTable(note="本地行情仓不可用，无法计算个股联动")

    end = store.calendar("", "")[-1] if store.calendar("", "") else ""
    if not end:
        return LinkTable(note="本地无交易日历")
    calendar_all = store.calendar("", end)
    if len(calendar_all) < min_samples:
        return LinkTable(note=f"本地交易日不足 {min_samples} 天")
    start = calendar_all[-min(len(calendar_all), max(int(window), min_samples))]
    table = LinkTable(window_days=window, min_samples=min_samples, top=top,
                      start=start, end=end,
                      generated_at=datetime.now().astimezone().isoformat(
                          timespec="seconds"))

    future_codes, calendar, futures = _load_futures(store, start, end)
    if not codes:
        # 默认只算**映射表里的品种**（89 个主力连续代码，如 `RB.SHF`）。
        # `ml_future` 里还有上千个具体月份合约（`RB2601.SHF` 这类），
        # 它们生命周期短、样本少，而且业务上没有任何理由给"螺纹钢 2601 合约"
        # 配三只股票 —— 不限制的话输出会从 89 条膨胀到 980 条，
        # 真正要看的品种被淹没。
        try:
            from src.mainline.futures import FuturesService

            codes = [item.future_code
                     for item in FuturesService(config=config, store=store).mappings]
        except Exception as exc:  # noqa: BLE001 读不到映射表就退回全部
            logger.info("映射表读取失败（改为计算全部品种）：%s",
                        brief(exc, BRIEF_TIGHT))
    if codes:
        wanted = set(codes)
        future_codes = [code for code in future_codes if code in wanted]
    if not future_codes:
        table.note = "本地没有期货行情（先同步 future 数据集）"
        return table
    stock_codes, stocks = _load_stocks(warehouse, calendar,
                                       min_amount=min_amount)
    if not stock_codes:
        table.note = "窗口内没有满足流动性门槛的个股"
        return table

    names = warehouse.stock_names(stock_codes)
    # 板块归属（辅助字段：不在映射板块内的高相关股最值得人工看一眼）
    board_names: dict[str, set[str]] = {}
    try:
        from src.mainline.futures import FuturesService

        service = FuturesService(config=config, store=store)
        members = store.member_map()
        for mapping in service.mappings:
            codes_in_board = set(members.get(mapping.board_code or "", []))
            if not codes_in_board:
                for board_code, items in members.items():
                    if board_code == mapping.board_code:
                        codes_in_board = set(items)
                        break
            board_names.setdefault(mapping.future_code, set()).update(
                codes_in_board)
    except Exception as exc:  # noqa: BLE001 板块归属只是辅助字段
        logger.info("板块归属读取失败（in_board 全部记 False）：%s",
                    brief(exc, BRIEF_TIGHT))

    matrix = np.array(
        [[np.nan if v is None else v for v in stocks[code]]
         for code in stock_codes], dtype=float)
    sample_counts: dict[str, int] = {}
    for index, code in enumerate(future_codes):
        if progress is not None:
            progress(f"{index + 1}/{len(future_codes)} {code}")
        correlations, counts = _correlate_row(futures[code], matrix,
                                              min_samples=min_samples)
        out: list[StockLink] = []
        for row_index, stock_code in enumerate(stock_codes):
            value = correlations[row_index]
            if value is None:
                continue
            out.append(StockLink(
                code=stock_code, name=names.get(stock_code, ""),
                correlation=value, samples=counts[row_index],
                in_board=stock_code in board_names.get(code, set())))
        if not out:
            continue
        # 按 |r| 排序：反向联动（期货涨、股票跌，如原油对航空）同样是强联动，
        # 只保留正相关会把这类映射整条丢掉。符号留在 correlation 里给人看。
        out.sort(key=lambda item: -abs(item.correlation))
        table.links[code] = out[:max(int(top), 1)]
        sample_counts[code] = len(out)
    table.note = "" if table.links else "没有任何品种算出了足够的重叠样本"
    return table


def _correlate_row(series: Sequence[float | None], matrix: Any, *,
                   min_samples: int) -> tuple[list[float | None], list[int]]:
    """一次算出**一个品种**与矩阵里**全部个股**的相关系数。

    逐股建 numpy 数组是 40 万次分配（141 品种 × ~3000 只票），实测慢到不可用；
    这里把"按掩码中心化再求相关"整体矩阵化 —— 逐日配对、任一侧缺失即丢弃，
    与 `_correlate` 的口径完全一致，只是不再一只一只算。
    """
    import numpy as np

    a = np.array([np.nan if v is None else v for v in series], dtype=float)
    mask = ~np.isnan(matrix) & ~np.isnan(a)[None, :]
    counts = mask.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        n = counts.astype(float)
        # 掩码内求和（掩码外先置 0，所以不会被计入）
        sum_x = np.where(mask, matrix, 0.0).sum(axis=1)
        sum_y = np.where(mask, a[None, :], 0.0).sum(axis=1)
        mean_x = sum_x / np.maximum(n, 1.0)
        mean_y = sum_y / np.maximum(n, 1.0)
        dx = np.where(mask, matrix - mean_x[:, None], 0.0)
        dy = np.where(mask, a[None, :] - mean_y[:, None], 0.0)
        cov = (dx * dy).sum(axis=1) / np.maximum(n, 1.0)
        sx = np.sqrt((dx * dx).sum(axis=1) / np.maximum(n, 1.0))
        sy = np.sqrt((dy * dy).sum(axis=1) / np.maximum(n, 1.0))
        corr = cov / (sx * sy)
    bad = (counts < min_samples) | (sx <= 1e-12) | (sy <= 1e-12) | ~np.isfinite(corr)
    values: list[float | None] = [None if bool(flag) else float(value)
                                  for value, flag in zip(corr, bad, strict=True)]
    return values, [int(item) for item in counts]


# ==================================================================
# 读写
# ==================================================================


def links_path(config: Any = None) -> Path:
    config = config or load_config()
    return PROJECT_ROOT / "configs" / LINKS_FILE


def save_links(table: LinkTable, path: Path | None = None) -> Path:
    """把结果写成 YAML（自动生成文件，带生成时间与方法说明）。"""
    import yaml

    target = path or links_path()
    payload = table.to_dict()
    header = (
        "# 主线挖掘 · 期货品种 ↔ A股个股 价格联动（**自动生成，请勿手工编辑**）\n"
        "#\n"
        "# 由 `python -m src.mainline.futures_stocks --build` 生成。\n"
        "# 手工维护的品种↔板块映射与传导逻辑在 mainline_futures_mapping.yaml，\n"
        "# 本文件只补充「与每个品种价格联动最强的个股」，两者在加载时合并。\n"
        "#\n"
        "# 口径：日收益率的皮尔逊相关系数；期货用 pre_close 规避主力连续换月跳空；\n"
        "# 样本不足 min_samples 的品种/个股直接跳过（不给低置信度的联动）。\n"
        "# ⚠️ 相关性 ≠ 因果：高相关可能只是同受一个宏观因子驱动。\n"
        "#    `in_board=false` 的高相关股最值得人工看一眼（可能是映射漏了它，\n"
        "#    也可能是伪相关）。\n")
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=100)
    target.write_text(header + body, encoding="utf-8")
    return target


def load_links(path: Path | None = None) -> dict[str, list[StockLink]]:
    """读联动文件（缺失时返回空 dict —— 没算过是正常状态，不该报错）。"""
    import yaml

    target = path or links_path()
    if not target.exists():
        return {}
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("个股联动文件解析失败：%s", brief(exc, BRIEF_TIGHT))
        return {}
    out: dict[str, list[StockLink]] = {}
    for code, items in (raw.get("links") or {}).items():
        rows: list[StockLink] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            rows.append(StockLink(
                code=str(item.get("code") or "").zfill(6),
                name=str(item.get("name") or ""),
                correlation=float(item.get("correlation") or 0.0),
                samples=int(item.get("samples") or 0),
                in_board=bool(item.get("in_board", False)),
                avg_amount=float(item.get("avg_amount") or 0.0)))
        if rows:
            out[str(code)] = rows
    return out


def _main(argv: Sequence[str] | None = None) -> int:
    """CLI：`python -m src.mainline.futures_stocks --build [--window 250]`。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="计算期货品种 ↔ A股个股的价格联动（取前 N 只）")
    parser.add_argument("--build", action="store_true", help="执行计算并写文件")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--show", default="", help="只看某个品种（如 RB.SHF）")
    parser.add_argument("--sync-futures", action="store_true",
                        help="先补拉期货历史（默认不联网）")
    args = parser.parse_args(argv)

    config = load_config()
    from src.mainline.datastore import MainlineDataStore

    store = MainlineDataStore(config=config)
    if args.sync_futures:
        result = store.sync_futures(start="20220101", end="", codes=None)
        print("同步期货：", result.note)
    if not args.build:
        for code, items in load_links().items():
            if args.show and code != args.show:
                continue
            print(f"{code}: " + "、".join(
                f"{item.name}({item.code}) r={item.correlation:+.3f}"
                f"{'' if item.in_board else ' [板块外]'}" for item in items))
        return 0

    def progress(text: str) -> None:
        print("  ", text, flush=True)

    table = build_links(window=args.window, top=args.top,
                        min_samples=args.min_samples, store=store,
                        config=config, progress=progress)
    if table.note:
        print("未完成：", table.note)
        return 1
    path = save_links(table)
    print(f"已写入 {path}（{len(table.links)} 个品种，"
          f"窗口 {table.start}~{table.end}）")
    for code, items in list(table.links.items())[:8]:
        print(f"  {code}: " + "、".join(
            f"{item.name} r={item.correlation:+.3f}"
            f"{'' if item.in_board else '[板块外]'}" for item in items))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_MIN_AMOUNT",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_TOP",
    "DEFAULT_WINDOW",
    "LINKS_FILE",
    "LinkTable",
    "StockLink",
    "build_links",
    "links_path",
    "load_links",
    "save_links",
]
