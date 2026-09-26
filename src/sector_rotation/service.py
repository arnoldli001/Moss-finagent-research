"""行业轮动报告的数据装配与规则研判。

三层结构，刻意分开：
  1. **取数**（`fetch_*`）：腾讯指数快照 / Tushare 板块截面 / akshare 大盘资金流，
     各自独立失败（一个源挂掉只损失对应板块，不拖垮整份报告）；
  2. **装配**（`assemble`）：纯函数，把原始数据整理成报告 JSON —— 可离线测试；
  3. **研判**（`narrate`）：纯函数，规则化生成"主线 + 三个方向"的文字 ——
     不用 LLM：每日定时任务要求确定性与可复现，规则生成的结论与数据必然一致，
     不存在"数字与文字对不上"的幻觉风险。文案里显式标注"规则生成"。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any

import pandas as pd

from src.core.errors import BRIEF_DEFAULT, BRIEF_TIGHT, brief
from src.intraday.subproc import run_json_subprocess
from src.sector_rotation import store

logger = logging.getLogger(__name__)

YI = 100_000_000.0

#: 报告跟踪的指数（腾讯快照代码 → 展示名）。前两个的成交额之和即"两市成交额"。
INDEX_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000016", "上证50"),
    ("sh000300", "沪深300"),
)

#: 腾讯快照 `~` 分隔字段下标（与 fundflow.provider 同源实测）
_TX_NAME, _TX_PRICE, _TX_CHG, _TX_PCT, _TX_DATETIME, _TX_AMOUNT_WAN = 1, 3, 31, 32, 30, 37


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not math.isfinite(number):
        return None
    return number


def _yi(value: float | None) -> float | None:
    """元 → 亿元（保留 2 位；None 透传）。"""
    return round(value / YI, 2) if value is not None else None


# ==================== 取数：腾讯指数快照 ====================

async def fetch_index_quotes() -> dict[str, dict[str, Any]]:
    """主要指数快照：`{code: {name, price, change_pct, amount_yi, datetime}}`。

    与 fundflow 的腾讯个股快照同一接口（qt.gtimg.cn），实测指数代码同样可用；
    失败返回空 dict（指数卡片整块降级为"暂缺"，不影响行业部分）。
    """
    import httpx

    symbols = [code for code, _ in INDEX_SYMBOLS]
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": "https://gu.qq.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get("https://qt.gtimg.cn/q=" + ",".join(symbols))
        response.encoding = "gbk"
        text = response.text
    except Exception as exc:  # noqa: BLE001
        logger.info("行业轮动：腾讯指数快照不可用：%s", brief(exc, BRIEF_TIGHT))
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in text.split("\n"):
        if "=" not in line or '"' not in line:
            continue
        try:
            payload = line.split('="', 1)[1].rstrip('";')
        except IndexError:
            continue
        fields = payload.split("~")
        if len(fields) < 38:
            continue
        symbol = line.split("=", 1)[0].strip().lstrip("v_")
        if symbol not in symbols:
            continue
        amount_wan = _finite(fields[_TX_AMOUNT_WAN])
        result[symbol] = {
            "name": fields[_TX_NAME].strip(),
            "price": _finite(fields[_TX_PRICE]),
            "change_pct": _finite(fields[_TX_PCT]),
            # 腾讯成交额单位是万元 → 亿元
            "amount_yi": round(amount_wan / 10000.0, 2) if amount_wan is not None else None,
            "datetime": fields[_TX_DATETIME].strip(),
            "source": "腾讯行情(qt.gtimg.cn)",
        }
    logger.info("行业轮动：指数快照 %d/%d", len(result), len(symbols))
    return result


# ==================== 取数：akshare 大盘资金流 + 赚钱效应（子进程隔离） ====================

async def fetch_market_flow() -> dict[str, Any]:
    """大盘主力资金流近 25 个交易日 + 涨跌家数（akshare，子进程隔离）。

    `stock_market_fund_flow` 是日频（收盘后更新当天），单位元；
    `stock_market_activity_legu` 给上涨/下跌/涨停/跌停家数。
    两者一次子进程取回，失败返回空结构。
    """
    payload = await run_json_subprocess(
        """
import akshare as ak
out = {}
try:
    df = ak.stock_market_fund_flow().tail(25)
    out["flow"] = [{
        "date": str(r["日期"])[:10],
        "sh_close": r["上证-收盘价"], "sh_pct": r["上证-涨跌幅"],
        "net": r["主力净流入-净额"],
        "elg": r["超大单净流入-净额"], "lg": r["大单净流入-净额"],
    } for _, r in df.iterrows()]
