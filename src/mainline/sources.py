"""主线挖掘：数据源层。

## 分层与职责

本文件只做**取数与规范化**，不含任何打分逻辑（打分在 `six_dim.py` /
`five_dim.py` / `leader.py`）。这样做的理由是：打分要能被离线回测反复重放，
而取数依赖网络与本地库 —— 两者混在一起会让"回测结果变了"永远分不清
是模型改了还是数据源改了。

    TushareSource    板块指数（sw_daily/ths_daily）、板块资金流（moneyflow_ind_dc）、
                     个股资金流（moneyflow）、两融、北向、股东户数、龙虎榜、期货
    WarehouseSource  本地 15 GiB 仓库的**横截面**读取（quant_daily/basic/moneyflow）
    BoardCatalog     可评分板块目录（申万一级 + 同花顺概念），带 24 小时缓存
    EastmoneySource  东财板块资金流免费接口（日线历史 + 分钟级），限速 ≥1 秒

## 三个重要的口径事实（都经实测确认，写在这里避免后人重复踩坑）

**1. `moneyflow_ind_dc`（东财板块资金流）本 token 可用。**
需求文档假设它需要 6000 积分并建议改用东财免费接口；实测本 token 直接可调，
因此**优先用它**（一次调用拿回全市场 1000+ 个板块的当日截面，比逐个板块
打东财免费接口快两个数量级）。东财免费接口保留为历史补齐与降级通道。

**2. `ths_member` 支持按个股反查概念。**
`ths_member(con_code="600519.SH")` 一次返回该股所属的全部概念（实测 70 条）。
这比"遍历 2500 个概念逐个查成分股"快得多，是成分股归属的主路径。

**3. 北向资金没有个股级日度数据。**
2024-08 起交易所停止披露北向日度个股净买入。`moneyflow_hsgt` 只有沪深股通
**汇总**。因此 `northbound_daily()` 返回的行 `stock_code` 为空，
个股级北向维度必须如实标注缺口 —— 编造一个假数字比留空更有害。

## 单位口径（不统一就会出 10000 倍级错误）

    资金流（moneyflow / moneyflow_ind_dc）  Tushare 原口径「万元」→ 统一乘 1e4 得**元**
    成交额（daily.amount）                  Tushare 原口径「千元」→ 乘 1e3 得**元**
    市值（daily_basic.circ_mv/total_mv）    Tushare 原口径「万元」→ 乘 1e4 得**元**
    两融（margin_detail.rzye 等）           已是**元**，不再换算
    龙虎榜（top_list/top_inst 各金额）       已是**元**，不再换算
    股东户数（stk_holdernumber.holder_num）  **户**，不换算
    期货（fut_daily.amount）                已是**元**
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.errors import BRIEF_DEFAULT, BRIEF_TIGHT, brief
from src.mainline.config import MainlineConfig, load_config
from src.mainline.models import (
    BoardBar,
    BoardFlow,
    BoardInfo,
    BoardKind,
    BoardSeries,
    FundPoint,
    FutureKind,
    HolderRow,
    MarginRow,
    NorthboundRow,
    SeatRow,
    StockFlow,
)
from src.mainline.warehouse import open_warehouse

logger = logging.getLogger(__name__)

_WAN = 1.0e4   # 万元 → 元
_QIAN = 1.0e3  # 千元 → 元

#: 申万一级行业的官方名单（31 个）。实测 `sw_daily` 单日返回 439 行，
#: 里面混着二级/三级行业与宽基综合指数，「取一级行业」必须显式判定。
#:
#: 判定规则来自实测：申万一级行业代码形如 `801X10`/`801X20`…**末位恒为 0**
#: （801010 农林牧渔、801030 基础化工、801780 银行…），二级三级末位非 0。
#: 该规则会额外捞进 5 个「申万制造/消费/投资/服务/300指数」宽基综合指数，
#: 它们不是行业、不该进行业轮动榜单，因此在下面显式排除。
#: 规则已用 `index_member_all` 反查 8 只不同行业个股交叉验证：反查得到的
#: L1 代码全部落在本名单内，无遗漏（2026-09-20 实测）。
_SW_L1_CODES = frozenset({
    "801010", "801030", "801040", "801050", "801080", "801110", "801120",
    "801130", "801140", "801150", "801160", "801170", "801180", "801200",
    "801210", "801230", "801710", "801720", "801730", "801740", "801750",
    "801760", "801770", "801780", "801790", "801880", "801890", "801950",
    "801960", "801970", "801980",
})


# ==================================================================
# 小工具
# ==================================================================


def to_code(ts_code: str) -> str:
    """`600519.SH` → `600519`（与项目其它模块的 6 位代码口径统一）。"""
    return str(ts_code).split(".")[0].strip().zfill(6)


def to_ts_code(code: str) -> str:
    """6 位代码 → Tushare ts_code（按首位判断交易所）。"""
    digits = re.sub(r"\D", "", str(code))
    if len(digits) != 6:
        return str(code)
    if digits[0] in "56":
        return f"{digits}.SH"
    if digits[0] in "0123":
        return f"{digits}.SZ"
    if digits[0] in "489":
        return f"{digits}.BJ"
    return f"{digits}.SH"


def _f(value: Any, default: float = 0.0) -> float:
    """安全转 float（None/NaN/非数字一律回默认值）。"""
    try:
        if value is None:
            return default
        result = float(value)
        return default if result != result else result  # NaN != NaN
    except (TypeError, ValueError):
        return default


def _opt(value: Any) -> float | None:
    """安全转「可空」float（缺失返回 None，**不回 0** —— 0 与"不知道"不同）。"""
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return None if result != result else result
    except (TypeError, ValueError):
        return None


def _is_sw_l1(code: str) -> bool:
    """是否**申万一级行业**（不是二级/三级，也不是宽基综合指数）。

    名单见 `_SW_L1_CODES`（规则与验证方法写在那里）。
    """
    return str(code).split(".")[0] in _SW_L1_CODES


def _tail_dates(frame: pd.DataFrame, column: str, count: int) -> list[str]:
    """取 DataFrame 某列**最近 count 个不同日期**（升序）。

    为什么不用 `frame[column].unique()`：Tushare 返回的顺序不保证，
    依赖它会让"近 5 日"变成"随机 5 日"，而这类错误在单日数据上完全看不出来。
    """
    if frame is None or len(frame) == 0 or column not in frame.columns:
        return []
    values = {str(item) for item in frame[column].tolist() if item}
    return sorted(values)[-count:]


# ==================================================================
# Tushare 源
# ==================================================================


class TushareSource:
    """Tushare Pro 取数（板块指数/资金流/两融/北向/股东户数/龙虎榜/期货）。

    内部 lazy 构造 `TushareClient`：token 缺失时**构造期不抛错**，
    第一次真正调用才抛 —— 否则服务启动时一个没配 token 的环境会让整个
    资金流监控页签装配失败，而其实它还能用别的数据源。
    """

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._lock = threading.Lock()

    @property
    def client(self) -> Any:
        with self._lock:
            if self._client is None:
                from src.quant.tushare_source import TushareClient

                self._client = TushareClient()
            return self._client

    def available(self) -> tuple[bool, str]:
        """探测 token 是否可解析（不产生网络请求）。"""
        try:
            from src.quant.tushare_source import resolve_token, token_hint

            return True, token_hint(resolve_token())
        except Exception as exc:  # noqa: BLE001
            return False, brief(exc, BRIEF_DEFAULT)

    # ---------- 交易日历 ----------

    def trade_dates(self, start: str, end: str) -> list[str]:
        """区间内的交易日（升序）。取不到时回落到「工作日近似」并记日志。"""
        try:
            frame = self.client.call("trade_cal", exchange="SSE",
                                     start_date=start, end_date=end)
            if frame is not None and len(frame):
                days = frame[frame["is_open"] == 1]["cal_date"].astype(str)
                return sorted(days.tolist())
        except Exception as exc:  # noqa: BLE001
            logger.warning("交易日历取不到（回落到工作日近似）：%s",
                           brief(exc, BRIEF_TIGHT))
        stamps = pd.bdate_range(start=pd.Timestamp(start), end=pd.Timestamp(end))
        return [stamp.strftime("%Y%m%d") for stamp in stamps]

    def recent_trade_date(self, end: str = "") -> str:
        """≤ end 的最近一个交易日（end 为空取今天）。"""
        stamp = pd.Timestamp(end) if end else pd.Timestamp.today()
        start = (stamp - pd.Timedelta(days=20)).strftime("%Y%m%d")
        days = self.trade_dates(start, stamp.strftime("%Y%m%d"))
        return days[-1] if days else stamp.strftime("%Y%m%d")

    # ---------- 板块指数 ----------

    def sw_l1_series(self, *, start: str, end: str) -> dict[str, BoardSeries]:
        """申万一级行业指数日线（`sw_daily`）。

        ⚠️ `sw_daily` 每日返回 **439 行**（含二三级），必须按代码过滤出
        一级行业（801xxx）—— 不过滤会把二级行业混进"申万一级"的榜单里。
        """
        out: dict[str, BoardSeries] = {}
        try:
            frame = self.client.call("sw_daily", start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sw_daily 取数失败：%s", brief(exc, BRIEF_TIGHT))
            return out
        if frame is None or len(frame) == 0:
            return out
        wanted = ("ts_code", "trade_date", "name", "open", "high", "low",
                  "close", "pct_change", "vol", "amount")
        columns = [col for col in wanted if col in frame.columns]
        frame = frame[columns].copy()
        frame = frame[frame["ts_code"].map(_is_sw_l1)]
        for code, group in frame.groupby("ts_code"):
            rows = group.sort_values("trade_date")
            series = BoardSeries(code=str(code), source="tushare:sw_daily")
            for row in rows.to_dict("records"):
                series.bars.append(BoardBar(
                    date=str(row.get("trade_date") or ""),
                    close=_f(row.get("close")), open=_f(row.get("open")),
                    high=_f(row.get("high")), low=_f(row.get("low")),
                    volume=_f(row.get("vol")),
                    amount=_f(row.get("amount")) * _QIAN,
                    pct_change=_f(row.get("pct_change"))))
            if not series.name:
                series.name = str(rows.iloc[0].get("name") or "")
            out[series.code] = series
        return out

    def concept_series(self, codes: list[str], *, start: str, end: str
                       ) -> dict[str, BoardSeries]:
        """同花顺概念板块指数日线（`ths_daily`），按 ts_code 逐个取。

        `ths_daily` 单次 `trade_date` 调用能拿回全市场约 1878 行，但
        **无法按日期区间拿历史**（必须给 ts_code），所以历史只能逐个板块取。
        调用方应按需裁剪 `codes`（默认只取进入榜单的板块）。
        """
        out: dict[str, BoardSeries] = {}
        for code in codes:
            try:
                frame = self.client.call("ths_daily", ts_code=code,
                                         start_date=start, end_date=end)
            except Exception as exc:  # noqa: BLE001
                logger.info("ths_daily %s 失败：%s", code, brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                continue
            rows = frame.sort_values("trade_date")
            series = BoardSeries(code=str(code), source="tushare:ths_daily")
            for row in rows.to_dict("records"):
                series.bars.append(BoardBar(
                    date=str(row.get("trade_date") or ""),
                    close=_f(row.get("close")), open=_f(row.get("open")),
                    high=_f(row.get("high")), low=_f(row.get("low")),
                    volume=_f(row.get("vol")),
                    pct_change=_f(row.get("pct_change"))))
            out[str(code)] = series
        return out

    # ---------- 板块资金流（东财口径） ----------

    def board_flow_snapshot(self, trade_date: str) -> dict[str, BoardFlow]:
        """某个交易日的全市场板块资金流截面（`moneyflow_ind_dc`）。

        返回 `{板块名: BoardFlow}`（每个只有当日一个点）。行业与概念都在里面，
        用 `content_type` 区分。主力净流入 = `net_amount`（东财口径的"主力"），
        单位由万元换成元。
        """
        out: dict[str, BoardFlow] = {}
        try:
            frame = self.client.call("moneyflow_ind_dc", trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("moneyflow_ind_dc %s 失败：%s", trade_date,
                           brief(exc, BRIEF_TIGHT))
            return out
        if frame is None or len(frame) == 0:
            return out
        for row in frame.to_dict("records"):
            name = str(row.get("name") or "").strip()
            code = str(row.get("ts_code") or "").strip()
            if not name and not code:
                continue
            net = _opt(row.get("net_amount"))
            flow = BoardFlow(
                code=code or name, name=name,
                source="tushare:moneyflow_ind_dc（东财口径）",
                content_type=str(row.get("content_type") or ""),
                change_pct=_opt(row.get("pct_change")),
                net_rate=_opt(row.get("net_amount_rate")),
                rank=_opt(row.get("rank")),
                buy_elg=(_opt(row.get("buy_elg_amount")) or 0.0) * _WAN,
                buy_lg=(_opt(row.get("buy_lg_amount")) or 0.0) * _WAN)
            if net is not None:
                flow.points = [(str(row.get("trade_date") or trade_date),
                                net * _WAN)]
            out[name or code] = flow
        return out

    def board_flow_history(self, names: list[str], *, start: str, end: str,
                           chunk_days: int = 45) -> dict[str, BoardFlow]:
        """多个板块的资金流历史（`moneyflow_ind_dc` 按日期区间 + 按名过滤）。

        接口**不支持按板块名查询**，只能按日期区间拿全市场再筛 —— 因此按
        `chunk_days` 分段拉取（一次拉半年会把 1000+ 板块 × 120 天全部传回来）。
        """
        out: dict[str, BoardFlow] = {}
        wanted = {name for name in names if name}
        if not wanted:
            return out
        days = self.trade_dates(start, end)
        if not days:
            return out
        for offset in range(0, len(days), chunk_days):
            window = days[offset:offset + chunk_days]
            try:
                frame = self.client.call(
                    "moneyflow_ind_dc", start_date=window[0], end_date=window[-1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("moneyflow_ind_dc %s~%s 失败：%s",
                               window[0], window[-1], brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                continue
            frame = frame[frame["name"].astype(str).isin(wanted)]
            for row in frame.to_dict("records"):
                name = str(row.get("name") or "")
                net = _opt(row.get("net_amount"))
                if net is None:
                    continue
                flow = out.get(name)
                if flow is None:
                    flow = BoardFlow(code=str(row.get("ts_code") or name),
                                     name=name,
                                     source="tushare:moneyflow_ind_dc（东财口径）")
                    out[name] = flow
                flow.points.append((str(row.get("trade_date") or ""), net * _WAN))
        for flow in out.values():
            flow.points.sort(key=lambda item: item[0])
            flow.points = flow.points[-260:]
        return out

    # ---------- 个股资金流 ----------

    def stock_flow(self, trade_date: str) -> dict[str, StockFlow]:
        """某日全市场个股资金流（`moneyflow`，万元 → 元）。"""
        out: dict[str, StockFlow] = {}
        try:
            frame = self.client.call("moneyflow", trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("moneyflow %s 失败：%s", trade_date,
                           brief(exc, BRIEF_TIGHT))
            return out
        if frame is None or len(frame) == 0:
            return out
        net_col = ("net_mf_amount" if "net_mf_amount" in frame.columns
                   else None)
        for row in frame.to_dict("records"):
            code = to_code(row.get("ts_code") or "")
            if not code:
                continue
            net = _opt(row.get(net_col)) if net_col else None
            flow = StockFlow(code=code, source="tushare:moneyflow")
            if net is not None:
                flow.net_series = [(str(row.get("trade_date") or trade_date),
                                    net * _WAN)]
            out[code] = flow
        return out

    # ---------- 两融 / 北向 / 股东户数 / 龙虎榜 ----------

    def margin_daily(self, trade_date: str) -> list[MarginRow]:
        """某日融资融券明细（`margin_detail`，金额已是元）。"""
        try:
            frame = self.client.call("margin_detail", trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.info("margin_detail %s 失败：%s", trade_date,
                        brief(exc, BRIEF_TIGHT))
            return []
        rows: list[MarginRow] = []
        for row in (frame.to_dict("records") if frame is not None else []):
            rows.append(MarginRow(
                code=to_code(row.get("ts_code") or ""),
                date=str(row.get("trade_date") or trade_date),
                rzye=_f(row.get("rzye")), rqye=_f(row.get("rqye")),
                net_buy=_f(row.get("rzmre")) - _f(row.get("rzche"))))
        return rows

    def margin_history(self, codes: list[str], *, start: str, end: str
                       ) -> dict[str, list[tuple[str, float]]]:
        """多只个股的融资余额序列 `{code: [(date, rzye)]}`。

        接口的 ts_code 是**可选**过滤条件，但不给就得按日期拿全市场
        （单日 4400 行），因此按日期分段拉再筛指定代码。
        """
        wanted = {to_code(code) for code in codes if code}
        out: dict[str, list[tuple[str, float]]] = {}
        if not wanted:
            return out
        for day in self.trade_dates(start, end):
            try:
                frame = self.client.call("margin_detail", trade_date=day)
            except Exception as exc:  # noqa: BLE001
                logger.info("margin_detail %s 失败：%s", day,
                            brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                continue
            frame = frame.copy()
            frame["code"] = frame["ts_code"].map(to_code)
            frame = frame[frame["code"].isin(wanted)]
            for row in frame.to_dict("records"):
                out.setdefault(str(row["code"]), []).append(
                    (day, _f(row.get("rzye"))))
        for series in out.values():
            series.sort(key=lambda item: item[0])
        return out

    def northbound_daily(self, start: str, end: str) -> list[NorthboundRow]:
        """北向资金**汇总**（`moneyflow_hsgt`）。

        ⚠️ 只有沪深股通**汇总**，没有个股归属：`stock_code` 一律为空串。
        个股级持股请用 `northbound_holdings()`。
        """
        try:
            frame = self.client.call("moneyflow_hsgt", start_date=start,
                                     end_date=end)
        except Exception as exc:  # noqa: BLE001
            logger.info("moneyflow_hsgt 失败：%s", brief(exc, BRIEF_TIGHT))
            return []
        rows: list[NorthboundRow] = []
        for row in (frame.to_dict("records") if frame is not None else []):
            # Tushare 该接口的数值列是**字符串**（实测 '259865.83'）
            rows.append(NorthboundRow(
                date=str(row.get("trade_date") or ""),
                north_money=_f(row.get("north_money")),
                stock_code="", net_buy=None))
        rows.sort(key=lambda item: item.date)
        return rows

    def northbound_holdings(self, trade_date: str) -> list[tuple[str, float, float]]:
        """某日**个股级**北向持股 `[(6位代码, 持股数, 持股比例%)]`（`hk_hold`）。

        ## 三个实测确认的口径事实（2026-09，别再重复踩）

        1. **日频披露只到 2024-08-16。** 之后交易所只在**季末**披露快照。
        2. **非季末日的调用不会返回空，而是返回港股**（`00001.HK` 这类
           港股通持股）。不过滤 `.SH/.SZ/.BJ` 就会把港股写进 A 股北向表 ——
           一种不会报错、只会让"北向资金"维度整体失真的静默污染。
        3. 因此本方法**只返回 A 股**；返回空列表表示"这一天没有个股级北向
           数据"（调用方据此记缺口，而不是记 0）。

        取数成本：一次调用拿回全市场（实测单日 3000~4100 行），
        因此按日回填 2022-01→2024-08 约 650 次调用是可接受的。
        """
        try:
            frame = self.client.call("hk_hold", trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.info("hk_hold %s 失败：%s", trade_date, brief(exc, BRIEF_TIGHT))
            return []
        if frame is None or len(frame) == 0:
            return []
        out: list[tuple[str, float, float]] = []
        for row in frame.to_dict("records"):
            raw = str(row.get("ts_code") or "")
            # 只留 A 股（见口径事实 2：非季末日会返回港股）
            if not raw.endswith((".SH", ".SZ", ".BJ")):
                continue
            digits = raw.split(".")[0]
            if not digits.isdigit():
                continue
            volume = _opt(row.get("vol"))
            if volume is None:
                continue
            out.append((digits.zfill(6), volume, _opt(row.get("ratio")) or 0.0))
        return out

    def northbound_holdings_range(self, start: str, end: str
                                  ) -> dict[str, list[tuple[str, float]]]:
        """区间内逐日的个股北向持股 `{code: [(date, vol)]}`（按交易日逐日取）。

        交易日以 `trade_dates()` 为准：对自然日逐日调用会对每个周末发一次
        注定拿不到数据的请求，而"周日的港股持仓"看起来又像有效数据。
        """
        out: dict[str, list[tuple[str, float]]] = {}
        for day in self.trade_dates(start, end):
            for code, volume, _ in self.northbound_holdings(day):
                out.setdefault(code, []).append((day, volume))
        for series in out.values():
            series.sort(key=lambda item: item[0])
        return out

    def holder_number(self, codes: list[str] | None = None, *,
                      start: str = "", end: str = "") -> list[HolderRow]:
        """股东户数（`stk_holdernumber`，季报频率）。

        ⚠️ **接口不支持按报告期拿全市场**（实测 `period` 参数被忽略，
        一次调用只返回约 5500 行"最新公告"），所以这里只按 ts_code 逐个查。
        该维度权重已被压到 15% 以内，代价可接受；全市场横截面留给后续
        落库到本地仓库再读。
        """
        rows: list[HolderRow] = []
        for code in (codes or []):
            try:
                frame = self.client.call("stk_holdernumber",
                                         ts_code=to_ts_code(code))
            except Exception as exc:  # noqa: BLE001
                logger.info("stk_holdernumber %s 失败：%s", code,
                            brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                continue
            records = sorted(frame.to_dict("records"),
                             key=lambda item: str(item.get("end_date") or ""))
            previous: float | None = None
            for row in records:
                current = _opt(row.get("holder_num"))
                change = None
                if current is not None and previous not in (None, 0):
                    change = current / previous - 1.0
                rows.append(HolderRow(
                    code=to_code(row.get("ts_code") or code),
                    end_date=str(row.get("end_date") or ""),
                    ann_date=str(row.get("ann_date") or ""),
                    holder_num=current, change_ratio=change))
                previous = current if current is not None else previous
        return rows

    def seat_rows(self, trade_date: str) -> list[SeatRow]:
        """某日龙虎榜机构/营业部席位明细（`top_inst`，金额已是元）。"""
        try:
            frame = self.client.call("top_inst", trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.info("top_inst %s 失败：%s", trade_date,
                        brief(exc, BRIEF_TIGHT))
            return []
        rows: list[SeatRow] = []
        for row in (frame.to_dict("records") if frame is not None else []):
            rows.append(SeatRow(
                date=str(row.get("trade_date") or trade_date),
                code=to_code(row.get("ts_code") or ""),
                exalter=str(row.get("exalter") or ""),
                buy=_f(row.get("buy")), sell=_f(row.get("sell")),
                net_buy=_f(row.get("net_buy")),
                side=str(row.get("side") or ""),
                reason=str(row.get("reason") or "")))
        return rows

    # ---------- 期货 ----------

    def future_series(self, codes: list[str], *, start: str, end: str
                      ) -> dict[str, pd.DataFrame]:
        """内盘期货主力连续日线（`fut_daily`，传主力代码如 `RB.SHF`）。"""
        out: dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                frame = self.client.call("fut_daily", ts_code=code,
                                         start_date=start, end_date=end)
            except Exception as exc:  # noqa: BLE001
                logger.info("fut_daily %s 失败：%s", code, brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                continue
            out[str(code)] = frame.sort_values("trade_date").reset_index(drop=True)
        return out

    def future_daily_all(self, trade_date: str) -> pd.DataFrame:
        """某日全部期货合约（含主力代码）行情，用于品种名录发现。"""
        try:
            frame = self.client.call("fut_daily", trade_date=trade_date)
            return frame if frame is not None else pd.DataFrame()
        except Exception as exc:  # noqa: BLE001
            logger.info("fut_daily %s 失败：%s", trade_date, brief(exc, BRIEF_TIGHT))
            return pd.DataFrame()

    def index_series(self, code: str, *, start: str, end: str) -> BoardSeries:
        """指数日线（`index_daily`），用于基准与板块映射。"""
        series = BoardSeries(code=code, source="tushare:index_daily")
        try:
            frame = self.client.call("index_daily", ts_code=code,
                                     start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001
            series.gap = f"指数 {code} 取数失败：{brief(exc, BRIEF_TIGHT)}"
            return series
        if frame is None or len(frame) == 0:
            series.gap = f"指数 {code} 无数据"
            return series
        for row in frame.sort_values("trade_date").to_dict("records"):
            series.bars.append(BoardBar(
                date=str(row.get("trade_date") or ""),
                close=_f(row.get("close")), open=_f(row.get("open")),
                high=_f(row.get("high")), low=_f(row.get("low")),
                volume=_f(row.get("vol")),
                amount=_f(row.get("amount")) * _QIAN,
                pct_change=_f(row.get("pct_chg"))))
        return series


# ==================================================================
# 本地仓库源
# ==================================================================


class WarehouseSource:
    """本地行情仓库的**只读**横截面读取（`data/quant/warehouse.db`）。

    为什么走本地而不是 Tushare：仓库里 `quant_daily` 有 1539 万行、覆盖
    2006 年至昨日，且 `(trade_date, code)` 上有索引 —— 实测单日全市场查询
    与 3 周区间查询都在毫秒级。要走 Tushare 拿同样数据得发上百次请求。

    只读连接（`mode=ro`）：这个库是 15 GiB 的行情仓库，绝不能被本模块写入。
    """

    def __init__(self, path: str | Path = "data/quant/warehouse.db") -> None:
        self.path = Path(path)
        self._available: bool | None = None
        self._gap = ""
        #: 退化到 `immutable=1` 的次数（保留给监控/自检）。
        #:
        #: ⚠️ **实际的状态不在这里**：模式由 `warehouse.open_warehouse` 统一
        #: 在进程级记住（`warehouse.prefer_immutable()`）。这个计数只反映
        #: 本对象自建立以来观察到的情况，别再拿它当唯一判据。
        self.fallback_count = 0

    def available(self) -> bool:
        if self._available is None:
            self._available = self.path.exists()
            if not self._available:
                self._gap = (f"本地行情仓库不存在：{self.path}"
                             "（执行 scripts/quant_warehouse.py 建库）")
        return bool(self._available)

    @property
    def gap(self) -> str:
        self.available()
        return self._gap

    def _connect(self, *, immutable: bool = False) -> sqlite3.Connection:
        """只读连接 —— 委托给唯一入口 `warehouse.open_warehouse`。

        `WarehouseSource` 是第一个遇到 `disk I/O error` 的地方（见
        `_query` 的说明），当时在这里就地加了 `immutable=1` 兜底。
        后来同一类 bug 又在 `relevance.py`（提纯链路静默失效）和
        `member_pure.load_market_caps`（提纯重建直接崩）各出现一次 ——
        说明"就地打补丁"解决不了问题，**唯一入口**才行。

        现在这里只是薄封装：`immutable=True` 时强制走 immutable
        （`warehouse.open_warehouse` 会在正常路径失败时自动切换）。
        """
        _ = immutable          # 兼容旧签名；模式由 open_warehouse 统一决定
        return open_warehouse(self.path, timeout=10.0)

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        if not self.available():
            return []
        try:
            with self._connect() as connection:
                return connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            # 不在这里吞掉 I/O 类错误：`open_warehouse` 已经处理过退化，
            # 走到这里说明连 immutable 也失败 —— 那是真的读不到，
            # 必须让调用方知道（返回空列表会让它静默丢数据）。
            logger.warning("本地仓库查询失败：%s", brief(exc, BRIEF_TIGHT))
            return []

    # ---------- 个股 ----------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """只读执行一条 SQL（公开入口；调用方负责保证是 SELECT）。

        为什么公开这个方法而不是让调用方碰 `_query`：板块级资金流要把成分股
        **按板块聚合**，聚合逻辑（`GROUP BY trade_date` 的 SUM）必须写在
        调用侧（不同板块的分组方式不同），但连接管理与只读约束必须留在本类。

        ⚠️ **本库的金额字段已经是「元」**（2026-09 逐票比对 Tushare 原始值确认）：

            quant_moneyflow.net_mf_amount / buy_*  = Tushare 原始万元 × 1e4
            quant_daily.amount                     = Tushare 原始千元 × 1e3
            quant_daily_basic.circ_mv / total_mv   = Tushare 原始万元 × 1e4

        与 Tushare 原始接口的口径**不同**，读本库时不要按原始口径再乘一次。
        """
        return self._query(sql, tuple(params))

    def stock_series(self, codes: list[str], *, start: str, end: str
                     ) -> dict[str, StockFlow]:
        """多只个股的日线 + 资金流 + 流通市值（一次 JOIN 拿齐）。

        为什么合成一个方法：五维打分要用"净流入 / 成交额 / 市值"三个比值，
        分三次查同一批代码会把 IO 翻三倍，而它们天然来自同一批日期。
        """
        wanted = [to_code(code) for code in codes if code]
        if not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        sql = f"""
            SELECT d.code, d.trade_date, d.close, d.pct_chg, d.amount,
                   b.circ_mv, b.total_mv, m.net_mf_amount
            FROM quant_daily d
            LEFT JOIN quant_daily_basic b
                   ON b.code = d.code AND b.trade_date = d.trade_date
            LEFT JOIN quant_moneyflow m
                   ON m.code = d.code AND m.trade_date = d.trade_date
            WHERE d.code IN ({marks}) AND d.trade_date BETWEEN ? AND ?
            ORDER BY d.code, d.trade_date
        """
        rows = self._query(sql, (*wanted, start, end))
        out: dict[str, StockFlow] = {}
        for row in rows:
            code = str(row["code"])
            flow = out.get(code)
            if flow is None:
                flow = StockFlow(code=code, source="local:quant_warehouse")
                out[code] = flow
            date = str(row["trade_date"])
            net = _opt(row["net_mf_amount"])
            if net is not None:
                flow.net_series.append((date, net))
            flow.close_series.append((date, _f(row["close"])))
            flow.amount_series.append((date, _f(row["amount"])))
            circ = _opt(row["circ_mv"])
            if circ is not None:
                flow.circ_mv = circ
            total = _opt(row["total_mv"])
            if total is not None:
                flow.total_mv = total
        return out

    def stock_names(self, codes: list[str]) -> dict[str, str]:
        wanted = [to_code(code) for code in codes if code]
        if not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        rows = self._query(
            f"SELECT code, name FROM quant_stock_basic WHERE code IN ({marks})",
            tuple(wanted))
        return {str(row["code"]): str(row["name"] or "") for row in rows}

    def board_amount(self, date: str) -> float:
        """某日全市场成交额（元），用于板块成交额占比类指标。"""
        rows = self._query(
            "SELECT SUM(amount) AS total FROM quant_daily WHERE trade_date = ?",
            (date,))
        return _f(rows[0]["total"]) if rows else 0.0

    def index_closes(self, code: str, *, start: str, end: str
                     ) -> list[tuple[str, float]]:
        rows = self._query(
            "SELECT trade_date, close FROM quant_index_daily "
            "WHERE code = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date", (to_code(code), start, end))
        return [(str(row["trade_date"]), _f(row["close"])) for row in rows]

    def market_fund_points(self, date: str, limit: int = 6000) -> list[FundPoint]:
        """某日全市场个股截面（收盘/涨跌幅/市值/成交额）。

        `limit` 是防御性上限：正常全市场约 5200 只，设成 6000 既够用又能
        在表结构异常（重复行）时快速暴露而不是把内存吃满。
        """
        rows = self._query(
            """
            SELECT d.code, d.trade_date, d.close, d.pct_chg, d.amount,
                   b.circ_mv, b.turnover_rate
            FROM quant_daily d
            LEFT JOIN quant_daily_basic b
                   ON b.code = d.code AND b.trade_date = d.trade_date
            WHERE d.trade_date = ?
            LIMIT ?
            """, (date, limit))
        return [FundPoint(
            code=str(row["code"]), date=str(row["trade_date"]),
            close=_f(row["close"]), pct_chg=_f(row["pct_chg"]),
            circ_mv=_opt(row["circ_mv"]), amount=_f(row["amount"]),
            turnover_rate=_opt(row["turnover_rate"])) for row in rows]

    def latest_trade_date(self) -> str:
        rows = self._query(
            "SELECT MAX(trade_date) AS last FROM quant_daily", ())
        return str(rows[0]["last"] or "") if rows and rows[0]["last"] else ""


# ==================================================================
# 板块目录
# ==================================================================


class BoardCatalog:
    """可评分板块目录：申万一级行业 + 同花顺概念。

    概念名录复用项目既有的 `ConceptRepository`（它已经把 `ths_index` 的
    8 个查询变体合并成 2517 条并落库缓存 24 小时）—— 重复实现只会让
    两份名录慢慢漂移。行业名录在 `sw_daily` 的返回里就地发现。
    """

    def __init__(self, config: MainlineConfig | None = None,
                 source: TushareSource | None = None) -> None:
        self.config = config or load_config()
        self.source = source or TushareSource()
        self._cache: dict[str, Any] = {"at": 0.0, "boards": []}

    def sw_l1_boards(self, trade_date: str = "") -> list[BoardInfo]:
        """申万一级行业目录（取最近交易日 sw_daily 的返回值）。"""
        day = trade_date or self.source.recent_trade_date()
        out: list[BoardInfo] = []
        try:
            frame = self.source.client.call("sw_daily", trade_date=day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("申万行业目录取数失败：%s", brief(exc, BRIEF_TIGHT))
            return out
        if frame is None or len(frame) == 0:
            return out
        seen: set[str] = set()
        for row in frame.to_dict("records"):
            code = str(row.get("ts_code") or "")
            if not _is_sw_l1(code) or code in seen:
                continue
            seen.add(code)
            out.append(BoardInfo(code=code, name=str(row.get("name") or code),
                                 kind=BoardKind.SW_L1, source="tushare:sw_daily"))
        return out

    def concept_boards(self) -> list[BoardInfo]:
        """同花顺概念板块目录（带成分股数，用于过滤宽指数）。"""
        data = self.config.data
        out: list[BoardInfo] = []
        try:
            from src.quant.concept_repo import concept_repository

            index = concept_repository().index()
        except Exception as exc:  # noqa: BLE001
            logger.warning("概念名录取数失败：%s", brief(exc, BRIEF_TIGHT))
            return out
        blocked = tuple(getattr(data, "exclude_keywords", ()) or ())
        for code, (name, members) in index.items():
            if not (data.min_members <= members <= data.max_members):
                continue
            # 宽基/风格指数不是题材主线（理由见 `DataConfig.exclude_keywords`）
            if blocked and any(word and word in str(name) for word in blocked):
                continue
            out.append(BoardInfo(code=str(code), name=str(name),
                                 kind=BoardKind.CONCEPT, members=int(members),
                                 source="tushare:ths_index"))
        out.sort(key=lambda item: item.name)
        return out

    def all_boards(self, *, trade_date: str = "", force: bool = False
                   ) -> list[BoardInfo]:
        """全部可评分板块（缓存 `catalog_ttl_hours` 小时）。"""
        ttl = max(60.0, self.config.data.catalog_ttl_hours * 3600.0)
        now = time.monotonic()
        if (not force and self._cache["boards"]
                and now - float(self._cache["at"]) < ttl):
            return list(self._cache["boards"])
        boards: list[BoardInfo] = []
        if self.config.universe.include_sw_l1:
            boards.extend(self.sw_l1_boards(trade_date))
        if self.config.universe.include_concept:
            boards.extend(self.concept_boards())
        if boards:
            self._cache.update(at=now, boards=boards)
        return boards

    def members_of(self, board: BoardInfo) -> list[str]:
        """板块成分股（6 位代码）。

        两条路径，实测确认（2026-09-20）：

        - **概念**：`ths_member(ts_code=概念代码)`。该接口同时支持
          「按概念查成分股」与「按个股反查概念」（`con_code=`），项目既有的
          `ConceptRepository` 用的是后者，这里用前者。
        - **申万一级行业**：`index_member_all(l1_code="801010.SI")`，返回
          126 行 `[l1_code, l1_name, l2_code, ..., ts_code, name, is_new]`，
          必须按 `is_new == 'Y'` 过滤掉**历史成分股**（实测 801010 里
          184 行有相当一部分是已调出的旧成分，不过滤会把早已不属于该行业的
          票算进板块资金流）。

        取不到一律返回空列表（打分侧按"缺少成分股"记缺口，不静默当 0）。
        """
        if board.kind is BoardKind.CONCEPT:
            try:
                frame = self.source.client.call("ths_member",
                                                ts_code=board.code)
            except Exception as exc:  # noqa: BLE001
                logger.info("ths_member %s 失败：%s", board.code,
                            brief(exc, BRIEF_TIGHT))
                return []
            if frame is None or len(frame) == 0:
                return []
            column = "con_code" if "con_code" in frame.columns else "ts_code"
            return [to_code(item) for item in frame[column].tolist()
                    if str(item).strip()]
        try:
            frame = self.source.client.call("index_member_all",
                                            l1_code=board.code)
        except Exception as exc:  # noqa: BLE001
            logger.info("index_member_all %s 失败：%s", board.code,
                        brief(exc, BRIEF_TIGHT))
            return []
        if frame is None or len(frame) == 0:
            return []
        if "is_new" in frame.columns:
            # `is_new` 实测是字符串 'Y'/'N'；缺失值视作非当前成分（保守）
            frame = frame[frame["is_new"].astype(str).str.upper() == "Y"]
        column = "ts_code" if "ts_code" in frame.columns else "con_code"
        codes = {to_code(item) for item in frame[column].tolist()
                 if str(item).strip()}
        return sorted(codes)


# ==================================================================
# 东方财富免费接口
# ==================================================================


class EastmoneySource:
    """东方财富板块资金流免费接口（限速 ≥1 秒）。

    - 日线历史：`push2his.eastmoney.com/api/qt/stock/fflow/daykline/get`
    - 当日分钟：`push2.eastmoney.com/api/qt/stock/fflow/kline/get`（klt=1）

    返回字段顺序（实测）：时间戳, 主力净流入, 小单, 中单, 大单, 超大单（元）。

    本机网络对东财 push2* 有 TLS SNI 阻断的历史（见项目 `eastmoney_direct`
    相关模块），因此这里**允许失败**：调用方应在 `moneyflow_ind_dc` 可用时
    优先走它，本类只作为历史补齐与降级通道。
    """

    DAYKLINE = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    MINKLINE = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"

    def __init__(self, config: MainlineConfig | None = None) -> None:
        self.config = config or load_config()
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        """请求间隔 ≥ `eastmoney_min_interval` 秒（需求 2.2 明确要求）。"""
        with self._lock:
            wait = self.config.data.eastmoney_min_interval - (
                time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self._throttle()
        try:
            import requests

            response = requests.get(
                url, params=params,
                timeout=self.config.data.eastmoney_timeout,
                headers={"User-Agent": "Mozilla/5.0", "Referer":
                         "https://data.eastmoney.com/"})
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 网络源失败一律降级
            logger.info("东财接口失败(%s)：%s", url.rsplit("/", 1)[-1],
                        brief(exc, BRIEF_TIGHT))
            return None

    def board_history(self, secid: str, *, days: int = 260
                      ) -> list[tuple[str, float]]:
        """板块日线资金流历史 `[(YYYYMMDD, 主力净流入元)]`。

        `secid` 形如 `90.BK1182`（板块）或 `1.600519`（沪市个股）/`0.000001`。
        """
        payload = self._get(self.DAYKLINE, {
            "secid": secid, "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56", "klt": 101, "lmt": days})
        if not payload:
            return []
        data = (payload.get("data") or {})
        klines = data.get("klines") or []
        out: list[tuple[str, float]] = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            stamp = parts[0].replace("-", "")
            out.append((stamp, _f(parts[1])))
        return out

    def board_minutes(self, secid: str, *, klt: int = 1) -> list[tuple[str, float]]:
        """板块当日分钟级主力净流入（盘中监测用）。

        `klt=1` 分钟级、`klt=101` 日级。
        """
        payload = self._get(self.MINKLINE, {
            "secid": secid, "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56", "klt": klt, "lmt": 0})
        if not payload:
            return []
        data = (payload.get("data") or {})
        klines = data.get("klines") or []
        out: list[tuple[str, float]] = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            out.append((str(parts[0]), _f(parts[1])))
        return out


# ==================================================================
# 组合句柄
# ==================================================================


class DataSources:
    """数据源组合（一次构造，供 service 注入；便于测试整体替换）。"""

    def __init__(self, *, config: MainlineConfig | None = None,
                 tushare: TushareSource | None = None,
                 warehouse: WarehouseSource | None = None,
                 eastmoney: EastmoneySource | None = None,
                 catalog: BoardCatalog | None = None) -> None:
        self.config = config or load_config()
        self.tushare = tushare or TushareSource()
        self.warehouse = warehouse or WarehouseSource()
        self.eastmoney = eastmoney or EastmoneySource(self.config)
        self.catalog = catalog or BoardCatalog(self.config, self.tushare)

    def health(self) -> dict[str, Any]:
        """数据源可用性摘要（供接口的 gaps/source_notes 使用）。"""
        ok, hint = self.tushare.available()
        return {
            "tushare": {"available": ok, "token": hint},
            "warehouse": {"available": self.warehouse.available(),
                          "path": str(self.warehouse.path),
                          "latest": (self.warehouse.latest_trade_date()
                                     if self.warehouse.available() else ""),
                          "gap": self.warehouse.gap},
            "eastmoney": {"available": True, "note": "免费接口，限速 ≥1 秒"},
        }

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """把阻塞取数丢线程池（接口层统一用它，避免在事件循环里卡网络）。"""
        return await asyncio.to_thread(fn, *args, **kwargs)


__all__ = [
    "BoardCatalog",
    "DataSources",
    "EastmoneySource",
    "FutureKind",
    "TushareSource",
    "WarehouseSource",
    "to_code",
    "to_ts_code",
]
