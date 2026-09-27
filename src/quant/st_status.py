"""ST 状态（按**历史名称**判定）：某只票在某一天是不是风险警示股。

## 为什么需要这一层

A 股常规做法是把 ST 剔除出可交易池（涨跌停 5%、退市风险、流动性差），
而本项目的股票池口径里只有"是不是 A 股"这一条 —— 后果是：

1. **因子筛选**：ST 股被算进每日截面。它们的基本面极端（连年亏损）、
   价格行为也特殊（5% 涨跌停），会污染 IC/分层回测；
2. **单票回测**：会真的在 ST 期间买入 —— 而实盘里很多账户根本不允许。

## 数据来源与两个必须踩对的点

来源：Tushare `namechange`。每行是一段**名称生效区间**
`(ts_code, name, start_date, end_date)`，`end_date` 为空 = 仍在生效。
实测 600519 有三段：`20010827~20060524` / `20060525~20061008` /
`20061009~今`（最后一段 `end_date` 缺失）。

1. **不能只看当前名称**：`stock_basic` 是"今天"的名录，用它会把
   "当年 ST、后来摘帽"的票当成正常股，也会把"当年正常、现在 ST"的票
   一棍子打死 —— 回测要还原的是**当时**的状态，所以必须按区间查。
2. **公告日 ≠ 生效日**：ST 提前公告、当天生效。实测区间是**闭区间**，
   下一段的 `start_date` = 上一段的 `end_date` + 1 天。因此按
   `[start, end]` 落区间，**不要**用 `ann_date`。

顺带一个接口细节：`namechange` 的日期参数过滤的是 **ann_date**
（窗口内公告的变更，即使生效日在窗口之外 —— 实测窗口 `20140101~20140131`
返回了一条 `start_date=20140305` 的记录）。所以按公告日窗口分年拉取
就能拿全量，而且每只票至少有一行（初始名称）。
"""
from __future__ import annotations

import logging
import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: 名称前几个字符里出现 `ST` 就算风险警示。
#: 限定窗口是为了不误伤名称里本来就带拉丁字母的正常票（如 `TCL科技`）。
_ST_PREFIX_WINDOW = 4


def is_st_name(name: str | None) -> bool:
    """名称是否带风险警示标记（`ST` / `*ST` / `SST` / `S*ST` / `S ST`）。

    先删掉空格与星号，再看**前 4 个字符**里有没有 `ST`：
    `*ST海航` → `ST海航` ✓、`S*ST前锋` → `SST前锋` ✓、
    `ST舍得` ✓，而 `TCL科技`、`贵州茅台` ✗。

    ⚠️ 不含 `PT`（2001 年前的特别转让）：那是 2006 年以前的历史制度，
    本项目数据从 2006 年起，覆盖不到也不该覆盖。
    """
    text = re.sub(r"[\s*]", "", name or "").upper()
    return "ST" in text[:_ST_PREFIX_WINDOW]


def _norm_day(value: Any) -> str | None:
    """`2014-03-05` / `20140305` / NaN → `20140305`（拿不到就 None）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none"):
        return None
    text = text.replace("-", "").replace("/", "")[:8]
    return text if len(text) == 8 and text.isdigit() else None


def _norm_code(value: Any) -> str | None:
    """`600519.SH` / `600519` → `600519`（拿不到就 None）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    digits = re.sub(r"\D", "", text.split(".")[0])
    return digits.zfill(6) if digits else None


@dataclass(frozen=True)
class NameSpan:
    """一段名称生效区间（闭区间；`end is None` = 仍在生效）。"""

    code: str
    name: str
    start: str
    end: str | None

    @property
    def st(self) -> bool:
        return is_st_name(self.name)

    def covers(self, day: str) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


