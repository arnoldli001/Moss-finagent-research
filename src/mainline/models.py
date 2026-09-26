"""主线挖掘：领域模型（纯数据，无 IO、无算法）。

这个文件是模块的**契约**：数据源层产出这里的输入结构，打分层消费它们并产出
评分结构，回测层消费评分结构，API 层把它们序列化成 `to_dict()`。

## 三个刻意的设计决定

**1. 每个分数都带 `notes` / `gap`。**

结论要能追溯（项目规则：所有分析结论必须附带数据溯源标签）。一个 60 分的
"主力资金强度"如果不说清"哪几日、多少亿、相对什么基准的 z-score"，用户无法判断
该不该信；而取数失败时**必须写清缺口**，不能用 0 分冒充"没有异常"（这两者
在界面上长得一样，但一个是"没事"、一个是"不知道"）。

**2. 缺数据不参与加权。**

`available=False` 的维度在合成时会从权重里剔除并重新归一化，而不是当 0 分。
把"没取到数"当 0 分会系统性压低所有板块的分数 —— 数据源一抖动，全市场都不告警。

**3. 分数口径统一在 0-100，权重口径统一在百分比。**

`BoardScore.total` 与三个子模型分都是 0-100（告警阈值直接比它）；
`weights` 是百分比（同一份 `weights` 加总 100）。混用会让阈值判断失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ==================================================================
# 枚举
# ==================================================================


class BoardKind(str, Enum):
    """板块类型（决定取数路径与差异化参数）。"""

    SW_L1 = "sw_l1"        # 申万一级行业（sw_daily）
    CONCEPT = "concept"    # 同花顺概念板块（ths_daily）


class SignalLevel(str, Enum):
    """告警等级（需求 3.6）。"""

    STRONG = "strong"      # 🔴 强信号
    MEDIUM = "medium"      # 🟡 中信号
    WEAK = "weak"          # 🟢 弱信号
    NONE = "none"          # 未触发

    @property
    def label(self) -> str:
        return {"strong": "🔴 强信号", "medium": "🟡 中信号",
                "weak": "🟢 弱信号", "none": "未触发"}[self.value]

    @property
    def rank(self) -> int:
        """用于 `min_level` 过滤（数字越大越强）。"""
        return {"none": 0, "weak": 1, "medium": 2, "strong": 3}[self.value]


class FutureKind(str, Enum):
    """期货品种类别（需求 5.1 的三分类）。"""

    DOMESTIC = "domestic"      # 内盘商品期货（55）
    FOREIGN = "foreign"        # 外盘期货/指数（25）
    NON_FUTURES = "non_futures"  # 非期货品种（9）：汇率/LOF/美债等


class FutureSignalKind(str, Enum):
    """期货异动信号类型（需求 5.4 的四类）。"""

    MOMENTUM = "momentum"                # 信号一：价格动量
    CORRELATION_JUMP = "correlation_jump"  # 信号二：期股相关性跃升
    OPEN_INTEREST = "open_interest"      # 信号三：持仓量异动
    TERM_STRUCTURE = "term_structure"    # 信号四：期限结构变化


# ==================================================================
# 数据源产出
# ==================================================================


@dataclass
class BoardInfo:
    """一个可评分的板块（申万一级行业或同花顺概念）。"""

    code: str                              # 801010.SI / 885898.TI / BK1182.DC
    name: str
    kind: BoardKind = BoardKind.CONCEPT
    members: int = 0                       # 成分股数量（0=未知）
    source: str = ""
    list_date: str = ""

    @property
    def ts_code(self) -> str:
        return self.code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "kind": self.kind.value,
                "members": self.members, "source": self.source}


@dataclass
class BoardBar:
    """板块指数一根日线。"""

    date: str                    # YYYYMMDD
    close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0          # 手 / 万手（口径随数据源，只用于比值）
    amount: float = 0.0          # 元
    pct_change: float = 0.0      # %
    #: 前收盘价。**`sw_daily` 不直接返回它**（只有 `change` 涨跌额），
    #: 落库时按 `close - change` 补算；`ths_daily` 则直接给 `pre_close`。
    #: 隔夜跳空维度完全依赖这个字段，缺失会让整列静默变成 None。
    pre_close: float = 0.0


@dataclass
class BoardSeries:
    """板块指数日线序列（升序）。"""

    code: str
    name: str = ""
    bars: list[BoardBar] = field(default_factory=list)
    source: str = ""
    gap: str = ""

    @property
    def available(self) -> bool:
        return len(self.bars) > 0

    @property
    def dates(self) -> list[str]:
        return [bar.date for bar in self.bars]

    @property
    def closes(self) -> list[float]:
        return [bar.close for bar in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [bar.volume for bar in self.bars]

    def to_dict(self, *, limit: int = 250) -> dict[str, Any]:
        tail = self.bars[-limit:]
        return {"code": self.code, "name": self.name, "source": self.source,
                "available": self.available, "gap": self.gap,
                "bars": [{"date": bar.date, "close": bar.close,
                          "open": bar.open, "high": bar.high, "low": bar.low,
                          "volume": bar.volume, "amount": bar.amount,
                          "pct_change": bar.pct_change} for bar in tail]}


@dataclass
class BoardFlow:
    """板块资金流（东财口径，单位：元）。

    ⚠️ **口径统一**（需求 8.4）：东财、同花顺、Tushare 的"主力资金"定义不同。
    本模块纵向对比**固定用东财口径**（`moneyflow_ind_dc` / 东财 push2 接口），
    不混用其它源的主力定义。`source` 字段用于把这个口径传给前端展示。
    """

    code: str
    name: str = ""
    points: list[tuple[str, float]] = field(default_factory=list)  # [(date, net)]
    source: str = ""
    gap: str = ""
    #: `moneyflow_ind_dc` 的 `content_type`：行业 / 概念（用于区分两类板块）
    content_type: str = ""
    #: 当日涨跌幅（%）与净占比（%）—— 只有截面（单日）数据才有
    change_pct: float | None = None
    net_rate: float | None = None
    rank: float | None = None
    buy_elg: float = 0.0
    buy_lg: float = 0.0

    @property
    def available(self) -> bool:
        return len(self.points) > 0

    def net_on(self, date: str) -> float | None:
        for stamp, net in self.points:
            if stamp == date:
                return net
        return None

    def latest(self) -> tuple[str, float] | tuple[str, None]:
        return self.points[-1] if self.points else ("", None)

    def to_dict(self, *, limit: int = 120) -> dict[str, Any]:
        tail = self.points[-limit:]
        return {"code": self.code, "name": self.name, "source": self.source,
                "available": self.available, "gap": self.gap,
                "content_type": self.content_type,
                "change_pct": self.change_pct,
                "net_rate": self.net_rate, "rank": self.rank,
                "buy_elg": self.buy_elg, "buy_lg": self.buy_lg,
                "points": [{"date": date, "net": net} for date, net in tail]}


@dataclass
class StockFlow:
    """个股主力净流入（元）与流通市值（元）。

    `circ_mv` 为 None 表示市值没取到 —— 比值类指标（净额/市值）必须跳过它，
    不能当 0（当 0 会让这只票在比值榜上排到最前）。
    """

    code: str
    name: str = ""
    net_series: list[tuple[str, float]] = field(default_factory=list)
    circ_mv: float | None = None
    total_mv: float | None = None
    close_series: list[tuple[str, float]] = field(default_factory=list)
    amount_series: list[tuple[str, float]] = field(default_factory=list)
    source: str = ""

    def net_window(self, window: int) -> float | None:
        """近 N 日净流入合计（不足 N 日返回实际天数之和；无数据返回 None）。"""
        if not self.net_series:
            return None
        tail = self.net_series[-window:]
        return float(sum(net for _, net in tail if net is not None))

    def close_on(self, date: str) -> float | None:
        for stamp, close in self.close_series:
            if stamp == date:
                return close
        return None


@dataclass
class MarginRow:
    """融资融券一行（融资余额单位：元）。"""

    code: str
    date: str
    rzye: float = 0.0        # 融资余额
    rqye: float = 0.0        # 融券余额
    net_buy: float = 0.0     # 融资净买入


@dataclass
class NorthboundRow:
    """北向资金一行。

    ⚠️ 2024-08 起交易所停止披露北向**日度个股**净买入，只有沪深股通汇总
    （`moneyflow_hsgt`）。因此 `stock_code` 为空时表示"只有总量、没有个股归属"，
    此时个股级维度必须如实标注缺口而不是编造。
    """

    date: str
    north_money: float = 0.0     # 北向合计净流入（万元，Tushare 原口径）
    stock_code: str = ""
    net_buy: float | None = None  # 个股级净买入（有则用，无则 None）


@dataclass
class HolderRow:
    """股东户数一行（季报频率，滞后 1-3 个月）。"""

    code: str
    end_date: str
    ann_date: str = ""
    holder_num: float | None = None
    #: 环比变化率（本期/上期 - 1）；负值=户数下降=筹码集中
    change_ratio: float | None = None


@dataclass
class SeatRow:
    """龙虎榜机构/营业部席位一行（金额单位：元）。"""

    date: str
    code: str
    exalter: str = ""            # 营业部名称
    buy: float = 0.0
    sell: float = 0.0
    net_buy: float = 0.0
    side: str = ""               # 0=买方 / 1=卖方
    reason: str = ""

    @property
    def is_institution(self) -> bool:
        """机构专用席位判定（Tushare 的席位名里带「机构专用」）。"""
        return "机构专用" in self.exalter


@dataclass
class FundPoint:
    """行情快照里的一只个股（个股级打分的输入）。"""

    code: str
    date: str
    close: float = 0.0
    pct_chg: float = 0.0
    circ_mv: float | None = None
    amount: float = 0.0
    turnover_rate: float | None = None


# ==================================================================
# 评分
# ==================================================================


@dataclass
class DimensionScore:
    """一个子维度的评分。

    评分统一 0-100；`available=False` 时 `score` 不参与加权（见 `_weighted`）。
    """

    key: str
    label: str
    score: float = 0.0
    weight: float = 0.0          # 该维度在所属层里的权重（百分比）
    raw: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    available: bool = True
    #: 该维度是否按**反向**参与层合成（层里实际用的是 `100 - score`）。
    #:
    #: 为什么需要这个标记：实测（`scripts/factor_ic_report.py`）`trading` 与
    #: `technical` 的 IC 在四个持有期上单调为负（A 股概念板块 20~60 日尺度上
    #: 均值回归），所以它们应当**反向**进入合成。但 `weighted_score` 会把
    #: `weight <= 0` 的项丢掉，"负权重"表达不了，只能在合成时取 `100 - score`。
    #:
    #: `score` 仍然存**自然分**（"量能 90"就是量能强），只有合成走反转 ——
    #: 否则面板上会出现"量能很强但得分 10"这种自相矛盾的显示。
    reversed: bool = False

    @property
    def effective_score(self) -> float:
        """进入层合成时实际使用的分（反向维度取 `100 - score`）。"""
        return (100.0 - self.score) if self.reversed else self.score

    @property
    def contribution(self) -> float:
        """对所属层总分的贡献（= 实际参与合成的分 × 权重 / 100）。"""
        if not self.available:
            return 0.0
        return self.effective_score * self.weight / 100.0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label,
                "score": round(self.score, 2), "weight": round(self.weight, 2),
                "contribution": round(self.contribution, 2),
                "raw": self.raw, "note": self.note,
                "available": self.available, "reversed": self.reversed}


@dataclass
class LayerScore:
    """一层（六维 / 五维 / 龙头）的合成结果。"""

    key: str
    label: str
    score: float = 0.0
    dimensions: list[DimensionScore] = field(default_factory=list)
    weight: float = 0.0          # 该层在三层合成里的权重（百分比）
    notes: list[str] = field(default_factory=list)
    #: 参与加权的维度占比（<1 说明有维度缺数据；前端要能看见）
    coverage: float = 1.0

    @property
    def available(self) -> bool:
        return any(dim.available for dim in self.dimensions)

    def dim(self, key: str) -> DimensionScore | None:
        for item in self.dimensions:
            if item.key == key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label,
                "score": round(self.score, 2), "weight": round(self.weight, 2),
                "coverage": round(self.coverage, 3),
                "available": self.available,
                "dimensions": [dim.to_dict() for dim in self.dimensions],
                "notes": list(self.notes)}


@dataclass
class LeaderInfo:
    """一只动态识别出的龙头股（三维交集法）。

    ## ⚠️ 这里的"龙头"是**资金/动量意义上的**，与业务相关性无关

    `identify_leaders()` 只看三个维度（近 5 日主力净流入 / 近 10 日涨幅 /
    近 5 日成交额），业务相关性**从不参与**。所以一只主营与该概念关系不大的
    股票，只要交易活跃就会被选成"龙头"。

    用户 2026-09-22 就是被这一点误导的：氟化工概念的龙头里出现
    **雅克科技**（LLM 主营判定最高的是"半导体材料 90"，8 个题材里没有一个
    含氟），它是靠 `ml_member_pure` 的 `corr 0.610` 入池的。

    因此这里补几个**只用于展示**的字段（不影响选取、不影响任何分数）：
    `business_score` / `business_source` / `business_reason` / `admission` ——
    让"资金龙头"与"业务龙头"在界面上**分得开**。

    ⚠️ 2026-09-23 补充：只显示业务分还不够，**必须同时显示"它凭什么进的池"**。
    带 `corr` 入池、但 LLM 其实判过且给了低分的票（如东华科技 002140
    在钛白粉概念：`corr 0.564`、LLM 主营 65 分 < 70 门槛）会被旧标签
    说成"LLM 未判过该题材" —— 与数据相反。见 `admission` / `business_label`。
    """

    code: str
    name: str = ""
    net_5d: float | None = None          # 近 5 日主力净流入（元）
    ret_10d: float | None = None         # 近 10 日涨幅（%）
    amount_ratio: float | None = None    # 近 5 日成交额占板块比例
    capital_rank: int = 0
    momentum_rank: int = 0
    volume_rank: int = 0
    #: 三维各自命中数（3 = 三维交集，最强）
    hits: int = 0
    source: str = ""
    #: LLM 对「(这只股票, 这个题材)」的主营相关度（0-100）；None = 没判过
    business_score: float | None = None
    #: 业务分的来源：`llm`（判过并给分）/ `corr`（只按相关性入的池）/ `""`（无）
    business_source: str = ""
    #: 一句话说明（如「主营分 85 ≥ 70」「corr 0.610 ≥ 0.55」）
    business_reason: str = ""
    #: 入池通道（**只用于展示**）：`corr` = 相关性直通 / `business` = 主营分达标 /
    #: `corr_business_failed` = 靠相关性入池、但 LLM 判过且**未达** 70 分门槛。
    #:
    #: ⚠️ 为什么要和 `business_source` 分开：`business_source` 回答"业务分从哪来"，
    #: `admission` 回答"这只票凭什么进的池"。2026-09-23 用户报障的
    #: `002140 东华科技 @ 885652 钛白粉概念` 正是 `corr` + `business_score=65`：
    #: 旧代码只看 `business_source == "corr"` 就显示「LLM **未判过**该题材」，
    #: 而事实是 LLM 判过、给了 65 分（低于 70 门槛）。**展示在与数据相反**，
    #: 这正是 16.67 警告过的那类"用错误信息误导用户"。
    admission: str = ""

    @property
    def business_warn(self) -> bool:
        """业务口径是否需要警示（前端用警示色标出）。"""
        return self.admission == "corr_business_failed"

    @property
    def business_label(self) -> str:
        """给前端用的一句话标签：这只票凭什么是这个概念的成员。

        四种情形**必须分开**（`score` 有没有值与"判过没判过"是两件事）：

            corr + 无分   → 相关性直通，LLM 从未判过这个题材（中性，无证据）
            corr + 有分   → ⚠️ **判过但低于门槛**：有明确反向证据仍按相关性入池
            llm/llm_theme → 业务分达标（正常）
            无判定        → 既没业务分也没入池通道（展示字段缺失）
        """
        score = self.business_score
        if self.business_source in ("llm", "llm_theme") and score is not None:
            return f"业务相关 {score:.0f} 分"
        if self.business_source == "corr":
            if score is None:
                return "仅股价相关（LLM 未判过该题材）"
            # 判过且被相关性通道覆盖 —— 业务分必须显示出来，不能吞掉
            return f"业务仅 {score:.0f} 分（未达门槛，凭股价相关入池）"
        return "无业务判定"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name,
                "net_5d": self.net_5d, "ret_10d": self.ret_10d,
                "amount_ratio": self.amount_ratio,
                "capital_rank": self.capital_rank,
                "momentum_rank": self.momentum_rank,
                "volume_rank": self.volume_rank, "hits": self.hits,
                "source": self.source,
                # ---- 以下只用于展示，不参与选取与打分 ----
                "business_score": self.business_score,
                "business_source": self.business_source,
                "business_reason": self.business_reason,
                "business_label": self.business_label,
                "admission": self.admission,
                "business_warn": self.business_warn}


@dataclass
class BoardScore:
    """一个板块在某交易日的完整漏斗评分（V2.0）。

    ## 漏斗语义（三个字段必须一起看）

        candidate  第一层是否进了候选池（前 20%）—— **False 时第二层根本没算**，
                   此时 `accumulation.score == 0` 是"没算"而不是"算出来是 0 分"。
        selected   第二层是否进了精选名单（前 5-10）
        total      最终预警分 = 第一层×w + 第二层×(100−w) + gate_bonus，
                   `w` = `synthesis.layer_weights.six_dim`（V2.3 起为 100）

    把"没算"和"0 分"混起来是漏斗式设计最容易出的错：候选池外的板块
    `accumulation` 全 0，如果不看 `candidate` 就下钻，会得出"这个板块
    建仓痕迹极弱"的结论 —— 而事实是我们**没看**它。
    """

    code: str
    name: str
    trade_date: str
    kind: BoardKind = BoardKind.CONCEPT

    six_dim: LayerScore = field(
        default_factory=lambda: LayerScore("six_dim", "开源六维基座"))
    accumulation: LayerScore = field(
        default_factory=lambda: LayerScore("accumulation", "建仓痕迹"))
    leader: LayerScore = field(
        default_factory=lambda: LayerScore("leader", "龙头共振确认"))

    total: float = 0.0                  # 最终预警分（0-100）
    weights: dict[str, float] = field(default_factory=dict)
    weight_mode: str = "static"         # static=初始权重 / dynamic=ICIR / equal=回退等权

    # ---------- 漏斗 ----------
    rank: int = 0                       # 第一层横截面排名（1 = 第一）
    candidate: bool = False             # 是否进入候选池（第二层的计算范围）
    selected: bool = False              # 是否进入精选名单
    #: 是否由**龙头共振提名**进入告警。为 True 时该板块**没有经过第二层**
    #: （`accumulation.score` 恒为 0，`base_total` 只由第一层构成），
    #: 它的总分与其他板块不可直接比较 —— 面板必须单独标注这一类，
    #: 否则会让人以为"这个板块的建仓痕迹分是 0 分"，而事实是**没算**。
    promoted: bool = False
    gate_bonus: float = 0.0             # 第三层门控加分（0 = 未触发共振）
    #: 第二层的 **ETF 异动加分**（0~`etf.bonus_cap`，不占权重）。
    #:
    #: 与 `gate_bonus` 并列，但两者在"什么时候兑现"上刻意不同：
    #: `gate_bonus` 要先入选精选才兑现（共振太常见，全发会让告警失去区分度），
    #: `etf_bonus` **无条件兑现** —— ETF 历史级放量是稀有事件
    #: （324 个板块里只有 22~35 个有 ETF 映射，再要求 2 倍放量 + 90 分位），
    #: 而且它恰恰要能把"还没进精选"的板块顶上来，所以不能等入选才给。
    etf_bonus: float = 0.0
    #: ETF 异动级别 0/1/2（面板展示用；加分按 `etf_bonus` 读）
    etf_level: int = 0
    base_total: float = 0.0             # 未加门控/ETF 加分前的层合成分

    #: **兑现前**的门控加分（"潜力值"）。
    #:
    #: `gate_bonus` 只在板块入选精选后才兑现，未入选的被清零 —— 这是刻意的
    #: （见字段说明）。但**真正的排序键**是
    #: `base_total + bonus_potential + etf_bonus`（`service.py` 的 `pool = sorted(...)`），
    #: 而落库的 `total` 用的是**兑现后**的 `gate_bonus`。于是：
    #:
    #: - 库里 `total` ≈ "平滑分数 + 稀疏大跳变"（只有约 12% 的候选板块拿到
    #:   那 15~20 分），用它对未来收益算秩相关会**系统性低估**排序质量；
    #: - 实测（V2.2）`base_total` H=20 IC 是 +0.075/+0.085，而 `total` 只有
    #:   +0.052/+0.040 —— 差的这一截就是那个跳变。
    #:
    #: 所以额外存一个"兑现前"的值，让 IC 报告能衡量**真实排序键**。
    #: 详见 `docs/MAINLINE_MINING.md` §16.35。
    bonus_potential: float = 0.0

    resonance: bool = False             # 龙头共振是否触发
    resonance_ratio: float | None = None
    #: 🆕 是否**越过自己的长期震荡上沿**（与绝对阈值并行的第二条触发路径）。
    #: 绝对阈值是全市场统一的，所以"能不能报"取决于分数够不够极端；
    #: 而真正要抓的是"这个板块刚突破它自己的区间"。实测农业种植 885812
    #: 在 20260623 的六维排名是全市场第 8，但 `total` 只有 62.3、
    #: 低于中信号线 → 不报。见 `AlertRuleConfig.breakout_enabled`。
    breakout: bool = False
    #: 上沿值（该板块过去 N 天 `total` 的分位数）；历史不足时为 None
    breakout_ceiling: float | None = None
    leaders: list[LeaderInfo] = field(default_factory=list)
    seat_note: str = ""

    level: SignalLevel = SignalLevel.NONE
    reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    change_pct: float | None = None      # 当日涨跌幅（%）
    net_today: float | None = None       # 当日主力净流入（元）
    profile_key: str = "default"         # 板块类别（决定衰减周期）
    decay_days: int = 20

    def dim(self, key: str) -> DimensionScore | None:
        """跨层按 key 找维度（前端雷达图按 key 取数）。"""
        for layer in self.layers:
            found = layer.dim(key)
            if found is not None:
                return found
        return None

    def dims_above(self, ratio: float, thresholds: dict[str, float],
                   default_threshold: float) -> list[str]:
        """得分超过**各自阈值** `ratio` 倍的维度 key 列表（强信号判定用）。

        为什么按"各自阈值"而不是统一 60 分：资金流维度的分布天然比宏观维度宽，
        用同一个绝对线会让资金流几乎所有板块都达标、宏观几乎都不达标 ——
        于是"至少 2 个维度达标"实际退化成"资金流达标就行"。
        """
        out: list[str] = []
        for layer in (self.six_dim, self.accumulation):
            for item in layer.dimensions:
                if not item.available:
                    continue
                base = thresholds.get(item.key, default_threshold)
                if base <= 0:
                    base = default_threshold
                if item.score >= base * ratio:
                    out.append(item.key)
        return out

    @property
    def layers(self) -> list[LayerScore]:
        return [self.six_dim, self.accumulation, self.leader]

    def to_dict(self, *, with_dimensions: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code, "name": self.name,
            "trade_date": self.trade_date, "kind": self.kind.value,
            "total": round(self.total, 2),
            "base_total": round(self.base_total, 2),
            "six_dim_score": round(self.six_dim.score, 2),
            "accumulation_score": round(self.accumulation.score, 2),
            "leader_score": round(self.leader.score, 2),
            #: 各层**参与加权的维度占比**（<1 说明有维度缺数据）。
            #:
            #: ⚠️ 这两个字段是**纯展示**，不参与任何合成 —— 与 `etf_bonus` 无关，
            #: 是独立修的一个问题：`weighted_score` 会把 `available=False` 的
            #: 维度从分母剔除并重新归一化，于是"只有杠杆有数据的板块"和
            #: "四个维度齐全的板块"会得到**同等强度**的第二层分（第二层曾占
            #: 最终分的 50%，V2.3 起为 0）。分数本身没错（缺数据不该压低分数），
            #: 错的是**把两者并列展示却不告诉使用者覆盖率**。
            #:
            #: 之所以只展示、不打折：折扣系数本身又是一个自由参数，而当前
            #: 有效自由参数已经有 8 个、真值事件只有 45~50 个 —— 再加一个
            #: 只会加剧过拟合。等各维度 IC/ICIR 算出来再决定要不要打折。
            "six_dim_coverage": round(self.six_dim.coverage, 3),
            "accumulation_coverage": round(self.accumulation.coverage, 3),
            "weights": {key: round(value, 2)
                        for key, value in self.weights.items()},
            "weight_mode": self.weight_mode,
            "rank": self.rank, "candidate": self.candidate,
            "selected": self.selected,
            "promoted": self.promoted,
            "gate_bonus": round(self.gate_bonus, 2),
            #: 突破触发的两个字段（面板要能显示"它突破了哪个上沿"）
            "breakout": self.breakout,
            "breakout_ceiling": (round(self.breakout_ceiling, 2)
                                 if self.breakout_ceiling is not None else None),
            #: 兑现前的门控加分。`total` 用的是兑现后的 `gate_bonus`，
            #: 所以 IC 报告要用 `base_total + bonus_potential + etf_bonus`
            #: 才能衡量**真实排序键**（见 `BoardScore.bonus_potential`）。
            "bonus_potential": round(self.bonus_potential, 2),
            "etf_bonus": round(self.etf_bonus, 2),
            "etf_level": self.etf_level,
            "resonance": self.resonance,
            "resonance_ratio": (round(self.resonance_ratio, 4)
                                if self.resonance_ratio is not None else None),
            "leaders": [item.to_dict() for item in self.leaders],
            "seat_note": self.seat_note,
            "level": self.level.value, "level_label": self.level.label,
            "reasons": list(self.reasons), "gaps": list(self.gaps),
            "change_pct": self.change_pct, "net_today": self.net_today,
            "profile_key": self.profile_key, "decay_days": self.decay_days,
        }
        if with_dimensions:
            payload["layers"] = [layer.to_dict() for layer in self.layers]
        return payload


@dataclass
class AlertSignal:
    """一条告警信号（需求 7.2 的「告警信号流水」一行）。"""

    board_code: str
    board_name: str
    trade_date: str
    level: SignalLevel = SignalLevel.NONE
    score: float = 0.0
    kind: BoardKind = BoardKind.CONCEPT

    six_dim_score: float = 0.0
    accumulation_score: float = 0.0
    leader_score: float = 0.0
    gate_bonus: float = 0.0
    #: 是否由**龙头共振提名**产生（该板块未经第二层，分数不与精选板块直接可比）。
    #: 单独透出这个布尔量，是为了让面板/接口能把它筛出来或标星，
    #: 而不是只靠 `reasons` 里的一段中文去判断。
    promoted: bool = False
    triggered_dims: list[str] = field(default_factory=list)
    resonance: bool = False
    reasons: list[str] = field(default_factory=list)

    #: 告警当日板块指数收盘价（用于后续计算"告警后涨幅"）
    entry_close: float | None = None
    change_pct: float | None = None

    # ---- 上报后的兑现情况（回测/复盘时由 evaluate 回填）----
    max_gain_pct: float | None = None       # 告警后 N 日最大涨幅（%）
    max_gain_date: str = ""
    ret_5d: float | None = None
    ret_10d: float | None = None
    ret_20d: float | None = None
    ret_60d: float | None = None
    confirmed: bool = False                 # 「连续 2 个信号周期内触发」确认
    confirmed_date: str = ""
    pushed: bool = False
    push_note: str = ""

    @property
    def alert_id(self) -> str:
        """稳定 ID（同板块同日同等级唯一），供前端列表 key 与去重使用。"""
        return f"{self.trade_date}-{self.board_code}-{self.level.value}"

    def to_dict(self) -> dict[str, Any]:
        return {"alert_id": self.alert_id, "board_code": self.board_code,
                "board_name": self.board_name, "trade_date": self.trade_date,
                "kind": self.kind.value,
                "level": self.level.value, "level_label": self.level.label,
                "score": round(self.score, 2),
                "six_dim_score": round(self.six_dim_score, 2),
                "accumulation_score": round(self.accumulation_score, 2),
                "leader_score": round(self.leader_score, 2),
                "gate_bonus": round(self.gate_bonus, 2),
                "promoted": self.promoted,
                "triggered_dims": list(self.triggered_dims),
                "resonance": self.resonance,
                "reasons": list(self.reasons),
                "entry_close": self.entry_close,
                "change_pct": self.change_pct,
                "max_gain_pct": (round(self.max_gain_pct, 2)
                                 if self.max_gain_pct is not None else None),
                "max_gain_date": self.max_gain_date,
                "ret_5d": self.ret_5d, "ret_10d": self.ret_10d,
                "ret_20d": self.ret_20d, "ret_60d": self.ret_60d,
                "confirmed": self.confirmed,
                "confirmed_date": self.confirmed_date,
                "pushed": self.pushed, "push_note": self.push_note}


# ==================================================================
# 快照与报告
# ==================================================================


@dataclass
class MainlineSnapshot:
    """主线挖掘面板的一次完整快照。"""

    generated_at: str = ""
    trade_date: str = ""
    session_state: str = ""
    session_label: str = ""

    scores: list[BoardScore] = field(default_factory=list)      # 按 total 降序
    alerts: list[AlertSignal] = field(default_factory=list)
    weight_mode: str = "static"
    weights: dict[str, float] = field(default_factory=dict)
    ic_summary: dict[str, Any] = field(default_factory=dict)

    #: 漏斗统计（前端漏斗图直接用这四个数）
    board_count_total: int = 0
    candidate_count: int = 0
    selected_count: int = 0

    tracked: list[dict[str, Any]] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    refresh_hint: str = ""
    disclaimer: str = ""

    @property
    def board_count(self) -> int:
        return len(self.scores)

    def to_dict(self, *, top: int = 60, alert_limit: int = 100) -> dict[str, Any]:
        ranked = self.scores[:top]
        total = self.board_count_total or len(self.scores)
        return {
            "generated_at": self.generated_at,
            "trade_date": self.trade_date,
            "session_state": self.session_state,
            "session_label": self.session_label,
            "board_count": total,
            "scored_count": len(self.scores),
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "alert_count": len(self.alerts),
            "weight_mode": self.weight_mode,
            "weights": {key: round(value, 2)
                        for key, value in self.weights.items()},
            "ic_summary": self.ic_summary,
            "scores": [item.to_dict(with_dimensions=False) for item in ranked],
            "candidates": [item.to_dict(with_dimensions=False)
                           for item in self.scores if item.candidate][:top],
            "selected": [item.to_dict(with_dimensions=False)
                         for item in self.scores if item.selected][:top],
            "alerts": [item.to_dict() for item in self.alerts[:alert_limit]],
            "tracked": self.tracked,
            "source_notes": self.source_notes,
            "gaps": self.gaps,
            "refresh_hint": self.refresh_hint,
            "disclaimer": self.disclaimer,
        }


@dataclass
class SceneCase:
    """分场景验证的一个历史案例（需求 4.5）。

    `keywords` 是**板块名关键字列表**而不是单个字符串：同一个题材在不同
    数据源里的板块名不一样（"锂电" / "锂电池" / "碳酸锂" 是三个板块），
    只写一个关键字会让案例判定"恰好没匹配上"而误判为未触发。
    """

    key: str
    label: str
    start_date: str
    check_window: tuple[str, str] = ("", "")
    min_lead_days: int = 3
    keywords: list[str] = field(default_factory=list)
    #: 实际结果（回测回填）
    triggered: bool = False
    first_alert_date: str = ""
    first_alert_board: str = ""
    lead_days: int | None = None
    max_gain_pct: float | None = None
    ret_60d: float | None = None
    passed: bool = False
    note: str = ""

    def matches(self, board_name: str) -> bool:
        """板块名是否命中本案例（任一关键字包含即命中）。"""
        if not board_name:
            return False
        return any(word and word in board_name for word in self.keywords)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label,
                "start_date": self.start_date,
                "check_window": list(self.check_window),
                "min_lead_days": self.min_lead_days,
                "keywords": list(self.keywords),
                "triggered": self.triggered,
                "first_alert_date": self.first_alert_date,
                "first_alert_board": self.first_alert_board,
                "lead_days": self.lead_days,
                "max_gain_pct": self.max_gain_pct,
                "ret_60d": self.ret_60d, "passed": self.passed,
                "note": self.note}


@dataclass
class BacktestFold:
    """Purged Walk-Forward 的一个 fold。"""

    index: int
    train_start: str
    train_end: str
    purge_end: str
    validate_start: str
    validate_end: str
    weights: dict[str, float] = field(default_factory=dict)
    weight_mode: str = "static"
    ic_six: float | None = None
    ic_accumulation: float | None = None
    ic_leader: float | None = None
    ic_total: float | None = None
    ic_equal: float | None = None       # 等权对照（回退判定用）
    boards: int = 0
    alerts: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "train_start": self.train_start,
                "train_end": self.train_end, "purge_end": self.purge_end,
                "validate_start": self.validate_start,
                "validate_end": self.validate_end,
                "weights": {key: round(value, 2)
                            for key, value in self.weights.items()},
                "weight_mode": self.weight_mode,
                "ic_six": self.ic_six,
                "ic_accumulation": self.ic_accumulation,
                "ic_leader": self.ic_leader, "ic_total": self.ic_total,
                "ic_equal": self.ic_equal, "boards": self.boards,
                "alerts": self.alerts, "note": self.note}


@dataclass
class FactorCorrelation:
    """因子相关性验证报告（需求 4.4 的去重效果检验）。"""

    #: 参与检验的因子名（`层.维度` 形式，如 `six.moneyflow`）
    factors: list[str] = field(default_factory=list)
    #: 相关系数矩阵：{factor_a: {factor_b: corr}}
    matrix: dict[str, dict[str, float]] = field(default_factory=dict)
    #: 跨层高相关因子对 `[(a, b, corr)]`（|corr| > limit）
    high_pairs: list[tuple[str, str, float]] = field(default_factory=list)
    #: 跨层因子相关性的绝对值均值（去重效果的总指标，越小越好）
    cross_layer_mean_abs: float | None = None
    #: 跨层因子相关性的绝对值最大
    cross_layer_max_abs: float | None = None
    limit: float = 0.70
    samples: int = 0
    passed: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"factors": list(self.factors), "matrix": self.matrix,
                "high_pairs": [{"a": a, "b": b, "corr": round(c, 4)}
                               for a, b, c in self.high_pairs],
                "cross_layer_mean_abs": self.cross_layer_mean_abs,
                "cross_layer_max_abs": self.cross_layer_max_abs,
                "limit": self.limit, "samples": self.samples,
                "passed": self.passed, "note": self.note}


@dataclass
class BacktestMetrics:
    """评估指标（需求 4.3）。"""

    ic_mean: float | None = None
    ic_std: float | None = None
    icir: float | None = None
    ic_win_rate: float | None = None
    ic_samples: int = 0

    long_short_annual: float | None = None
    long_excess: float | None = None
    long_annual: float | None = None
    benchmark_annual: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None

    signal_count: int = 0
    signal_hit_rate: float | None = None      # 触发后 20 日涨幅 > 5% 比例
    false_positive_rate: float | None = None  # 触发后 20 日未上涨比例
    avg_max_gain: float | None = None         # 告警后 60 日最大涨幅均值
    median_max_gain: float | None = None

    by_dim: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_holding: dict[str, dict[str, Any]] = field(default_factory=dict)
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ic_mean": self.ic_mean, "ic_std": self.ic_std,
            "icir": self.icir, "ic_win_rate": self.ic_win_rate,
            "ic_samples": self.ic_samples,
            "long_short_annual": self.long_short_annual,
            "long_excess": self.long_excess, "long_annual": self.long_annual,
            "benchmark_annual": self.benchmark_annual,
            "sharpe": self.sharpe, "max_drawdown": self.max_drawdown,
            "signal_count": self.signal_count,
            "signal_hit_rate": self.signal_hit_rate,
            "false_positive_rate": self.false_positive_rate,
            "avg_max_gain": self.avg_max_gain,
            "median_max_gain": self.median_max_gain,
            "by_dim": self.by_dim, "by_holding": self.by_holding,
            "targets": self.targets,
        }


@dataclass
class BacktestReport:
    """一次回测的完整结果（对应需求六的五章报告）。"""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    range_start: str = ""
    range_end: str = ""
    config_note: str = ""

    metrics: BacktestMetrics = field(default_factory=BacktestMetrics)
    folds: list[BacktestFold] = field(default_factory=list)
    scenes: list[SceneCase] = field(default_factory=list)
    signals: list[AlertSignal] = field(default_factory=list)
    false_positives: list[AlertSignal] = field(default_factory=list)
    #: 因子相关性验证（需求 4.4）
    correlation: FactorCorrelation = field(default_factory=FactorCorrelation)

    markdown: str = ""
    gaps: list[str] = field(default_factory=list)
    error: str = ""
    disclaimer: str = ""

    def to_dict(self, *, signal_limit: int = 500) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "seconds": self.seconds,
            "range_start": self.range_start, "range_end": self.range_end,
            "config_note": self.config_note,
            "metrics": self.metrics.to_dict(),
            "folds": [fold.to_dict() for fold in self.folds],
            "scenes": [scene.to_dict() for scene in self.scenes],
            "signals": [item.to_dict() for item in self.signals[:signal_limit]],
            "false_positives": [item.to_dict()
                                for item in self.false_positives[:signal_limit]],
            "correlation": self.correlation.to_dict(),
            "gaps": self.gaps, "error": self.error,
            "disclaimer": self.disclaimer,
        }


# ==================================================================
# 期货先行信号
# ==================================================================


@dataclass
class FutureMapping:
    """期货品种 ↔ A 股板块的传导映射（配置表一行）。

    `strength` 是 1-5 星强度；每季度用过去 250 个交易日的滚动相关系数校准
    （需求 5.3），校准结果写进 `calibrated_strength` 而不覆盖人工设定的
    `strength` —— 这样"人工口径被自动逻辑改掉"这件事永远不会静默发生。
    """

    future_code: str                  # RB.SHF / C（外盘用字母代码）
    future_name: str
    board_code: str = ""              # 目标板块代码（可为空，按名字匹配）
    board_name: str = ""
    direction: str = "positive"       # positive=同向 / negative=反向 / auxiliary
    strength: int = 3                 # ★ 1-5
    lead_days: int = 5
    logic: str = ""
    chain: str = ""                   # 产业链分组（用于"产业链级异动"判定）
    kind: FutureKind = FutureKind.DOMESTIC
    calibrated_strength: float | None = None
    calibrated_at: str = ""
    #: **与该品种价格联动最强的个股**（由 `futures_stocks.py` 从本地行情算出来，
    #: 不是 LLM 生成的）。每项形如
    #: `{"code", "name", "correlation", "samples", "in_board"}`。
    #: `in_board=False` 表示这只高相关股不在映射板块内 —— 值得人工看一眼
    #: （可能是映射漏了它，也可能是伪相关）。
    stocks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"future_code": self.future_code,
                "future_name": self.future_name,
                "board_code": self.board_code, "board_name": self.board_name,
                "direction": self.direction, "strength": self.strength,
                "lead_days": self.lead_days, "logic": self.logic,
                "chain": self.chain, "kind": self.kind.value,
                "calibrated_strength": self.calibrated_strength,
                "calibrated_at": self.calibrated_at,
                "stocks": list(self.stocks)}


@dataclass
class FutureSignal:
    """一个期货品种的异动信号集合（需求 5.4）。"""

    code: str
    name: str
    kind: FutureKind = FutureKind.DOMESTIC
    trade_date: str = ""
    close: float | None = None
    change_pct: float | None = None

    ret_5d: float | None = None
    ret_10d: float | None = None
    ret_20d: float | None = None
    z_5d: float | None = None
    oi_change_5d: float | None = None
    term_structure: float | None = None

    #: 0-100 综合异动强度（用于仪表盘排序）
    intensity: float = 0.0
    kinds: list[FutureSignalKind] = field(default_factory=list)
    level: SignalLevel = SignalLevel.NONE
    reasons: list[str] = field(default_factory=list)
    chain: str = ""
    boards: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "kind": self.kind.value,
                "trade_date": self.trade_date, "close": self.close,
                "change_pct": self.change_pct,
                "ret_5d": self.ret_5d, "ret_10d": self.ret_10d,
                "ret_20d": self.ret_20d, "z_5d": self.z_5d,
                "oi_change_5d": self.oi_change_5d,
                "term_structure": self.term_structure,
                "intensity": round(self.intensity, 2),
                "kinds": [item.value for item in self.kinds],
                "level": self.level.value, "level_label": self.level.label,
                "reasons": list(self.reasons), "chain": self.chain,
                "boards": list(self.boards), "gaps": list(self.gaps)}


@dataclass
class FutureAlert:
    """期货先行信号告警（需求 5.5 的告警规则）。"""

    date: str
    code: str
    name: str
    level: SignalLevel
    title: str
    detail: str = ""
    boards: list[str] = field(default_factory=list)
    chain: str = ""
    kinds: list[FutureSignalKind] = field(default_factory=list)

    @property
    def alert_id(self) -> str:
        return f"{self.date}-{self.code}-{self.level.value}"

    def to_dict(self) -> dict[str, Any]:
        return {"alert_id": self.alert_id, "date": self.date, "code": self.code,
                "name": self.name, "level": self.level.value,
                "level_label": self.level.label, "title": self.title,
                "detail": self.detail, "boards": list(self.boards),
                "chain": self.chain,
                "kinds": [item.value for item in self.kinds]}


@dataclass
class FutureDashboard:
    """期货先行信号子视图的完整载荷（需求 5.5）。"""

    generated_at: str = ""
    trade_date: str = ""
    signals: list[FutureSignal] = field(default_factory=list)
    alerts: list[FutureAlert] = field(default_factory=list)
    #: 期股联动热力图矩阵：{future_code: {board_name: correlation}}
    correlation: dict[str, dict[str, float]] = field(default_factory=dict)
    mappings: list[FutureMapping] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    source_notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    disclaimer: str = ""

    def to_dict(self, *, top: int = 89) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "trade_date": self.trade_date,
            "counts": self.counts,
            "signals": [item.to_dict() for item in self.signals[:top]],
            "alerts": [item.to_dict() for item in self.alerts],
            "correlation": self.correlation,
            "mappings": [item.to_dict() for item in self.mappings],
            "source_notes": self.source_notes,
            "gaps": self.gaps,
            "disclaimer": self.disclaimer,
        }


@dataclass
class FuturesBacktestItem:
    """期货先行信号回测的单个品种结果（需求 5.6）。"""

    code: str
    name: str
    kind: FutureKind = FutureKind.DOMESTIC
    chain: str = ""
    signals: int = 0
    hit_rate: float | None = None
    false_positive_rate: float | None = None
    median_lead_days: float | None = None
    median_max_gain: float | None = None
    ic_mean: float | None = None
    boards: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "kind": self.kind.value,
                "chain": self.chain, "signals": self.signals,
                "hit_rate": self.hit_rate,
                "false_positive_rate": self.false_positive_rate,
                "median_lead_days": self.median_lead_days,
                "median_max_gain": self.median_max_gain,
                "ic_mean": self.ic_mean, "boards": list(self.boards)}


__all__ = [
    "AlertSignal",
    "BacktestFold",
    "BacktestMetrics",
    "BacktestReport",
    "BoardBar",
    "BoardFlow",
    "BoardInfo",
    "BoardKind",
    "BoardScore",
    "BoardSeries",
    "DimensionScore",
    "FactorCorrelation",
    "FundPoint",
    "FutureAlert",
    "FutureDashboard",
    "FutureKind",
    "FutureMapping",
    "FutureSignal",
    "FutureSignalKind",
    "FuturesBacktestItem",
    "HolderRow",
    "LayerScore",
    "LeaderInfo",
    "MainlineSnapshot",
    "MarginRow",
    "NorthboundRow",
    "SceneCase",
    "SeatRow",
    "SignalLevel",
    "StockFlow",
]