except Exception as exc:
    out["flow_error"] = f"{type(exc).__name__}: {exc}"
try:
    act = ak.stock_market_activity_legu()
    out["breadth"] = {str(r["item"]): r["value"] for _, r in act.iterrows()}
except Exception as exc:
    out["breadth_error"] = f"{type(exc).__name__}: {exc}"
__emit(out)
""",
        timeout=90.0, label="行业轮动-大盘资金流(akshare)")
    if not payload:
        logger.info("行业轮动：大盘资金流子进程无返回")
        return {"series": [], "breadth": {}}
    series = [
        {"date": str(row.get("date") or ""),
         "net_yi": _yi(_finite(row.get("net"))),
         "elg_yi": _yi(_finite(row.get("elg"))),
         "lg_yi": _yi(_finite(row.get("lg")))}
        for row in payload.get("flow") or []
    ]
    series = [row for row in series if row["date"] and row["net_yi"] is not None]
    breadth_raw = payload.get("breadth") or {}
    breadth = {
        "up": int(_finite(breadth_raw.get("上涨")) or 0),
        "down": int(_finite(breadth_raw.get("下跌")) or 0),
        "limit_up": int(_finite(breadth_raw.get("涨停")) or 0),
        "limit_down": int(_finite(breadth_raw.get("跌停")) or 0),
    }
    for key in ("flow_error", "breadth_error"):
        if payload.get(key):
            logger.info("行业轮动：%s = %s", key, str(payload[key])[:120])
    return {"series": series, "breadth": breadth}


# ==================== 取数：Tushare 板块截面（东财口径，复用资金流监控同源链路） ====================

def _tushare_client() -> Any:
    from src.quant.tushare_source import TushareClient

    return TushareClient()


def fetch_sector_frame() -> pd.DataFrame:
    """东财板块截面（最新交易日全量）：name/ts_code/pct_change/net_amount/...。

    口径与 `fundflow.provider._sector_frame_sync` 完全一致（同一接口同一坑位）：
    不传 trade_date 会返回多日拼接，必须按 ts_code 去重保留最新一行。
    """
    frame = _tushare_client().call("moneyflow_ind_dc")
    if frame is None or len(frame) == 0:
        return pd.DataFrame()
    frame = frame.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    return (frame.sort_values("trade_date")
            .drop_duplicates(subset=["ts_code"], keep="last"))


def fetch_flow_5d(boards: list[dict[str, Any]]) -> dict[str, float | None]:
    """指定板块近 5 个交易日主力净额之和：`{ts_code: 亿元}`。

    逐板块一次 `moneyflow_ind_dc(ts_code=...)`（实测单次返回约 730 行日频），
    只给榜单上那 ≤10 个板块取，日更批量场景 10~20 秒可接受。
    单板块失败只损失那一行（返回 None），不拖垮整份报告。
    """
    client = _tushare_client()
    result: dict[str, float | None] = {}
    for board in boards:
        code = str(board.get("code") or "")
        if not code:
            continue
        try:
            frame = client.call("moneyflow_ind_dc", ts_code=code)
            if frame is None or len(frame) == 0:
                result[code] = None
                continue
            frame = frame.copy()
            frame["trade_date"] = frame["trade_date"].astype(str)
            tail = frame.sort_values("trade_date").tail(5)
            total = pd.to_numeric(tail["net_amount"], errors="coerce").sum()
            result[code] = round(float(total) / YI, 2) if math.isfinite(float(total)) else None
        except Exception as exc:  # noqa: BLE001
            logger.info("行业轮动：板块5日净额失败(%s)：%s", code, brief(exc, BRIEF_TIGHT))
            result[code] = None
        time.sleep(0.3)  # Tushare 限频友好：10 个板块也就多 3 秒
    return result


# ==================== 装配（纯函数，可离线测试） ====================

def board_level(name: str) -> int:
    """东财板块层级：名称后缀 Ⅲ→3、Ⅱ→2、其余→1（东财行业口径，非申万）。"""
    text = str(name)
    if text.endswith("Ⅲ"):
        return 3
    if text.endswith("Ⅱ"):
        return 2
    return 1


def assemble_boards(frame: pd.DataFrame) -> tuple[str, list[dict[str, Any]]]:
    """板块截面 → (交易日, 行业板块列表)。

    只保留 `content_type` 含"行业"的板块；嵌套子行业（如 林业Ⅱ/林业Ⅲ
    涨跌幅与净额完全相同时）去重，保留层级高、名称短的那一条，避免
    热力图上同一板块出现两次。
    """
    if frame is None or len(frame) == 0:
        return "", []
    industry = frame[frame["content_type"].astype(str).str.contains("行业", na=False)]
    trade_date = str(industry["trade_date"].max()) if len(industry) else ""
    boards: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    rows = sorted(
        industry.iterrows(),
        key=lambda item: (board_level(item[1].get("name")), len(str(item[1].get("name")))))
    for _, row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        pct = _finite(row.get("pct_change"))
        net = _finite(row.get("net_amount"))
        key = (round(pct or 0.0, 2), round(net or 0.0, 2))
        if key in seen:
            continue
        seen.add(key)
        boards.append({
            "name": name,
            "code": str(row.get("ts_code") or ""),
            "level": board_level(name),
            "pct": round(pct, 2) if pct is not None else None,
            "net_yi": _yi(net),
        })
    boards.sort(key=lambda item: -(item["pct"] if item["pct"] is not None else -999))
    return trade_date, boards


def pick_heat(boards: list[dict[str, Any]], *, top: int = 10, bottom: int = 10) -> list[dict[str, Any]]:
    """热力图板块：涨幅前 top + 跌幅前 bottom（按涨跌幅排序的列表首尾取）。"""
    valid = [board for board in boards if board.get("pct") is not None]
    if len(valid) <= top + bottom:
        return valid
    picked = valid[:top] + valid[-bottom:]
    return sorted(picked, key=lambda item: -(item["pct"] or 0))


def assemble(
    *,
    trade_date: str,
    boards: list[dict[str, Any]],
    indices: dict[str, dict[str, Any]],
    market_flow: dict[str, Any],
    flow_5d: dict[str, float | None],
    prev_turnover_yi: float | None,
) -> dict[str, Any]:
    """把各源数据装配成报告 JSON（纯函数）。"""
    index_cards = []
    for code, display in INDEX_SYMBOLS:
        quote = indices.get(code)
        if not quote:
            continue
        index_cards.append({
            "code": code,
            "name": display,
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "amount_yi": quote.get("amount_yi"),
        })
    turnover = None
    if indices.get("sh000001", {}).get("amount_yi") is not None \
            and indices.get("sz399001", {}).get("amount_yi") is not None:
        turnover = round(indices["sh000001"]["amount_yi"]
                         + indices["sz399001"]["amount_yi"], 2)
    turnover_delta = (round(turnover - prev_turnover_yi, 2)
                      if turnover is not None and prev_turnover_yi else None)

    level1 = [board for board in boards if board["level"] == 1]
    flow_pool = level1 if len(level1) >= 10 else boards
    by_net = [board for board in flow_pool if board.get("net_yi") is not None]
    inflow = sorted(by_net, key=lambda item: -(item["net_yi"] or 0))[:5]
    outflow = sorted(by_net, key=lambda item: (item["net_yi"] or 0))[:5]

    flow_5d_rows = []
    for board in inflow + outflow:
        code = board["code"]
        flow_5d_rows.append({
            "name": board["name"], "code": code,
            "pct": board.get("pct"), "net_yi": board.get("net_yi"),
            "net5_yi": flow_5d.get(code),
        })

    flow_series = market_flow.get("series") or []
    breadth = market_flow.get("breadth") or {}
    payload: dict[str, Any] = {
        "meta": {
            "trade_date": trade_date,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": 1,
            "narrative_engine": "rule-based-v1",
            "sources": [
                "指数/成交额：腾讯行情 qt.gtimg.cn（实时快照）",
                "行业板块涨跌幅与主力净额：Tushare moneyflow_ind_dc（东财板块口径，日频）",
                "大盘主力资金流/涨跌家数：akshare（东财/乐咕，日频）",
            ],
        },
        "indices": index_cards,
        "market": {
            "turnover_yi": turnover,
            "turnover_prev_yi": prev_turnover_yi,
            "turnover_delta_yi": turnover_delta,
            **{key: breadth.get(key) for key in ("up", "down", "limit_up", "limit_down")},
        },
        "market_flow": {
            "latest": flow_series[-1] if flow_series else None,
            "series": flow_series[-20:],
        },
        "heat": pick_heat(boards),
        "flow_1d": {"inflow": inflow, "outflow": outflow},
        "flow_5d": flow_5d_rows,
        "industries": boards,
    }
    payload["narrative"] = narrate(payload)
    return payload


# ==================== 研判（纯函数：规则生成，不用 LLM） ====================

def _fmt_yi(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}亿"


def _names(boards: list[dict[str, Any]], count: int = 3) -> str:
    return "、".join(board["name"] for board in boards[:count]) or "—"


def narrate(payload: dict[str, Any]) -> dict[str, Any]:
    """规则化研判：结论完全由 payload 里的数字推导，可复现、可测试。

    输出三段：headline（一句话主线）、body（数据展开）、views（三个方向卡片）。
    所有文字都标注"规则生成"，不做任何超出数据的推断。
    """
    market = payload.get("market") or {}
    up, down = market.get("up") or 0, market.get("down") or 0
    total = up + down
    up_ratio = (up / total) if total else None

    if up_ratio is None:
        mood = "涨跌家数暂缺"
    elif up_ratio >= 0.65:
        mood = "普涨"
    elif up_ratio <= 0.35:
        mood = "普跌"
    else:
        mood = "分化"

    latest_flow = (payload.get("market_flow") or {}).get("latest") or {}
    flow_net = latest_flow.get("net_yi")

    pct_map = {card["name"]: card.get("change_pct") for card in payload.get("indices") or []}
    value_pct, growth_pct = pct_map.get("上证50"), pct_map.get("创业板指")
    style = "风格均衡"
    if value_pct is not None and growth_pct is not None:
        spread = value_pct - growth_pct
        if spread >= 1.5:
            style = f"大盘价值占优（上证50 较创业板指 +{spread:.1f}pct）"
        elif spread <= -1.5:
            style = f"成长占优（创业板指 较上证50 +{-spread:.1f}pct）"

    turnover = market.get("turnover_yi")
    delta = market.get("turnover_delta_yi")
    turnover_txt = "成交额暂缺"
    if turnover is not None:
        turnover_txt = f"成交 {turnover / 10000:.2f} 万亿"
        if delta is not None:
            turnover_txt += f"（较上日 {'放量' if delta > 0 else '缩量'} {abs(delta):.0f} 亿）"

    heat = payload.get("heat") or []
    leaders = [b for b in heat if (b.get("pct") or 0) > 0][:3]
    laggards = [b for b in reversed(heat) if (b.get("pct") or 0) < 0][:3]
    inflow = (payload.get("flow_1d") or {}).get("inflow") or []
    outflow = (payload.get("flow_1d") or {}).get("outflow") or []

    headline = f"{mood} · 主力{_fmt_yi(flow_net)} · {style}"
    body = (
        f"上涨 {up} 家 / 下跌 {down} 家，涨停 {market.get('limit_up') or '—'} 家；"
        f"{turnover_txt}。"
        f"领涨：{_names(leaders)}；领跌：{_names(laggards)}。"
        f"主力净流入居前：{_names(inflow)}；净流出居前：{_names(outflow)}。"
        "提示：单日流向多为噪音，连续性以近 5 日口径验证。"
    )

    flow5 = {row["name"]: row.get("net5_yi") for row in payload.get("flow_5d") or []}

    def continuity(name: str) -> str:
        net5 = flow5.get(name)
        if net5 is None:
            return "5日数据暂缺"
        return "近5日同向，信号加强" if net5 > 0 else "近5日仍流出，按单日反弹对待"

    views = [
        {
            "level": "in",
            "title": f"资金流入方向：{_names(inflow)}",
            "text": (
                f"当日主力净流入居前（{', '.join(f'{b['name']}{_fmt_yi(b.get('net_yi'))}' for b in inflow[:3])}）。"
                f"{continuity(inflow[0]['name']) if inflow else ''}"
                "单日流入不改变配置结论，需观察 2~3 日连续性。"),
        },
        {
            "level": "out",
            "title": f"资金流出方向：{_names(outflow)}",
            "text": (
                f"当日主力净流出居前（{', '.join(f'{b['name']}{_fmt_yi(b.get('net_yi'))}' for b in outflow[:3])}）。"
                "流出居前 ≠ 基本面恶化，先分清是获利兑现还是趋势撤退——"
                "看该方向近 5 日是否持续流出、以及板块内龙头是否同步走弱。"),
        },
    ]
    # 情绪博弈方向：涨幅靠前但主力净流出的板块（涨而无钱，持续性存疑）
    hot_no_money = [b for b in heat if (b.get("pct") or 0) > 1.0
                    and (b.get("net_yi") or 0) < 0][:3]
    if hot_no_money:
        views.append({
            "level": "watch",
            "title": f"情绪博弈方向：{_names(hot_no_money)}",
            "text": (
                f"{_names(hot_no_money)}涨幅靠前但主力净流出"
                f"（{', '.join(_fmt_yi(b.get('net_yi')) for b in hot_no_money)}）——"
                "涨而无钱，多为事件/情绪驱动的短打，追高性价比差，按事件交易对待。"),
        })
    else:
        strong = [b for b in heat if (b.get("pct") or 0) > 1.0
                  and (b.get("net_yi") or 0) > 0][:3]
        views.append({
            "level": "watch",
            "title": f"量价齐升方向：{_names(strong) if strong else '—'}",
            "text": (
                (f"{_names(strong)}上涨且主力净流入为正，量价配合良好，"
                 "是当日最扎实的方向；仍建议以近 5 日资金连续性验证后再提升仓位优先级。"
                 if strong else
                 "当日无'涨幅>1%且主力净流入'的方向，市场缺乏量价共振主线，"
                 "整体按存量博弈对待，控制追高动作。")),
        })
    return {"headline": headline, "body": body, "views": views,
            "note": "规则生成（narrative_engine=rule-based-v1），结论完全由本页数据推导，非投资建议"}


# ==================== 编排 ====================

#: 盘中自动再生成的最小间隔（收盘后一天一份，不重复生成）
REGEN_TTL_SECONDS = 900.0


async def generate(*, force: bool = False) -> dict[str, Any]:
    """生成最近交易日报告：取数 → 装配 → 研判 → 落盘，返回报告 JSON。

    幂等策略：
      - 已落盘且 `force=False` → 直接返回落盘那份；
      - 各数据源独立失败：某源挂掉只让对应板块为空（payload 里对应字段为
        None/空列表），整份报告仍然产出，meta.sources 标注口径。
    """
    cached_date = store.latest_date()
    if cached_date and not force:
        cached = store.load(cached_date)
        if cached:
            return cached

    indices, market_flow = await asyncio.gather(
        fetch_index_quotes(), fetch_market_flow())
    frame = await asyncio.to_thread(fetch_sector_frame)
    trade_date, boards = assemble_boards(frame)
    if not trade_date:
        # Tushare 不可用：用大盘资金流的最新日期兜底，行业板块留空
        series = market_flow.get("series") or []
        trade_date = (series[-1]["date"] if series else
                      datetime.now().strftime("%Y-%m-%d"))
        logger.warning("行业轮动：Tushare 板块截面不可用，行业部分为空（%s）", trade_date)

    # 成交额环比：读上一份落盘报告的成交额（自引用，跨日自然衔接）
    prev_turnover = None
    if cached_date and cached_date != trade_date.replace("-", ""):
        prev = store.load(cached_date)
        if prev:
            prev_turnover = (prev.get("market") or {}).get("turnover_yi")

    # 5 日净额：只给当日流入/流出榜的板块取（≤10 个 Tushare 调用）
    candidates = [b for b in boards if b["level"] == 1 and b.get("net_yi") is not None]
    top_in = sorted(candidates, key=lambda b: -(b["net_yi"] or 0))[:5]
    top_out = sorted(candidates, key=lambda b: (b["net_yi"] or 0))[:5]
    flow_5d = await asyncio.to_thread(fetch_flow_5d, top_in + top_out) if boards else {}

    payload = assemble(
        trade_date=trade_date, boards=boards, indices=indices,
        market_flow=market_flow, flow_5d=flow_5d,
        prev_turnover_yi=prev_turnover)

    # 盘中标记：腾讯快照日期新于板块截面日期 → 指数是盘中实时，行业是上一交易日
    tx_dt = str(indices.get("sh000001", {}).get("datetime") or "")
    tx_date = "".join(ch for ch in tx_dt if ch.isdigit())[:8]
    if tx_date and trade_date and tx_date != trade_date.replace("-", ""):
        payload["meta"]["intraday"] = True
        payload["meta"]["index_asof"] = tx_dt

    from src.sector_rotation.report import render_html

    html = render_html(payload)
    store.save(trade_date, payload, html)
    logger.info("行业轮动报告已生成：%s（行业 %d 个，指数 %d 个）",
                trade_date, len(boards), len(payload.get("indices") or []))
    return payload
