"""ETF 份额监控：指标计算与信号生成。

## 这个模块回答什么问题

核心宽基 ETF 的份额变化主要反映**国家队与大型基金的逆周期操作**：
市场跌到低位时它们大额申购（份额上升），涨到高位时赎回。
因此「指数在低位 + 宽基 ETF 份额大幅增加」是一个有意义的**底部辅助信号**，
反过来则是顶部辅助信号。

## 三个必须写清楚的口径

**1. 份额是存量，不是流量。** `fund_share.fd_share` 是"截至该日的基金份额"，
所以"净申购"必须用**相邻两日之差**，不能与同日其它字段混算。
5/10/20 日累计变化同理，取的是 `share_t / share_{t-N} - 1`。

**2. 份额数据次日 8:30 才更新。** 盘后拉到的是**上一交易日**的数据，
信号本质是 T+1 确认 —— 适合阶段顶底判断，**不适合日内做 T**。
这一点不写清楚，使用者会拿它当盘中信号用。

**3. 宽基与行业 ETF 的信噪比完全不同。**
宽基份额变化是机构逆周期操作；行业 ETF 的份额变化容易被散户追涨杀跌主导，
资金大量流入后**短期反转效应显著**。所以：

    宽基（level=core/assist）  → 出机会信号 / 风险信号
    行业（level=industry）     → **只出反转警示**，不出机会信号

把两者用同一套规则处理，是这个模块最容易犯的错。

**4. 指数位置用「分位排名」而不是「(close-min)/(max-min)」。**
后者的分子分母都由窗口内的两个极值决定 —— 一个异常插针就能把整段
分位数压扁或拉满。分位排名（`percentileofscore` 口径）对极值稳健得多。
`thresholds.percentile_mode` 可切换。

## 5. 机会信号必须过「市场环境」这一道门

这是回测给出的最重要的一条，也是本模块唯一一处**主动收窄**信号的地方。

把 2018-2026 的机会信号按市场环境拆开后会看到一个刺眼的分化 ——
同样是"指数低位 + 宽基 ETF 大额申购"：

    熊市（ 77 个）T+34 中位数 +8.78%   胜率 76.6%
    牛市（ 60 个）T+34 中位数 -1.53%   胜率 36.7%
    震荡（445 个）T+34 中位数 -0.70%   胜率 46.5%

符号完全相反。混在一起统计时正负相抵，582 个样本整体只剩 -0.04%、胜率 49.5%，
看起来像个噪声 —— 那是把三种环境平均掉的假象，不是信号本身的性质。

门控之后"会真正告警"的 66 个信号：T+34 中位数 **+9.18%**、胜率 **83.3%**，
且 **T+1 口径几乎不变（+8.90%、83.3%）** —— 收益不来自信号日当天已经涨完的
部分，实盘拿得到。被门控拦下的 223 个则是 +0.52%、52.9%（近乎抛硬币），
拦掉它们不损失任何东西。

机制解释是清楚的：宽基 ETF 的大额逆势申购主要来自国家队托底，
最激烈的时点就是熊市底部；牛市里的份额增长则更多是散户跟风申购，
与行业 ETF 的追涨行为同源，因此**不具备领先性**。

所以 `regime.opportunity_allowed` 决定哪些环境放行机会信号，其余环境
**降级为弱信号进观察列表**（`gated=True`），而不是直接丢弃 —— 直接丢弃
会让使用者以为"今天没信号"，而降级能同时表达"有异动"和"但环境不支持"。

风险信号**也做门控，但方向相反**：`regime.risk_allowed` 默认排除熊市。
这一条最初我判断错了 —— 当时以为"高位赎回 → 后市走弱"在各环境方向都成立，
但 `kind × regime` 交叉表显示熊市里的风险信号 T+34 中位数是 **+4.69%**
（方向反了）。机制上说得通：熊市里的下跌会被国家队买回去，所以熊市里读到
"高位 + 赎回"更像是一次反弹的开始，而不是顶部。

⚠️ 这个例外**只有 13 个样本**，而且其中只有 **1 个**真正走到了告警等级 ——
也就是说这道风险门控在 2018-2026 全程只拦下 1 个信号，**近乎无作用**。
保留它是为了让"熊市例外"这件事显式写在配置里，而不是因为它有统计价值。
不要把它当作已验证的规则。## 与「主线挖掘」的联动默认关闭

需求写的是"信号触发时把宽基相关板块加权 +5~10 分"，但它自己在注意事项
8.6 里也写了"加权幅度应通过历史回测确定最优值"。因此 `linkage.enabled`
默认为 `false` —— 未经回测的联动会把两套独立信号混成一个无法归因的分数，
这正是 V2.0 一直在防的那类问题。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.mainline.config import PROJECT_ROOT, load_yaml_config

logger = logging.getLogger(__name__)

CONFIG_FILE = "etf_flow.yaml"

#: 信号类型
KIND_OPPORTUNITY = "opportunity"
KIND_RISK = "risk"
KIND_INDUSTRY_REVERSAL = "industry_reversal"

KIND_LABELS = {
    KIND_OPPORTUNITY: "🟢 机会信号",
    KIND_RISK: "🔴 风险信号",
    KIND_INDUSTRY_REVERSAL: "🟡 行业反转警示",
}

#: 等级数值（与 `models.SignalLevel` 对齐但本模块独立，避免耦合）
LEVEL_STRONG, LEVEL_MEDIUM, LEVEL_WEAK, LEVEL_NONE = (
    "strong", "medium", "weak", "none")
LEVEL_LABELS = {"strong": "🔴 强信号", "medium": "🟡 中信号",
                "weak": "🟢 弱信号", "none": "未触发"}


@dataclass
class EtfSpec:
    """观测清单里的一只 ETF。"""

    code: str
    name: str = ""
    group: str = ""
    index: str = ""
    index_name: str = ""
    level: str = "assist"        # core / assist / industry


@dataclass
class WatchGroup:
    """同系列的一组 ETF（共振验证的单位）。"""

    key: str
    label: str = ""
    index: str = ""
    index_name: str = ""
    level: str = "assist"
    etfs: list[EtfSpec] = field(default_factory=list)


@dataclass
class FlowConfig:
    """`configs/etf_flow.yaml` 的解析结果。"""

    groups: list[WatchGroup] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    backtest: dict[str, Any] = field(default_factory=dict)
    linkage: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    loaded: bool = False
    gap: str = ""

    @property
    def all_etfs(self) -> list[EtfSpec]:
        return [item for group in self.groups for item in group.etfs]

    def group_of(self, code: str) -> WatchGroup | None:
        for group in self.groups:
            if any(item.code == code for item in group.etfs):
                return group
        return None

    def threshold(self, path: str, default: Any = None) -> Any:
        """按 `"opportunity.max_percentile"` 这种点号路径取阈值。"""
        node: Any = self.thresholds
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(path: str | Path | None = None) -> FlowConfig:
    """加载配置；文件缺失/损坏时返回空配置 + gap（不抛错）。"""
    raw = load_yaml_config(str(path) if path else CONFIG_FILE)
    if raw is None:
        return FlowConfig(loaded=False,
                          gap=f"ETF 份额监控配置不可用：configs/{CONFIG_FILE}")
    out = FlowConfig(loaded=True)
    out.thresholds = dict(raw.get("thresholds") or {})
    out.backtest = dict(raw.get("backtest") or {})
    out.linkage = dict(raw.get("linkage") or {})
    out.data = dict(raw.get("data") or {})
    for item in raw.get("watchlist") or []:
        if not isinstance(item, dict):
            continue
        group = WatchGroup(key=str(item.get("group") or ""),
                           label=str(item.get("label") or ""),
                           index=str(item.get("index") or ""),
                           index_name=str(item.get("index_name") or ""),
                           level=str(item.get("level") or "assist"))
        for etf in item.get("etfs") or []:
            if not isinstance(etf, dict):
                continue
            code = str(etf.get("code") or "").strip()
            if not code:
                continue
            group.etfs.append(EtfSpec(
                code=code, name=str(etf.get("name") or ""),
                group=group.key, index=group.index,
                index_name=group.index_name, level=group.level))
        if group.etfs:
            out.groups.append(group)
    if not out.groups:
        out.gap = "观测清单为空（watchlist 段）"
    return out


# ==================================================================
# 指标
# ==================================================================


@dataclass
class EtfIndicator:
    """一只 ETF 在某个交易日的份额指标。"""

    code: str
    name: str = ""
    group: str = ""
    level: str = "assist"
    trade_date: str = ""
    shares: float | None = None
    #: `shares` 实际取自哪个交易日。与 `trade_date` 不同时说明当天份额还没发布，
    #: 界面必须显示这个 as-of 日期（否则用户会把昨天的份额当成今天的）。
    shares_date: str = ""
    close: float | None = None
    #: 份额变化率（小数）
    change_1d: float | None = None
    change_5d: float | None = None
    change_10d: float | None = None
    change_20d: float | None = None
    #: 当日成交额（元）与放量倍数（辅助展示）
    amount: float | None = None
    amount_ratio: float | None = None
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "group": self.group,
                "level": self.level, "trade_date": self.trade_date,
                "shares": self.shares, "shares_date": self.shares_date,
                "close": self.close,
                "change_1d": self.change_1d, "change_5d": self.change_5d,
                "change_10d": self.change_10d, "change_20d": self.change_20d,
                "amount": self.amount, "amount_ratio": self.amount_ratio,
                "gaps": list(self.gaps)}


@dataclass
class IndexPosition:
    """一个宽基指数的位置指标。"""

    code: str
    name: str = ""
    trade_date: str = ""
    close: float | None = None
    #: 0-1 的分位（越大越接近窗口高位）
    percentile: float | None = None
    mode: str = "rank"
    window: int = 34
    high: float | None = None
    low: float | None = None
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name,
                "trade_date": self.trade_date, "close": self.close,
                "percentile": (round(self.percentile, 4)
                               if self.percentile is not None else None),
                "mode": self.mode, "window": self.window,
                "high": self.high, "low": self.low, "gaps": list(self.gaps)}


def _pct_change(current: Any, previous: Any) -> float | None:
    """份额变化率（小数）；基准缺失或为 0 时返回 None。"""
    now = _f(current)
    base = _f(previous)
    if now is None or base is None or base <= 0:
        return None
    return now / base - 1.0


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


#: 判定"份额拆分/合并"的阈值（见 `adjust_share_splits`）。
#:
#: 为什么必须是**双向**条件而不是"份额跳变超过 N%"：拆分与"真的有大额申赎"
#: 在份额这一个数字上完全同形 —— 只看份额变化会把 2026-03-30 那次 1 拆 2
#: （份额 +96%）当成历史最大的一次申购。价格是唯一能区分两者的信息。
SPLIT_SHARE_MIN = 0.15      # 份额跳变幅度下限（15%）
SPLIT_PRICE_MIN = 0.10      # 价格反向跳变幅度下限（10%）
#: 市值比允许区间：拆分是**机械换股**，份额×价格必然基本不变。
#: 留 ±15% 是为了容忍拆分当天的真实涨跌（那天市场该涨还是会涨）。
SPLIT_MV_LOW, SPLIT_MV_HIGH = 0.85, 1.15


def adjust_share_splits(rows: Sequence[dict[str, Any]]
                        ) -> tuple[list[dict[str, Any]], list[str]]:
    """把份额序列里的**拆分/合并**还原成"可比口径"，返回 `(行, 拆分日列表)`。

    ## 为什么必须做这件事（2026-09-22 实测）

    份额是**存量**，变化率靠相邻两日相除。但 ETF 会做**份额拆分/合并**：
    单位净值腰斩、份额翻倍，总市值一分不变。系统没有这种概念，于是把它读成
    一次巨额申赎。实测观测清单 9 只 ETF 在 2018-2026 共发生 3 次：

        512100.SH  2022-09-05  份额 -64% / 价格 +176%  → 被读成巨额赎回
        159516.SZ  2026-03-30  份额 +96% / 价格 -49%   → 被读成巨额申购
        512170.SH  2021-02-25  份额 +193% / 价格 -68%  → 被读成巨额申购

    后果不是"少赚一点"，而是**凭空造出信号**：159516 那次直接连着触发了
    2026-03-30 / 04-01 / 04-02 / 04-03 四条"行业反转警示"（5 日累计 +92% ~ +99%），
    而用户看到的是"三月跌了一波之后的低位买入信号"—— 完全是被机械换股骗出来的。
    而且污染不会只影响当天：5 日窗口会带着这个跳变**连续 5 个交易日**都超标，
    这正是那四条连续误报的机制。

    ## 判据

    三个条件同时成立才算拆分（缺一个都可能把真实申赎误判）：
    1. 份额跳变 ≥ `SPLIT_SHARE_MIN`；
    2. 价格**反向**跳变 ≥ `SPLIT_PRICE_MIN`（拆分必然伴随单位净值反向变化）；
    3. `份额比 × 价格比 ≈ 1`（市值不变）—— 这一条排除"真实申购 + 恰好当天大跌"。

    ## 还原方式（先把不变式写清楚，别靠试）

    份额比 `s = 拆分后份额 / 拆分前份额`。可流通份额变了 `s` 倍，所以
    **拆分后 1 份 = 拆分前 1/s 份**（1 拆 2 时 s≈2：新的 1 份 = 旧的半份）。

    两个要求：
    1. **份额列必须显示原始存量**（当前份额 2,357,118 万份要和你券商里看到的一致）；
    2. **变化率必须跨拆分连续**（不能把机械翻倍算成 +96% 的申购）。

    要求 1 意味着"当前行"不能动，于是**历史行要折算到新基准**：

        新基准下的历史份额 = 原始份额 × ∏(该行之后每个拆分日的 s)

    以 159516 的 1 拆 2 为例：3/27 的 1,200,859 × 1.9629 = 2,357,197，
    而拆分日的真实存量是 2,357,118 —— 差 0.003%，正是当天的真实申赎，
    于是拆分日的日环比 ≈ 0。合并（s<1）同理：100 × 0.3555 = 35.55 = 合并日的存量。

    ⚠️ 这里连踩两次坑，所以把结论直接写死：**是乘、不是除；改的是拆分日之前
    的行、不是之后的行**。而且检测必须用**原始**份额（边改边测会把同一位置
    重复识别成拆分）。两个错误都不抛异常，只让变化率从 0 变成 +285%。
    `TestAdjustShareSplits` 把这两个方向都钉住了。

    还原后：
    * 拆分日当天的日环比 ≈ 0（机械换股不再算成申赎）；
    * 拆分日**之前**的 5/10/20 日窗口不再混着两种口径，污染窗口自然消失。

    ⚠️ 只动内存里这一份序列，**不写回 `ml_etf`** —— 库里的份额是原始托管数据，
    改它会让"当前份额"这个绝对量变得不可对账。
    """
    out: list[dict[str, Any]] = [dict(row) for row in rows]
    #: 逐行的折算因子（检测一律用 `rows` 的原始份额，见上方警告）
    factors: list[float] = [1.0] * len(rows)
    splits: list[str] = []
    for index in range(1, len(rows)):
        share0 = _f(rows[index - 1].get("shares"))
        share1 = _f(rows[index].get("shares"))
        close0 = _f(rows[index - 1].get("close"))
        close1 = _f(rows[index].get("close"))
        if not (share0 and share1 and close0 and close1):
            continue
        if share0 <= 0 or close0 <= 0:
            continue
        share_ratio = share1 / share0
        price_ratio = close1 / close0
        if abs(share_ratio - 1.0) < SPLIT_SHARE_MIN:
            continue
        if abs(price_ratio - 1.0) < SPLIT_PRICE_MIN:
            continue
        if (share_ratio - 1.0) * (price_ratio - 1.0) >= 0:
            continue                      # 同向变动 → 不是拆分
        market_value = share_ratio * price_ratio
        if not (SPLIT_MV_LOW < market_value < SPLIT_MV_HIGH):
            continue                      # 市值明显变了 → 是真实申赎，别动它
        # 这次拆分之前的历史，都要乘上 s 才能折算到新基准（乘，不是除）
        for earlier in range(index):
            factors[earlier] *= share_ratio
        splits.append(str(rows[index].get("trade_date") or ""))
    if not splits:
        return out, splits
    for row, factor in zip(out, factors, strict=True):
        value = _f(row.get("shares"))
        if value is not None and factor != 1.0:
            row["shares"] = value * factor
    return out, splits


def index_percentile(closes: Sequence[Any], *, window: int,
                     mode: str = "rank") -> float | None:
    """指数当前收盘在近 `window` 日中的位置（0-1）。

    `rank`：分位排名 = 小于等于当日的天数 / 窗口长度。**默认**，对异常极值稳健。
    `range`：(close - min) / (max - min)。分子分母都由两个极值决定，
             一根插针就能把整段分位压扁，只在明确想要"区间位置"时使用。
    """
    values = [item for item in (_f(v) for v in closes) if item is not None]
    if len(values) < max(int(window), 5):
        return None
    tail = values[-int(window):]
    current = tail[-1]
    if mode == "range":
        low, high = min(tail), max(tail)
        if high - low <= 1e-12:
            return None
        return max(0.0, min(1.0, (current - low) / (high - low)))
    below = sum(1 for item in tail if item <= current)
    return max(0.0, min(1.0, below / len(tail)))


# ==================================================================
# 市场环境
# ==================================================================

#: 市场环境分类（`regime` 段的默认阈值，可被配置覆盖）
REGIME_BULL, REGIME_BEAR, REGIME_RANGE = "bull", "bear", "range"
REGIME_LABELS = {REGIME_BULL: "牛市", REGIME_BEAR: "熊市",
                 REGIME_RANGE: "震荡市"}
DEFAULT_REGIME_WINDOW = 120
DEFAULT_REGIME_BULL = 0.15
DEFAULT_REGIME_BEAR = -0.15


@dataclass
class MarketRegime:
    """市场环境判定结果。"""

    key: str = REGIME_RANGE
    label: str = REGIME_LABELS[REGIME_RANGE]
    index_code: str = ""
    index_name: str = ""
    trade_date: str = ""
    window: int = DEFAULT_REGIME_WINDOW
    #: 参考指数近 `window` 日收益（小数）；历史不足时为 None
    change: float | None = None
    close: float | None = None
    #: 机会信号在本环境下是否放行
    opportunity_allowed: bool = True
    #: 风险信号在本环境下是否放行
    risk_allowed: bool = True
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label,
                "index_code": self.index_code, "index_name": self.index_name,
                "trade_date": self.trade_date, "window": self.window,
                "change": (round(self.change, 4)
                           if self.change is not None else None),
                "close": self.close,
                "opportunity_allowed": self.opportunity_allowed,
                "risk_allowed": self.risk_allowed,
                "gaps": list(self.gaps)}


def classify_regime(closes: Sequence[Any], *, window: int = DEFAULT_REGIME_WINDOW,
                    bull: float = DEFAULT_REGIME_BULL,
                    bear: float = DEFAULT_REGIME_BEAR) -> tuple[str, float | None]:
    """由指数收盘价序列（升序）判定环境，返回 `(key, 近 window 日收益)`。

    用**相对收益**而不是绝对点位：同样是 3500 点，从 3000 涨上来和从 4000
    跌下来是完全不同的环境。窗口取 120 日 ≈ 半年，比 34 日的分位窗口长得多 ——
    分位窗口刻画"当下位置"，环境窗口刻画"过去半年的方向"，两者互补。
    """
    values = [_f(v) for v in closes]
    clean = [v for v in values if v is not None]
    if len(clean) <= int(window) or not clean[-1 - int(window)]:
        return REGIME_RANGE, None
    change = clean[-1] / clean[-1 - int(window)] - 1.0
    if change > float(bull):
        return REGIME_BULL, change
    if change < float(bear):
        return REGIME_BEAR, change
    return REGIME_RANGE, change


def build_regime(index_bars: dict[str, list[tuple[str, float]]], *,
                 config: FlowConfig, trade_date: str = "") -> MarketRegime:
    """算当前市场环境。

    参考指数的选择顺序：`regime.reference_index` 配置 > `level: core` 的组
    对应指数 > 任意有数据的组。宽基系列（沪深300 等）才是市场环境的合适
    代理，用行业 ETF 的指数判环境会把"行业自己的行情"误当成大盘环境。
    """
    window = int(config.threshold("regime.window", DEFAULT_REGIME_WINDOW)
                 or DEFAULT_REGIME_WINDOW)
    bull = float(config.threshold("regime.bull", DEFAULT_REGIME_BULL)
                 or DEFAULT_REGIME_BULL)
    bear = float(config.threshold("regime.bear", DEFAULT_REGIME_BEAR)
                 or DEFAULT_REGIME_BEAR)
    out = MarketRegime(window=window, trade_date=trade_date)
    code = str(config.threshold("regime.reference_index", "") or "").strip()
    if not code:
        for group in config.groups:
            if group.level == "core" and group.index:
                code = group.index
                break
    if not code:
        for group in config.groups:
            if group.index and index_bars.get(group.index):
                code = group.index
                break
    if not code:
        out.gaps.append("观测清单里没有任何可用的参考指数")
        return out

    names = {group.index: (group.index_name or group.index)
             for group in config.groups if group.index}
    out.index_code = code
    out.index_name = names.get(code, code)
    rows = index_bars.get(code) or []
    if not rows:
        out.gaps.append(f"本地没有参考指数 {code} 的日线数据，环境判定按震荡市处理")
        apply_regime_policy(out, config)
        return out
    out.trade_date = rows[-1][0] or trade_date
    out.close = _f(rows[-1][1])
    out.key, out.change = classify_regime([value for _, value in rows],
                                          window=window, bull=bull, bear=bear)
    out.label = REGIME_LABELS.get(out.key, out.key)
    if out.change is None:
        out.gaps.append(f"参考指数历史不足 {window} 个交易日，环境判定按震荡市处理")
    apply_regime_policy(out, config)
    return out


def apply_regime_policy(regime: MarketRegime, config: FlowConfig) -> None:
    """按配置决定机会/风险信号是否放行（就地修改）。

    机会信号默认**只在熊市放行**（回测：熊市 T+34 +8.78%/76.6%，
    牛市 -1.53%/36.7%，震荡 -0.70%/46.5%）。
    风险信号默认**排除熊市**（熊市 +4.69% 方向相反，但仅 13 个样本）。
    """
    regime.opportunity_allowed = regime.key in _allowed_regimes(
        config, "opportunity_allowed", default=[REGIME_BEAR])
    regime.risk_allowed = regime.key in _allowed_regimes(
        config, "risk_allowed",
        default=[REGIME_BULL, REGIME_RANGE])


def _allowed_regimes(config: FlowConfig, key: str, *,
                     default: list[str]) -> set[str]:
    """读 `regime.<key>` 白名单；配置缺失/类型异常时回退 `default`。

    与 `_is_gated` 共用同一函数，避免"配置读法"和"判定读法"两套口径
    不一致 —— 那种偏差在实盘里表现为门控时灵时不灵，极难排查。
    """
    allowed = config.threshold(f"regime.{key}")
    if allowed is None:
        return {item.lower() for item in default}
    if isinstance(allowed, str):
        allowed = [allowed]
    try:
        return {str(item).strip().lower() for item in allowed}
    except TypeError:
        return {item.lower() for item in default}


def build_indicators(bars: dict[str, list[dict[str, Any]]], config: FlowConfig,
                     *, trade_date: str = "") -> dict[str, EtfIndicator]:
    """由 `{etf_code: [{trade_date, shares, close, amount}, ...]}`（升序）算指标。

    份额窗口不足时对应字段为 None（**不补 0**）：0 表示"份额没变"，
    与"不知道"完全是两回事，而机会信号要求"份额显著增加"，
    把缺失当 0 会让信号静默消失。

    ⚠️ 算变化率之前先过一遍 `adjust_share_splits`：ETF 的份额拆分/合并会让
    存量机械翻倍或腰斩，不过滤就会**凭空造出巨额申赎信号**（实测 3 次，
    其中 159516 在 2026-03-30 的 1 拆 2 直接产生了四条"行业反转"误报）。
    `shares` 字段仍按**原始**存量展示，只有变化率用还原后的口径。
    """
    out: dict[str, EtfIndicator] = {}
    for spec in config.all_etfs:
        rows = bars.get(spec.code) or []
        item = EtfIndicator(code=spec.code, name=spec.name or spec.code,
                            group=spec.group, level=spec.level,
                            trade_date=trade_date)
        if not rows:
            item.gaps.append("本地没有该 ETF 的份额/行情数据")
            out[spec.code] = item
            continue
        adjusted, splits = adjust_share_splits(rows)
        last = adjusted[-1]
        item.trade_date = str(last.get("trade_date") or trade_date)
        item.shares = _f(last.get("shares"))
        item.shares_date = item.trade_date
        item.close = _f(last.get("close"))
        item.amount = _f(last.get("amount"))
        # 份额列缺失（= 当天还没发布）时**回退到最近一个有份额的交易日**。
        #
        # ⚠️ 为什么必须回退（2026-09-22 实测）：份额是**次日 8:30** 才发布的，
        # 而日线当晚就有 —— 于是每天 15:00 之后到次日 8:30 之间，最新交易日
        # 的份额列必然为空。此时若直接让 shares=None，那 7 只的份额、1/5/10/20 日
        # 变化率、指数分位会**一起变空**，面板看起来就是"坏了"（这正是本会话
        # 开头那个 bug 的形状，只不过当时是残留数据、现在是每日必然发生的窗口）。
        # 回退到上一交易日的份额并**如实标注 as-of 日期**：数据是真实的（只是旧
        # 一天），比显示一片空白有用得多，而"哪一天的口径"由界面写清楚。
        base = next(((index, row) for index, row in reversed(list(enumerate(adjusted)))
                     if _f(row.get("shares")) is not None), None)
        if base is None:
            item.gaps.append("份额列缺失（fund_share 未同步或该 ETF 无份额数据）")
            out[spec.code] = item
            continue
        base_index, base_row = base
        if base_index != len(adjusted) - 1:
            item.shares = _f(base_row.get("shares"))
            item.shares_date = str(base_row.get("trade_date") or "")
            item.gaps.append(
                f"份额为 {item.shares_date} 口径（当日份额次日 8:30 才发布，"
                "尚未入库）")
        for window, attr in ((1, "change_1d"), (5, "change_5d"),
                             (10, "change_10d"), (20, "change_20d")):
            if base_index >= window:
                setattr(item, attr,
                        _pct_change(item.shares,
                                    adjusted[base_index - window].get("shares")))
        if splits:
            # 如实标注：这些日的份额变化率已按拆分还原，"份额"列本身不还原。
            item.gaps.append(
                "份额拆分/合并已还原（"
                + "、".join(splits[-3:]) + "）；份额列为原始存量")
        history = [_f(row.get("amount")) for row in adjusted[-61:-1]]
        clean = [value for value in history if value]
        if clean and item.amount:
            typical = sorted(clean)[len(clean) // 2]
            if typical > 0:
                item.amount_ratio = item.amount / typical
        out[spec.code] = item
    return out


def build_positions(index_bars: dict[str, list[tuple[str, float]]], *,
                    config: FlowConfig, trade_date: str = ""
                    ) -> dict[str, IndexPosition]:
    """由 `{index_code: [(date, close), ...]}`（升序）算宽基指数位置分位。"""
    window = int(config.threshold("percentile_window", 34) or 34)
    mode = str(config.threshold("percentile_mode", "rank") or "rank")
    out: dict[str, IndexPosition] = {}
    names: dict[str, str] = {}
    for group in config.groups:
        if group.index:
            names.setdefault(group.index, group.index_name or group.index)
    for code, rows in index_bars.items():
        item = IndexPosition(code=code, name=names.get(code, code),
                             trade_date=trade_date, mode=mode, window=window)
        closes = [value for _, value in rows]
        if not closes:
            item.gaps.append("本地没有该指数的日线数据")
            out[code] = item
            continue
        item.close = closes[-1]
        if rows:
            item.trade_date = rows[-1][0] or trade_date
        tail = closes[-window:]
        item.high = max(tail) if tail else None
        item.low = min(tail) if tail else None
        item.percentile = index_percentile(closes, window=window, mode=mode)
        if item.percentile is None:
            item.gaps.append(f"指数历史不足 {window} 个交易日，无法算分位")
        out[code] = item
    return out


# ==================================================================
# 信号
# ==================================================================


@dataclass
class FlowSignal:
    """一条 ETF 份额信号。"""

    date: str
    code: str
    name: str = ""
    group: str = ""
    group_label: str = ""
    kind: str = KIND_OPPORTUNITY
    level: str = LEVEL_NONE
    index_code: str = ""
    index_name: str = ""
    index_percentile: float | None = None
    change_1d: float | None = None
    change_5d: float | None = None
    #: 同系列同向的 ETF 数量 / 该系列总数
    resonance_count: int = 0
    resonance_total: int = 0
    resonance: bool = False
    #: 市场环境（`MarketRegime.key`）
    regime: str = REGIME_RANGE
    #: 是否因环境不支持而被降级（机会信号在非放行环境）
    gated: bool = False
    reasons: list[str] = field(default_factory=list)
    #: 回测回填
    forward: dict[str, float] = field(default_factory=dict)

    @property
    def alert_id(self) -> str:
        return f"{self.date}-{self.code}-{self.kind}"

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def level_label(self) -> str:
        return LEVEL_LABELS.get(self.level, self.level)

    @property
    def regime_label(self) -> str:
        return REGIME_LABELS.get(self.regime, self.regime)

    def to_dict(self) -> dict[str, Any]:
        return {"alert_id": self.alert_id, "date": self.date,
                "code": self.code, "name": self.name, "group": self.group,
                "group_label": self.group_label, "kind": self.kind,
                "kind_label": self.kind_label, "level": self.level,
                "level_label": self.level_label,
                "index_code": self.index_code, "index_name": self.index_name,
                "index_percentile": self.index_percentile,
                "change_1d": self.change_1d, "change_5d": self.change_5d,
                "resonance": self.resonance,
                "resonance_count": self.resonance_count,
                "resonance_total": self.resonance_total,
                "regime": self.regime, "regime_label": self.regime_label,
                "gated": self.gated,
                "reasons": list(self.reasons), "forward": dict(self.forward)}


def evaluate(indicators: dict[str, EtfIndicator],
             positions: dict[str, IndexPosition], *,
             config: FlowConfig, trade_date: str = "",
             regime: MarketRegime | None = None) -> list[FlowSignal]:
    """按需求 3.2 / 3.3 / 3.4 生成信号。

    信号来源有三种，判定顺序：
        1. 宽基机会/风险（要求指数分位在极端区 + 份额同向大幅变化）
        2. 行业 ETF 反转警示（只要求份额 5 日大增，**不看指数分位**）
        3. 弱信号（份额日环比超阈值但指数分位不在极端区 → 进观察列表）
    共振验证在同系列内部做：同向 ETF 数 ≥ `resonance.min_etfs` 才算确认。

    `regime` 传入时，**机会信号**还要过环境门：`regime.opportunity_allowed`
    为假则降级为弱信号（`gated=True`）并说明原因，不静默丢弃。
    风险信号与行业反转警示不受环境门控（见模块文档第 5 条）。
    """
    out: list[FlowSignal] = []
    regime_key = regime.key if regime is not None else REGIME_RANGE
    for group in config.groups:
        if not group.etfs:
            continue
        position = positions.get(group.index) if group.index else None
        percentile = position.percentile if position is not None else None
        # 同系列按当日份额变化方向分桶（共振判定的分子/分母）
        rising = [item for item in group.etfs
                  if (indicators.get(item.code) or EtfIndicator(code=item.code))
                  .change_1d is not None
                  and (indicators[item.code].change_1d or 0) > 0]
        falling = [item for item in group.etfs
                   if (indicators.get(item.code) or EtfIndicator(code=item.code))
                   .change_1d is not None
                   and (indicators[item.code].change_1d or 0) < 0]
        min_etfs = int(config.threshold("resonance.min_etfs", 3) or 3)

        for spec in group.etfs:
            item = indicators.get(spec.code)
            if item is None or item.change_1d is None:
                continue
            signal = FlowSignal(
                date=item.trade_date or trade_date, code=spec.code,
                name=item.name, group=group.key, group_label=group.label,
                index_code=group.index, index_name=group.index_name,
                index_percentile=percentile, change_1d=item.change_1d,
                change_5d=item.change_5d, regime=regime_key)

            if group.level == "industry":
                # 行业 ETF：只出反转警示（理由见模块文档）
                limit = float(config.threshold(
                    "industry_reversal.min_share_change_5d", 0.10) or 0.10)
                if item.change_5d is not None and item.change_5d > limit:
                    signal.kind = KIND_INDUSTRY_REVERSAL
                    signal.level = LEVEL_MEDIUM
                    signal.reasons.append(
                        f"行业 ETF 份额 5 日累计 {item.change_5d * 100:+.1f}%"
                        f"（> {limit * 100:.0f}%）：个人资金追涨特征，"
                        "短期反转风险上升")
                    out.append(signal)
                continue

            # 宽基：机会 / 风险
            hit = _match_broad(signal, item, config, percentile)
            if hit is not None:
                kind, level, reasons = hit
                signal.kind, signal.level = kind, level
                signal.reasons.extend(reasons)
                same_side = rising if kind == KIND_OPPORTUNITY else falling
                signal.resonance_count = len(same_side)
                signal.resonance_total = len(group.etfs)
                signal.resonance = len(same_side) >= min_etfs
                if signal.resonance and not _is_gated(kind, regime):
                    # 共振把中信号提升为强信号（需求 3.4）
                    signal.level = LEVEL_STRONG
                    signal.reasons.append(
                        f"共振确认：{group.label} {len(same_side)}/{len(group.etfs)} "
                        "只同向变化")
                if _is_gated(kind, regime):
                    # 机会/风险信号在非放行环境：降级为观察，不静默丢弃
                    signal.level = LEVEL_WEAK
                    signal.gated = True
                    allowed = sorted(_allowed_regimes_for(config, kind))
                    labels = "/".join(REGIME_LABELS.get(item, item)
                                      for item in allowed) or "（无）"
                    signal.reasons.append(
                        f"⛔ 环境门控：当前 {signal.regime_label}"
                        f"（参考指数近 {regime.window} 日 "
                        f"{(regime.change or 0) * 100:+.1f}%），"
                        f"{signal.kind_label}只在 {labels} 放行 —— "
                        "降级为观察，不构成仓位建议")
                out.append(signal)
                continue

            # 弱信号：份额异动但指数不在极端区 → 观察列表
            weak = float(config.threshold("weak_share_change_1d", 0.03) or 0.03)
            if abs(item.change_1d) > weak:
                signal.kind = (KIND_OPPORTUNITY if item.change_1d > 0
                               else KIND_RISK)
                signal.level = LEVEL_WEAK
                signal.resonance_count = len(
                    rising if item.change_1d > 0 else falling)
                signal.resonance_total = len(group.etfs)
                signal.reasons.append(
                    f"份额日环比 {item.change_1d * 100:+.1f}% 超过 "
                    f"{weak * 100:.0f}%，但指数分位 "
                    + ("未知" if percentile is None
                       else f"{percentile * 100:.0f}%")
                    + " 不在极端区域 → 进入观察列表")
                out.append(signal)
    order = {LEVEL_STRONG: 0, LEVEL_MEDIUM: 1, LEVEL_WEAK: 2, LEVEL_NONE: 3}
    out.sort(key=lambda item: (order.get(item.level, 9), item.code))
    return out


def _allowed_regimes_for(config: FlowConfig, kind: str) -> set[str]:
    """某个信号类型放行的环境集合（与 `apply_regime_policy` 同一口径）。"""
    if kind == KIND_RISK:
        return _allowed_regimes(config, "risk_allowed",
                                default=[REGIME_BULL, REGIME_RANGE])
    return _allowed_regimes(config, "opportunity_allowed",
                            default=[REGIME_BEAR])


def _is_gated(kind: str, regime: MarketRegime | None) -> bool:
    """该信号在当前环境下是否应被降级。

    `regime` 需先经过 `apply_regime_policy` 填充放行标记；为 None 时
    一律不门控（回测的冷启动期、以及未传环境的历史调用都走这条）。
    行业反转警示不参与门控：它衡量的是行业 ETF 的散户追涨，与大盘环境无关。
    """
    if regime is None:
        return False
    if kind == KIND_OPPORTUNITY:
        return not regime.opportunity_allowed
    if kind == KIND_RISK:
        return not regime.risk_allowed
    return False


def _match_broad(signal: FlowSignal, item: EtfIndicator, config: FlowConfig,
                 percentile: float | None
                 ) -> tuple[str, str, list[str]] | None:
    """宽基的机会/风险判定（返回 `(kind, level, reasons)` 或 None）。"""
    if percentile is None:
        return None
    opp_max = float(config.threshold("opportunity.max_percentile", 0.30) or 0.30)
    opp_1d = float(config.threshold("opportunity.min_share_change_1d", 0.03) or 0.03)
    opp_5d = float(config.threshold("opportunity.min_share_change_5d", 0.05) or 0.05)
    risk_min = float(config.threshold("risk.min_percentile", 0.70) or 0.70)
    risk_1d = float(config.threshold("risk.max_share_change_1d", -0.03) or -0.03)
    risk_5d = float(config.threshold("risk.max_share_change_5d", -0.05) or -0.05)

    if percentile <= opp_max and (item.change_1d or 0) > opp_1d:
        reasons = [f"指数分位 {percentile * 100:.1f}% ≤ {opp_max * 100:.0f}%（低位）",
                   f"份额日环比 {(item.change_1d or 0) * 100:+.2f}% "
                   f"> {opp_1d * 100:.1f}%"]
        if item.change_5d is not None and item.change_5d > opp_5d:
            reasons.append(f"5 日累计 {item.change_5d * 100:+.2f}% "
                           f"> {opp_5d * 100:.1f}%（增强条件满足）")
        return KIND_OPPORTUNITY, LEVEL_MEDIUM, reasons
    if percentile >= risk_min and (item.change_1d or 0) < risk_1d:
        reasons = [f"指数分位 {percentile * 100:.1f}% ≥ {risk_min * 100:.0f}%（高位）",
                   f"份额日环比 {(item.change_1d or 0) * 100:+.2f}% "
                   f"< {risk_1d * 100:.1f}%"]
        if item.change_5d is not None and item.change_5d < risk_5d:
            reasons.append(f"5 日累计 {item.change_5d * 100:+.2f}% "
                           f"< {risk_5d * 100:.1f}%（增强条件满足）")
        return KIND_RISK, LEVEL_MEDIUM, reasons
    return None


# ==================================================================
# 快照
# ==================================================================


@dataclass
class FlowSnapshot:
    """面板一次完整载荷。"""

    trade_date: str = ""
    generated_at: str = ""
    session_label: str = ""
    regime: MarketRegime = field(default_factory=MarketRegime)
    indicators: list[EtfIndicator] = field(default_factory=list)
    positions: list[IndexPosition] = field(default_factory=list)
    signals: list[FlowSignal] = field(default_factory=list)
    #: 每个系列的当日共振状态（仪表盘用）
    resonance: list[dict[str, Any]] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    disclaimer: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"trade_date": self.trade_date,
                "generated_at": self.generated_at,
                "session_label": self.session_label,
                "regime": self.regime.to_dict(),
                "indicators": [item.to_dict() for item in self.indicators],
                "positions": [item.to_dict() for item in self.positions],
                "signals": [item.to_dict() for item in self.signals],
                "resonance": self.resonance,
                "signal_count": len(self.signals),
                "alert_count": sum(1 for item in self.signals
                                   if item.level in (LEVEL_STRONG, LEVEL_MEDIUM)),
                "gated_count": sum(1 for item in self.signals if item.gated),
                "source_notes": self.source_notes, "gaps": self.gaps,
                "disclaimer": self.disclaimer}


def build_resonance(indicators: dict[str, EtfIndicator],
                    config: FlowConfig) -> list[dict[str, Any]]:
    """每个系列的共振仪表盘数据（红/绿指示灯 + 共振标记）。"""
    out: list[dict[str, Any]] = []
    min_etfs = int(config.threshold("resonance.min_etfs", 3) or 3)
    for group in config.groups:
        rows: list[dict[str, Any]] = []
        rising = falling = 0
        for spec in group.etfs:
            item = indicators.get(spec.code)
            change = item.change_1d if item is not None else None
            if change is not None:
                if change > 0:
                    rising += 1
                elif change < 0:
                    falling += 1
            rows.append({"code": spec.code, "name": spec.name,
                         "change_1d": change,
                         "change_5d": item.change_5d if item else None,
                         "direction": ("in" if (change or 0) > 0
                                       else ("out" if (change or 0) < 0
                                             else "flat"))})
        side = max(rising, falling)
        out.append({"group": group.key, "label": group.label,
                    "level": group.level, "index": group.index,
                    "index_name": group.index_name,
                    "rising": rising, "falling": falling,
                    "total": len(group.etfs),
                    "resonance": side >= min_etfs,
                    "direction": ("in" if rising > falling
                                  else ("out" if falling > rising else "flat")),
                    "etfs": rows})
    return out


def config_path() -> Path:
    return PROJECT_ROOT / "configs" / CONFIG_FILE


# ==================================================================
# 快照组装（API / 面板入口）
# ==================================================================

#: 组装快照时回看的自然日数。
#: 环境窗口要 120 个**交易日** ≈ 175 个自然日，份额最长的累计窗口是 20 日，
#: 再留一倍余量防节假日与数据缺口 —— 400 个自然日足够，且单次查询量很小
#: （9 只 ETF × 约 270 行）。取得过大只会让每次刷新多读无用的行。
SNAPSHOT_LOOKBACK_DAYS = 400


def build_snapshot(store: Any, *, config: FlowConfig | None = None,
                   trade_date: str = "") -> FlowSnapshot:
    """从本地仓组装一次 ETF 份额监控快照（**只读**，不发网络请求）。

    数据链路：`ml_etf`（份额+行情）→ 指标 → 分位 → 环境 → 信号 → 共振。

    无论哪一步缺数据都在 `gaps` 里说明并继续：面板要能显示"份额还没同步"
    这类状态，而不是整页报错。**没有数据不等于 0** —— 指标缺份额时
    `change_*` 保持 None，信号就不会被静默当成"份额没变"。
    """
    config = config or load_config()
    out = FlowSnapshot(trade_date=trade_date,
                       generated_at=datetime.now().astimezone().isoformat(
                           timespec="seconds"))
    if not config.loaded:
        out.gaps.append(config.gap or "ETF 份额监控配置不可用")
        return out
    if store is None:
        out.gaps.append("数据仓不可用，无法读取 ETF 份额")
        return out

    end = str(trade_date or "")
    if not end:
        try:
            rows = store._read("SELECT MAX(trade_date) AS d FROM ml_etf")  # noqa: SLF001
            end = str(rows[0]["d"] or "") if rows else ""
        except Exception as exc:  # noqa: BLE001 只读探测，任何异常都降级
            logger.warning("ETF 份额监控：探测最新交易日失败：%s",
                           type(exc).__name__)
            end = ""
    if not end:
        out.gaps.append("本地 ml_etf 表没有数据，请先同步 ETF 份额（etf 数据集）")
        return out
    start = _lookback_start(end, SNAPSHOT_LOOKBACK_DAYS)

    codes = [spec.code for spec in config.all_etfs]
    indexes = [group.index for group in config.groups if group.index]
    try:
        bars = store.etf_bars(codes, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        out.gaps.append(f"读取 ETF 行情/份额失败：{type(exc).__name__}")
        bars = {}
    try:
        index_bars = store.index_bars(indexes, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        out.gaps.append(f"读取指数日线失败：{type(exc).__name__}")
        index_bars = {}

    missing = [code for code in codes if not bars.get(code)]
    if missing:
        out.gaps.append(f"{len(missing)} 只 ETF 本地无数据：" + "、".join(missing[:8]))

    out.trade_date = _latest_bar_date(bars) or end
    indicators = build_indicators(bars, config, trade_date=out.trade_date)
    positions = build_positions(index_bars, config=config,
                               trade_date=out.trade_date)
    out.regime = build_regime(index_bars, config=config,
                              trade_date=out.trade_date)
    out.indicators = [indicators[spec.code] for spec in config.all_etfs
                      if spec.code in indicators]
    out.positions = list(positions.values())
    out.signals = evaluate(indicators, positions, config=config,
                           trade_date=out.trade_date, regime=out.regime)
    out.resonance = build_resonance(indicators, config)
    out.gaps.extend(out.regime.gaps)
    if not out.regime.opportunity_allowed:
        out.source_notes.append(
            f"当前 {out.regime.label}：机会信号不放行（只进观察列表）"
            "—— 机会信号仅在熊市有效，详见 docs/ETF_FLOW_BACKTEST.md")
    out.disclaimer = ("份额数据次日更新，信号为 T+1 确认，不适合日内做 T。"
                      "本监控为量化统计结果，不构成投资建议。")
    return out


def _lookback_start(end: str, days: int) -> str:
    """把 `YYYYMMDD` 往前推 `days` 个自然日（解析失败时原样返回 end）。"""
    text = str(end or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (moment - timedelta(days=int(days))).strftime("%Y%m%d")
    return text


def _latest_bar_date(bars: dict[str, list[dict[str, Any]]]) -> str:
    """所有 ETF 里最新的那个交易日（份额更新到哪天以最靠前的为准）。"""
    latest = ""
    for rows in bars.values():
        for row in rows:
            value = str(row.get("trade_date") or "")
            if value > latest:
                latest = value
    return latest


__all__ = [
    "CONFIG_FILE",
    "DEFAULT_REGIME_BEAR",
    "DEFAULT_REGIME_BULL",
    "DEFAULT_REGIME_WINDOW",
    "KIND_INDUSTRY_REVERSAL",
    "KIND_LABELS",
    "KIND_OPPORTUNITY",
    "KIND_RISK",
    "LEVEL_LABELS",
    "LEVEL_MEDIUM",
    "LEVEL_NONE",
    "LEVEL_STRONG",
    "LEVEL_WEAK",
    "REGIME_BEAR",
    "REGIME_BULL",
    "REGIME_LABELS",
    "REGIME_RANGE",
    "EtfIndicator",
    "EtfSpec",
    "FlowConfig",
    "FlowSignal",
    "FlowSnapshot",
    "IndexPosition",
    "MarketRegime",
    "WatchGroup",
    "apply_regime_policy",
    "build_indicators",
    "build_positions",
    "build_regime",
    "build_resonance",
    "build_snapshot",
    "classify_regime",
    "config_path",
    "evaluate",
    "index_percentile",
    "load_config",
]
