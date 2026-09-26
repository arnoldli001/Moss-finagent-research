"""市场环境因子：指数量能 + 板块涨幅排行 + 海外映射。

对应需求新增的三个因子维度：
  1. **指数量能**（index_volume）：个股所属指数（上证/创业板/科创50/深证成指）
     当日成交量的**全天预测量**相比昨日是增量还是缩量，配合指数当日涨跌给出方向分；
  2. **板块涨幅排行**（board_rank）：个股关联板块的盘中涨跌幅 + 该板块在全部
     板块中的涨幅排名分位（同花顺概念快照自带「涨幅排名 10/390」）；
  3. **海外映射**（overseas）：个股映射的美股（英伟达/美光/台积电/日月光/谷歌/
     英特尔/甲骨文/康宁等）昨夜涨跌幅，以及**韩国SK海力士**（与A股同时开市，
     属盘中同步信号而非隔夜）。

指数量能的口径（"预测量能"）：
    全天预测量 = 当前累计量 / 已交易时间占比
    量能比 = 全天预测量 / 昨日全天量
  盘中任何时刻都可比较；收盘后时间占比=1，即为当日实际量。

量价配合打分：score = 方向 × (0.5 + 0.5×量能强度)
  - 放量上涨 → +1（流动性支持做多）
  - 缩量上涨 → 0（无量上涨不可靠，"上涨无量=见顶"）
  - 放量下跌 → -1（放量杀跌）
  - 缩量下跌 → 0（卖压不足，缩量阴跌）
即：量能只**放大或衰减**方向分，不单独给方向 —— 避免"放量"本身被当成利好或利空。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.trading_session import (
    elapsed_session_ratio as _elapsed_session_ratio,
)
from src.intraday.config import OverseasParams
from src.intraday.features import safe_float

logger = logging.getLogger(__name__)

# 指数口径：个股代码前缀 → (指数代码, 指数名)
# 与需求「上证指数、创业板或科创板」一致，另补深证成指覆盖深市主板。
INDEX_BY_BOARD: list[tuple[tuple[str, ...], str, str]] = [
    (("688", "689"), "000688", "科创50"),
    (("300", "301"), "399006", "创业板指"),
    (("000", "001", "002", "003"), "399001", "深证成指"),
    # 场内ETF按上市交易所归属（无名称信息时的最小可靠口径）：
    #   588=科创板ETF→科创50；15/16=深市ETF→深证成指；51/56=沪市ETF→上证指数。
    # 跟踪行业/主题指数的细分关联在估值代理层（akshare_connector）按ETF名称处理。
    (("588",), "000688", "科创50"),
    (("15", "16"), "399001", "深证成指"),
    (("51", "56"), "000001", "上证指数"),
]
#: 板块归属的兜底指数 —— `(代码, 名称)`。
#: ⚠️ 与 `intraday/service.py` 的 `DEFAULT_INDEX_CODE`（纯代码字符串）**不是一回事**：
#: 两者曾同名 `DEFAULT_INDEX`，一个元组一个 str，import 错会在解包处才报错。
DEFAULT_BOARD_INDEX = ("000001", "上证指数")


def board_index_for(code: str) -> tuple[str, str]:
    """证券代码 → 所属指数（个股按板块、ETF按上市交易所/科创板归属）。"""
    for prefixes, index_code, name in INDEX_BY_BOARD:
        if code.startswith(prefixes):
            return index_code, name
    return DEFAULT_BOARD_INDEX


def elapsed_session_ratio(now: datetime | None = None) -> float:
    """当前时点「已交易时间」占全天比例（0~1），用于折算全天预测量。

    非交易日/盘前返回 0（调用方据此跳过预测），收盘后返回 1.0。
    时段边界来自 `core.trading_session`，与盘中状态判定共用同一套常量。
    """
    return _elapsed_session_ratio(now)


@dataclass
class IndexVolume:
    """指数量能快照。"""

    code: str
    name: str
    available: bool = False
    change_pct: float | None = None
    volume: float | None = None          # 当前累计成交量（手）
    amount: float | None = None          # 当前累计成交额（元，若有）
    yesterday_volume: float | None = None
    projected_volume: float | None = None
    volume_ratio: float | None = None    # 全天预测量 / 昨日全天量
    elapsed_ratio: float = 0.0
    verdict: str = ""
    source_name: str = ""
    gap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "available": self.available,
            "change_pct": self.change_pct,
            "volume": self.volume, "amount": self.amount,
            "yesterday_volume": self.yesterday_volume,
            "projected_volume": self.projected_volume,
            "volume_ratio": self.volume_ratio,
            "elapsed_ratio": round(self.elapsed_ratio, 4),
            "verdict": self.verdict, "source_name": self.source_name,
            "gap": self.gap,
        }


@dataclass
class OverseasQuote:
    """单条海外映射行情。"""

    symbol: str          # 腾讯代码（usNVDA / kr000660）
    name: str
    market: str          # us=隔夜映射 / kr=盘中同步
    change_pct: float | None = None
    price: float | None = None
    prev_close: float | None = None
    quote_time: str = ""
    available: bool = False
    gap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name, "market": self.market,
            "change_pct": self.change_pct, "price": self.price,
            "prev_close": self.prev_close, "quote_time": self.quote_time,
            "available": self.available, "gap": self.gap,
        }


@dataclass
class OverseasSnapshot:
    """海外映射汇总。"""

    available: bool = False
    overnight_score: float | None = None   # 美股隔夜方向分
    intraday_score: float | None = None    # 韩股同步方向分
    score: float | None = None             # 合成方向分 ∈[-1,1]
    overnight_avg_pct: float | None = None
    intraday_avg_pct: float | None = None
    quotes: list[OverseasQuote] | None = None
    verdict: str = ""
    gaps: list[str] | None = None


def score_index_volume(
    *, change_pct: float | None, volume_ratio: float | None,
    scale_pct: float = 1.0, volume_scale: float = 0.5,
) -> tuple[float | None, str]:
    """指数量价配合打分：score = 方向 × (0.5 + 0.5×量能强度)。

    方向 = clip(指数涨跌幅 / scale_pct)（默认 ±1% 满分）
    量能强度 = clip((量能比 - 1) / volume_scale)（默认 ±50% 满分）
    """
    if change_pct is None:
        return None, ""
    direction = max(-1.0, min(1.0, change_pct / scale_pct if scale_pct else 0.0))
    if volume_ratio is None:
        return direction * 0.75, "量能数据缺失，仅按指数方向给分（×0.75）"
    strength = max(-1.0, min(1.0,
                             (volume_ratio - 1.0) / volume_scale if volume_scale else 0.0))
    score = direction * (0.5 + 0.5 * strength)
    if direction > 0 and strength > 0:
        note = "指数放量上涨，流动性支持做多"
    elif direction > 0 and strength < 0:
        note = "指数缩量上涨，无量上涨不可靠（上涨无量=见顶）"
    elif direction < 0 and strength > 0:
        note = "指数放量下跌，放量杀跌需谨慎"
    elif direction < 0 and strength < 0:
        note = "指数缩量下跌，卖压不足但方向仍偏弱"
    else:
        note = "指数与量能均中性"
    return max(-1.0, min(1.0, score)), note


def score_overseas(
    quotes: list[OverseasQuote], params: OverseasParams,
) -> OverseasSnapshot:
    """海外映射打分：美股隔夜分 × overnight_weight + 韩股同步分 × intraday_weight。"""
    gaps: list[str] = []
    usable = [q for q in quotes if q.available and q.change_pct is not None]
    for quote in quotes:
        if not quote.available:
            gaps.append(f"{quote.name}({quote.symbol}) 行情不可用"
                        + (f"：{quote.gap}" if quote.gap else ""))
    if not usable:
        return OverseasSnapshot(
            available=False, quotes=quotes,
            gaps=gaps or ["无可用海外映射行情"],
            verdict="海外映射行情不可用，本维度未计入总分")

    def weighted(market: str) -> tuple[float | None, float | None]:
        group = [q for q in usable if q.market == market]
        if not group:
            return None, None
        total = sum(_weight_for(q.symbol, params) for q in group) or 1.0
        average = sum(q.change_pct * _weight_for(q.symbol, params)  # type: ignore[operator]
                      for q in group) / total
        raw = average / params.scale_pct if params.scale_pct else 0.0
        return max(-1.0, min(1.0, raw)), round(average, 3)

    overnight_score, overnight_avg = weighted("us")
    intraday_score, intraday_avg = weighted("kr")
    components: list[tuple[float, float]] = []
    if overnight_score is not None:
        components.append((params.overnight_weight, overnight_score))
    if intraday_score is not None:
        components.append((params.intraday_weight, intraday_score))
    if not components:
        return OverseasSnapshot(
            available=False, quotes=quotes, gaps=gaps,
            verdict="海外映射行情不可用，本维度未计入总分")
    weight_sum = sum(w for w, _ in components) or 1.0
    score = sum(w * v for w, v in components) / weight_sum
    score = max(-1.0, min(1.0, score))

    notes: list[str] = []
    if overnight_avg is not None:
        notes.append(f"美股映射隔夜均涨跌 {overnight_avg:+.2f}%"
                     f"（{len([q for q in usable if q.market == 'us'])}只）")
    if intraday_avg is not None:
        names = "、".join(q.name for q in usable if q.market == "kr")
        notes.append(f"{names}盘中 {intraday_avg:+.2f}%（与A股同时开市，同步信号）")
    tone = "偏多" if score > 0.15 else ("偏空" if score < -0.15 else "中性")
    return OverseasSnapshot(
        available=True, overnight_score=overnight_score,
        intraday_score=intraday_score, score=score,
        overnight_avg_pct=overnight_avg, intraday_avg_pct=intraday_avg,
        quotes=quotes, gaps=gaps,
        verdict="；".join(notes) + f" → 海外映射{tone}")


def _weight_for(symbol: str, params: OverseasParams) -> float:
    """映射权重（按代码查配置；未配置则用默认权重）。"""
    return params.weights.get(symbol, params.default_weight)


def score_board_rank(
    *, rank: str | None, change_pct: float | None,
    rank_weight: float = 0.6, change_weight: float = 0.4,
    change_scale_pct: float = 2.0,
) -> tuple[float | None, dict[str, Any], str]:
    """板块涨幅排行打分：排名分位（主）+ 板块涨跌幅（次）。

    rank 形如 "10/390"（同花顺概念快照的「涨幅排名」）→ 分位 = 1-(名次-1)/(总数-1)。
    """
    inputs: dict[str, Any] = {}
    parts: list[tuple[float, float]] = []
    rank_score = None
    if rank:
        parsed = _parse_rank(rank)
        if parsed is not None:
            position, total = parsed
            percentile = 1.0 - (position - 1) / max(1, total - 1)
            rank_score = max(-1.0, min(1.0, (percentile - 0.5) * 2.0))
            inputs["rank"] = rank
            inputs["rank_percentile"] = round(percentile, 4)
            parts.append((rank_weight, rank_score))
    if change_pct is not None:
        change_score = max(-1.0, min(1.0,
                                     change_pct / change_scale_pct
                                     if change_scale_pct else 0.0))
        inputs["board_change_pct"] = change_pct
        parts.append((change_weight, change_score))
    if not parts:
        return None, inputs, "板块排名与涨跌幅均不可得"
    weight_sum = sum(w for w, _ in parts) or 1.0
    score = sum(w * v for w, v in parts) / weight_sum
    score = max(-1.0, min(1.0, score))
    if rank_score is None:
        note = "仅板块涨跌幅可用（排名缺失）"
    elif rank_score > 0.5:
        note = "板块涨幅排名居前，属当日强势板块"
    elif rank_score < -0.5:
        note = "板块涨幅排名靠后，属当日弱势板块"
    else:
        note = "板块涨幅排名居中"
    return score, inputs, note


def _parse_rank(rank: str) -> tuple[int, int] | None:
    """'10/390' → (10, 390)。"""
    if not rank or "/" not in rank:
        return None
    left, _, right = rank.partition("/")
    position = safe_float(left.strip())
    total = safe_float(right.strip())
    if position is None or total is None or total <= 1 or position < 1:
        return None
    return int(position), int(total)


def parse_tencent_overseas_line(line: str, market: str) -> OverseasQuote | None:
    """解析腾讯美股/韩股快照一行（字段布局与A股不同）。

    布局：0 市场码 | 1 名称 | 2 代码 | 3 最新价 | 4 昨收 | 5 开盘 | 6 成交量
          | … | 30 行情时间 | 31 涨跌 | 32 涨跌幅% | 33 最高 | 34 最低
    """
    if '"' not in line:
        return None
    symbol = line.split("=")[0].replace("v_", "").strip()
    parts = line.split('"')[1].split("~")
    if len(parts) < 35:
        return None
    price = safe_float(parts[3])
    prev_close = safe_float(parts[4])
    change_pct = safe_float(parts[32])
    if change_pct is None and price is not None and prev_close:
        change_pct = (price / prev_close - 1.0) * 100.0
    return OverseasQuote(
        symbol=symbol, name=str(parts[1]).strip(), market=market,
        change_pct=change_pct, price=price, prev_close=prev_close,
        quote_time=str(parts[30]).strip(), available=price is not None)