class StStatus:
    """按历史名称判定的 ST 状态表（可离线构造，不碰网络）。"""

    def __init__(self, spans: dict[str, list[NameSpan]] | None = None) -> None:
        self._spans: dict[str, list[NameSpan]] = {
            code: sorted(items, key=lambda item: item.start)
            for code, items in (spans or {}).items() if items
        }

    # ---------- 构造 ----------

    @classmethod
    def from_frame(cls, frame: pd.DataFrame | None) -> StStatus:
        """从 `namechange` 长表构造（列名兼容 `code` / `ts_code`）。"""
        if frame is None or len(frame) == 0:
            return cls({})
        spans: dict[str, list[NameSpan]] = {}
        skipped = 0
        for record in frame.to_dict("records"):
            code = _norm_code(record.get("code") or record.get("ts_code"))
            start = _norm_day(record.get("start_date"))
            if not code or not start:
                skipped += 1
                continue
            spans.setdefault(code, []).append(NameSpan(
                code=code, name=str(record.get("name") or ""),
                start=start, end=_norm_day(record.get("end_date"))))
        if skipped:
            # 宁可记账也不静默：缺 code/start_date 的行等于状态未知
            logger.warning("namechange 有 %d 行缺少 code/start_date，已跳过", skipped)
        return cls(spans)

    @classmethod
    def load(cls, root: str | Path | None = None,
             universe: str = "a_share") -> StStatus:
        """读本地 `namechange` static 分区；没有就返回空表（不报错）。

        返回空表而不是抛异常是刻意的：没有这份数据时，"剔除 ST"应当
        **降级为不剔除并如实标注**，而不是让整个筛选/回测失败。
        调用方用 `available` 判断到底有没有生效。
        """
        from src.quant.dataset_store import DEFAULT_ROOT, DatasetStore

        store = DatasetStore("namechange", root=root or DEFAULT_ROOT,
                             universe=universe)
        if not store.has("static"):
            logger.info("namechange 未下载 → ST 剔除不可用；"
                        "补数据：python scripts/quant_sync.py download "
                        "--namechange --end <今天>")
            return cls({})
        try:
            return cls.from_frame(store.read("static"))
        except Exception as exc:  # noqa: BLE001 读失败不该让回测挂掉
            logger.warning("读 namechange 失败：%s", exc)
            return cls({})

    # ---------- 基本属性 ----------

    @property
    def available(self) -> bool:
        return bool(self._spans)

    @property
    def code_count(self) -> int:
        return len(self._spans)

    @property
    def span_count(self) -> int:
        return sum(len(items) for items in self._spans.values())

    @property
    def st_codes(self) -> set[str]:
        """历史上曾被 ST/*ST 的票（用于体检"数据看起来对不对"）。"""
        return {code for code, items in self._spans.items()
                if any(item.st for item in items)}

    # ---------- 查询 ----------

    def is_st(self, code: str | None, day: str | None) -> bool:
        """某只票在某一天是否 ST。查不到 → False（未知不等于 ST）。

        区间**倒序**扫（start_date 最大的一段优先）：正常数据里各段首尾相接、
        不会重叠，但 Tushare 偶尔会有 `end_date` 缺失的旧行，那时必须以
        "离今天最近的那次改名"为准 —— 否则同一天会出现"逐格判定不算 ST、
        批量掩码算 ST"这种自相矛盾（实测被单测抓到过）。
        """
        norm_code, norm_day = _norm_code(code), _norm_day(day)
        if not norm_code or not norm_day:
            return False
        for span in reversed(self._spans.get(norm_code, ())):
            if span.covers(norm_day):
                return span.st
        return False

    def mask(self, dates: Sequence[str], codes: Sequence[str]) -> pd.DataFrame:
        """`(日期 × 代码)` 布尔表，True = 该日该票是 ST。

        两个实现要点：

        1. **不要逐格调 `is_st`**（5000 天 × 5500 只需要 2750 万次解释器
           调用）。这里把每段名称区间换算成日期轴上的下标区间，对 numpy
           切片一次性赋值 —— 复杂度只与"名称区间的数量"有关（全历史数千段）。
        2. 用**赋值**而不是 `|=`：按 start_date 升序处理，后一段覆盖前一段，
           结果就等于"start_date 最大且覆盖该日的那一段" —— 与 `is_st`
           的倒序扫描语义严格一致（用 `|=` 时两者会在重叠数据上分叉）。
        """
        day_list = [str(day) for day in dates]
        code_list = [str(code) for code in codes]
        grid = np.zeros((len(day_list), len(code_list)), dtype=bool)
        if not day_list or not code_list:
            return pd.DataFrame(grid, index=day_list, columns=code_list)
        column_of = {code: index for index, code in enumerate(code_list)}
        for code, spans in self._spans.items():
            column = column_of.get(_norm_code(code) or code)
            if column is None:
                continue
            for span in spans:                       # 已按 start 升序
                low = bisect_left(day_list, span.start)
                high = (bisect_right(day_list, span.end) if span.end
                        else len(day_list))
                if high > low:
                    grid[low:high, column] = span.st
        return pd.DataFrame(grid, index=day_list, columns=code_list)

    def st_stock_days(self, dates: Sequence[str],
                      codes: Sequence[str]) -> int:
        """窗口内"ST 股票日"的个数（体检与说明文案用）。"""
        grid = self.mask(dates, codes)
        return int(grid.to_numpy().sum())

    def describe(self) -> str:
        """一句话体检：覆盖了多少票、多少段、多少只曾被 ST。"""
        if not self.available:
            return "namechange 数据未下载，ST 剔除未生效"
        return (f"namechange 覆盖 {self.code_count} 只票 / {self.span_count} 段名称，"
                f"其中历史上曾被 ST 的有 {len(self.st_codes)} 只")


def load_st_status(root: str | Path | None = None,
                   universe: str = "a_share") -> StStatus:
    """便捷入口（`StStatus.load` 的函数式写法）。"""
    return StStatus.load(root=root, universe=universe)
