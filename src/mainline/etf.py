"""主线挖掘：ETF 资金异动（**最终分加分项** + 提名触发器）。

## 为什么需要这一维：一条真实的漏报引出的

2026 年 6 月底到 7 月，「农业ETF易方达」`562900.SH` 出现**历史级的高成交额**，
多个交易日成交额是它自身中位数的 2 倍以上 —— 场外资金在持续申购进场。
但主线模块对农业板块**完全没有反应**：三个层里没有任何一项在看 ETF。

为什么前两层都看不到它：

    第一层 技术指标/量能   看的是**板块指数自身**的成交量（成员股加总）
    第二层 杠杆资金        看的是**个股**融资余额
    第二层 北向资金        看的是**个股**外资持股
    第二层 量价形态        看的是板块指数的形态

ETF 份额变化是**场外资金申购**进场 —— 与上面三者来源完全不同，是真正的增量信息。
而且它在板块指数还没动的时候就能看到"钱在往里进"，正是本模块要抓的"启动前"。

## 两个信号 → 两级异动（V2.1 修正）

    份额变化（净申购）  Σ 份额_t / Σ 份额_{t-1} - 1     ← 真金白银，但不是每天更新
    成交额放量          成交额_t / 近 N 日均值           ← 每天都有，但会被套利/做市放大

原实现要求**两者同时成立**才算异动。这条耦合在逻辑上就是错的：
"份额没净申购"不等于"没有资金异动" —— 放量可能来自折价套利承接、
也可能来自份额尚未更新。改成两级，**都能触发**，只是加分不同
（`breakout_bonus`）：

    一级 `breakout_level = 1`  历史级放量（绝对倍率 + 自身分位），不要求净申购
    二级 `breakout_level = 2`  一级 + 份额净申购（`share_change > 0`）

单看份额：`fund_share` 在小额申赎时可能几天不变，信号稀疏。
单看成交额：ETF 成交额受折溢价套利驱动，某天翻倍未必有净申购。
所以二级是更强的确认，但一级**不是噪声**，只是置信度低一档。

### ⚠️ 关于农业案例：真正卡住它的是「倍数」门槛，不是份额门槛

`562900.SH` 在启动窗口的**实测原始值**（`scripts/_diag_562900.py`）：

    日期         成交额(元)    近60日中位     倍数   分位   份额变化
    2026-06-26   18,777,797   12,209,500    1.54   93%   +0.87%
    2026-06-29   24,212,906   12,209,500    1.98  100%   -3.01%
    2026-07-01   22,798,484   12,209,500    1.87   98%   -1.80%
    2026-07-06   26,764,013   12,352,407    2.17  100%   +6.31%

⚠️ **06-29 是 1.98 倍，差 1% 没到 `breakout_amount_ratio = 2.0`。**
（早先口头引用过的"2.22 倍 / 2.03 倍"是 `volume`（手数）的倍数，
不是 `amount`（元）—— 两个字段差一个量级，别再混用。）

所以对**这一案**而言：拆两级并没有把 06-26 / 06-29 救回来，真正卡住它们的
是倍数门槛。拆两级的价值是**结构性的**（去掉一个逻辑上不该有的耦合、
把二值判断变成分级响应），不是这一案的解药 —— 分清这点很重要，
否则会误以为"改完了农业就能全报出来"。

要不要放宽 `breakout_amount_ratio`（或在倍数之外补一条"分位 ≥95% 即算"
的旁路）是**标定问题**，应当连同 `bonus_*` 一起由回测绘召回-精度曲线来定，
不该在实现里偷偷调。当前 06-26/06-29 未触发是**已知且有意保留**的行为。

## 口径与陷阱

- **`etf_share_size` 无权限**（实测：5000 积分档未开通），改用 **`fund_share`**
  的 `fd_share`（万份）。两者含义一致（基金份额），只是字段名与单位不同。
- **`fund_share` 是"截至该日的份额"**，不是"当日申购量"。因此份额变化必须
  **和上一交易日比**，不能与同日的别的字段混算。
- **迷你 ETF 的单日异动不是板块信号**：一只 3000 万规模的 ETF 放量 5 倍也只有
  1500 万，噪声远大于信息。因此设 `min_amount` 门槛，并且**按成交额加权聚合**
  到板块 —— 一个板块往往有 5~20 只跟踪同一主题的 ETF，简单平均会被小 ETF 带偏。

## 放在哪里

1. **第二层第四个维度** `etf`（默认权重 15%）；这是使用者明确要求的口径。
2. **提名触发器**：ETF 同时满足"净申购 + 历史级放量"时，即使该板块第一层没进
   候选池，也允许它通过 `_nominate` 进入告警 —— 与龙头共振提名并列的第二条
   "绕过初筛"通道，且同样需要第一层分不低于下限。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.mainline.config import MainlineConfig, load_config, load_yaml_config
from src.mainline.scoring import (
    clamp,
    median,
    percentile_score,
    safe_ratio,
    to_float,
)

logger = logging.getLogger(__name__)

DEFAULT_MAPPING_FILE = "mainline_etf_mapping.yaml"

#: 人工提供的映射表（**优先级高于自动生成的上面那份**）。
#: 由 `scripts/import_user_etf_mapping.py` 从用户 CSV 生成，允许一只 ETF 归多个板块。
MANUAL_MAPPING_FILE = "mainline_etf_mapping_manual.yaml"

#: 异动级别的中文名（面板与 reasons 共用，避免各处自己拼字符串）。
LEVEL_LABELS: dict[int, str] = {
    0: "无异动",
    1: "一级异动（历史级放量）",
    2: "二级异动（放量+净申购）",
}


@dataclass
class EtfInfo:
    """一只 ETF 及其映射到的板块。"""

    code: str
    name: str = ""
    board_name: str = ""
    board_code: str = ""
    keyword: str = ""
    source: str = "name"


@dataclass
class EtfSignal:
    """一个**板块**的 ETF 资金异动信号（多只 ETF 聚合后）。"""

    board_code: str
    board_name: str = ""
    etf_count: int = 0
    #: 板块 ETF 合计份额变化率（净申购，小数）
    share_change: float | None = None
    #: 连续净申购天数
    share_streak: int = 0
    #: 成交额 / 近 N 日均值
    amount_ratio: float | None = None
    #: 当日成交额在**自身**近 N 日分布中的分位（0-1，"历史级高低"的口径）
    amount_percentile: float | None = None
    #: 板块 ETF 合计成交额（元）
    amount: float = 0.0
    #: 0-100 维度分（横截面分位由调用方填；这里先存原始量）
    score: float = 0.0
    #: 是否发生异动（**任一级别**成立即为 True，等价于 `breakout_level >= 1`）
    breakout: bool = False
    #: 异动级别：0=无，1=一级（纯放量），2=二级（放量 + 净申购）。
    #:
    #: 为什么需要分级而不是一个布尔：需求要的是"异常放量、资金异动"，
    #: 但**放量先于净申购**（见模块头的 562900 实测）。一级用来保召回
    #: （启动第一周就能看到），二级用来保精度（两条都成立才是真申购）。
    #: 两者给不同的加分（`breakout_bonus`），而不是"成立/不成立"。
    breakout_level: int = 0
    #: 板块内**任意一只** ETF 单独达到的最高级别（0/1/2）
    max_etf_level: int = 0
    #: 单只口径里最强的"异动强度"（`倍数 × 自身分位`，0~∞）
    #:
    #: 为什么要单独存：聚合口径会被大 ETF 摊薄（农业聚合 1.38× / 65 分位，
    #: 而 562900 自己 2.17× / 100 分位）。提名通道要按"异动有多强"排序，
    #: 若只用聚合值，**恰恰是单只口径触发的那些板块会排到后面** ——
    #: 而它们才是这个口径存在的理由。
    best_etf_strength: float = 0.0
    #: 板块内是否有**任意一只** ETF 单独突破（个股级口径）
    #:
    #: 为什么两个口径都要：一个主题常有十几只 ETF，其中规模最大的那只份额
    #: 几天不动，就会把整个板块的聚合份额变化摊薄到 0 —— 而真正在放量申购的
    #: 往往是中小规模的那只。2026-07 的农业 ETF 就是如此：易方达 562900 自己
    #: 7/06 份额 +6.31%、成交额 2.21 倍，但 4 只农业 ETF 聚合后份额变化是
    #: **+0.0%**，聚合口径完全不响。
    #: 聚合口径更稳（多人同时申购才响），单只口径更灵敏（有人先动就响）。
    any_breakout: bool = False
    #: 触发单只突破的 ETF 明细 `[(code, name, share_change, amount_ratio)]`
    breakout_etfs: list[tuple[str, str, float, float]] = field(
        default_factory=list)
    reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def level_of(self) -> int:
        """板块聚合口径与单只口径，取**较高**的那个级别。

        两个口径都要的理由见 `any_breakout`：聚合口径稳但会被大 ETF 摊薄，
        单只口径灵敏但噪声大。既然现在分了两级，就不必再二选一 ——
        取较高级别，低级别的自然成为"有动静但未确认"。
        """
        return max(int(self.breakout_level), int(self.max_etf_level))

    def strength(self) -> float:
        """异动强度（`放量倍数 × 自身分位`），**两个口径取较大者**。

        必须与 `level_of` 用同一套"取较大者"的逻辑：若只用聚合值排序，
        被大 ETF 摊薄、靠单只口径才触发的板块会排到后面 —— 而它们正是
        最该被这条通道捞出来的（农业就是：聚合 1.38×/65 分位，
        562900 自己 2.17×/100 分位）。
        """
        aggregate = ((self.amount_ratio or 0.0)
                     * (self.amount_percentile or 0.0))
        return max(aggregate, float(self.best_etf_strength))

    def to_dict(self) -> dict[str, Any]:
        return {"board_code": self.board_code, "board_name": self.board_name,
                "etf_count": self.etf_count,
                "share_change": self.share_change,
                "share_streak": self.share_streak,
                "amount_ratio": self.amount_ratio,
                "amount_percentile": self.amount_percentile,
                "amount": self.amount, "score": round(self.score, 2),
                "breakout": self.breakout, "any_breakout": self.any_breakout,
                "breakout_level": self.breakout_level,
                "max_etf_level": self.max_etf_level,
                "best_etf_strength": round(self.best_etf_strength, 4),
                "breakout_etfs": [{"code": c, "name": n,
                                   "share_change": s, "amount_ratio": r}
                                  for c, n, s, r in self.breakout_etfs],
                "reasons": list(self.reasons), "gaps": list(self.gaps)}


@dataclass
class EtfMapping:
    """ETF 名称 → 板块名 的映射表。

    ## 为什么 `overrides` 是 `代码 → [板块]`（一对多）

    人工映射表里同一个主题族会共用一组 ETF —— 实测用户提供的
    `关联etf.csv` 里，`BC电池 / HJT电池 / PET铜箔 / PVDF概念 / TOPCON电池 /
    固态电池 / 钠离子电池 / 锂电池概念 / 储能 …` **14 个板块共用同一组
    4 只电池 ETF**。这是有意的（"这个主题族就用这几只 ETF"），
    不是填表错误。

    一对多的代价必须说清楚：**同一组 ETF 的信号对这 14 个板块完全相同**，
    所以 ETF 加分只能把"这个族"整体抬起来，**分不出族内哪个子概念更强**。
    这是数据本身的粒度限制，不是实现缺陷。
    """

    keywords: list[tuple[str, str]] = field(default_factory=list)
    #: `{ETF 短代码: [板块名, ...]}`（人工映射优先，一个代码可归多个板块）
    overrides: dict[str, list[str]] = field(default_factory=dict)
    min_amount: float = 5_000_000.0
    min_history: int = 30
    loaded: bool = False
    gap: str = ""

    def matches(self, code: str, name: str) -> list[tuple[str, str]]:
        """返回 `[(板块名, 命中的依据), ...]`；未命中返回空列表。

        关键字路径（`keywords`）仍然**取最长命中、只归一个板块** ——
        关键字是"名字里有某词"的弱证据，一条 ETF 同时命中多个关键字时
        说明这些关键字都太宽，取最长是唯一可解释的裁决。
        显式清单（`overrides`）是人工确认过的强证据，允许一对多。
        """
        text = str(name or "")
        short = str(code or "").split(".")[0]
        raw = self.overrides.get(short)
        # ⚠️ 容错：值若是 `str` 而不是 `list`，`for board in raw` 会**逐字符迭代**
        # —— 把 `"农业种植"` 拆成 `农`/`业`/`种`/`植` 四个假板块，
        # 而且不报错，只是所有信号都归到不存在的板块上（然后被静默跳过）。
        # 实测这个坑在改一对多时被测试用例触发过，所以在这里挡住。
        boards = [raw] if isinstance(raw, str) else list(raw or [])
        if boards:
            return [(board, "override") for board in boards]
        best: tuple[int, str, str] | None = None
        for keyword, board in self.keywords:
            if keyword and keyword in text:
                if best is None or len(keyword) > best[0]:
                    best = (len(keyword), board, keyword)
        if best is None:
            return []
        return [(best[1], best[2])]

    def match(self, code: str, name: str) -> tuple[str, str]:
        """返回**第一个** `(板块名, 命中的关键字)`；未命中返回 `("", "")`。

        保留单数接口是为了兼容既有调用与测试。新代码请用 `matches`
        —— 一对多时只取第一个会静默丢掉其余板块。
        """
        found = self.matches(code, name)
        return found[0] if found else ("", "")


def load_mapping(config: MainlineConfig | None = None) -> EtfMapping:
    """加载 ETF↔板块映射表（缺失时返回空表 + gap，不抛错）。

    ## 两份表，人工优先

    1. `configs/mainline_etf_mapping.yaml` —— **自动生成**（`build_etf_mapping.py`），
       每板块按规模取前 N 只，覆盖面窄但可复现。
    2. `configs/mainline_etf_mapping_manual.yaml` —— **人工提供**
       （`import_user_etf_mapping.py`），覆盖面广且允许一对多。

    合并规则：**人工表的每只 ETF 完全覆盖自动表对它的归属**（不是追加）——
    否则同一只 ETF 会同时算到两个板块上，而 `overrides` 的语义是
    "这只 ETF 代表哪个板块"。人工没提到的代码才用自动表的结论补缺。
    """
    cfg = config or load_config()
    name = getattr(cfg, "etf", None)
    path = getattr(name, "mapping_file", DEFAULT_MAPPING_FILE)
    raw = load_yaml_config(path)
    if raw is None:
        return EtfMapping(loaded=False, gap=f"ETF 映射表不可用：configs/{path}")
    out = EtfMapping(loaded=True)
    for item in raw.get("overrides") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            short = str(item[0]).split(".")[0]
            board = str(item[1])
            slot = out.overrides.setdefault(short, [])
            if board not in slot:
                slot.append(board)
    manual = load_yaml_config(MANUAL_MAPPING_FILE)
    if manual:
        wanted: dict[str, list[str]] = {}
        for item in manual.get("overrides") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                short = str(item[0]).split(".")[0]
                board = str(item[1])
                slot = wanted.setdefault(short, [])
                if board not in slot:
                    slot.append(board)
        # 人工表**整只 ETF 覆盖**自动表（不是追加）：同一只 ETF 若两边都有，
        # 自动表的结论作废 —— 否则它会同时算到两个板块上。
        # 但人工表内部允许一对多（主题族共用一组 ETF）。
        for short, boards in wanted.items():
            out.overrides[short] = boards
    for item in raw.get("keywords") or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.keywords.append((str(item[0]), str(item[1])))
    filters = raw.get("filters") or {}
    out.min_amount = float(to_float(filters.get("min_amount"),
                                    out.min_amount) or out.min_amount)
    out.min_history = int(to_float(filters.get("min_history"),
                                   out.min_history) or out.min_history)
    # ⚠️ 判据是"**两种依据都没有**"而不是"keywords 为空"：
    # V2 的映射表以**显式代码清单**（`overrides`）为主（需求是"每板块最多
    # 留 N 只"，这是全局约束，关键字表达不了）。只查 keywords 会让一份
    # 完全有效的 overrides-only 表被报成"缺 keywords 段"。
    if not out.keywords and not out.overrides:
        out.gap = (f"ETF 映射表既没有 keywords 也没有 overrides：configs/{path}")
    return out


# ==================================================================
# 信号计算
# ==================================================================


def board_etf_signals(bars: dict[str, list[dict[str, float]]],
                      mapping: EtfMapping, *,
                      board_by_name: dict[str, str],
                      window: int = 60,
                      breakout_amount_ratio: float = 2.0,
                      breakout_percentile: float = 0.90
                      ) -> dict[str, EtfSignal]:
    """把多只 ETF 聚合成**板块级**信号。

    `bars` 是 `{etf_code: [{trade_date, close, amount, shares}, ...]}`（升序）。
    `board_by_name` 是 `{板块名: 板块代码}`（来自本地板块名录，
    用它把映射表里的板块名钉到真实板块上；名字对不上的直接跳过并记 gap）。

    ## 聚合为什么"按成交额加权"而不是等权

    一个主题往往有十几只 ETF（农业就有 20+ 只），其中多数是几千万规模的迷你
    ETF，单日净申购金额小、噪声大。等权平均会让这些噪声票主导板块信号；
    按成交额加权则让"真有大资金在里面"的那几只有话语权。
    """
    buckets: dict[str, dict[str, Any]] = {}
    unmatched: dict[str, int] = {}
    for code, rows in bars.items():
        if not rows:
            continue
        name = str(rows[0].get("name") or "")
        # ⚠️ 用 `matches` 而不是 `match`：人工映射允许一只 ETF 归多个板块
        # （主题族共用一组 ETF），只取第一个会静默丢掉其余板块。
        hits = mapping.matches(code, name)
        if not hits:
            continue
        if len(rows) < max(int(mapping.min_history), 5):
            continue
        latest_amount = to_float(rows[-1].get("amount")) or 0.0
        if latest_amount < mapping.min_amount:
            continue
        for board_name, keyword in hits:
            board_code = board_by_name.get(board_name)
            if not board_code:
                unmatched[board_name] = unmatched.get(board_name, 0) + 1
                continue
            slot = buckets.setdefault(board_code, {"board_name": board_name,
                                                   "items": []})
            slot["items"].append((code, rows, keyword))

    out: dict[str, EtfSignal] = {}
    for board_code, slot in buckets.items():
        signal = _aggregate(board_code, slot["board_name"], slot["items"],
                            window=window,
                            breakout_amount_ratio=breakout_amount_ratio,
                            breakout_percentile=breakout_percentile)
        if signal is not None:
            out[board_code] = signal
    if unmatched:
        logger.info("ETF 映射到不存在的板块（已跳过）：%s",
                    "、".join(sorted(unmatched)[:5]))
    return out


def _aggregate(board_code: str, board_name: str,
               items: Sequence[tuple[str, list[dict[str, float]], str]], *,
               window: int, breakout_amount_ratio: float,
               breakout_percentile: float) -> EtfSignal | None:
    """把同一板块下的多只 ETF 聚合成一个信号。"""
    signal = EtfSignal(board_code=board_code, board_name=board_name,
                       etf_count=len(items))
    total_amount = 0.0
    share_now = 0.0
    share_prev = 0.0
    ratios: list[tuple[float, float]] = []      # (成交额, 放量倍数)
    streaks: list[int] = []
    percentiles: list[float] = []
    per_etf: list[tuple[str, str, float, float]] = []   # 单只口径的异动明细
    per_levels: list[int] = []                          # 与 per_etf 一一对应的级别
    best_etf_strength = 0.0                             # 单只口径的最强强度
    for code, rows, _keyword in items:
        amount = to_float(rows[-1].get("amount")) or 0.0
        total_amount += amount
        # 份额：当日 / 上一交易日（`fund_share` 是"截至该日的份额"，
        # 不是当日申购量，因此必须与上一日比）
        now = to_float(rows[-1].get("shares"))
        prev = to_float(rows[-2].get("shares")) if len(rows) >= 2 else None
        if now is not None and prev is not None:
            share_now += now
            share_prev += prev
        # 成交额放量倍数
        history = [to_float(row.get("amount")) or 0.0 for row in rows[-(window + 1):-1]]
        typical = median(history)
        if typical and typical > 0:
            ratios.append((amount, amount / typical))
        # 当日成交额在自身近 N 日分布中的分位（"历史级"的判定口径）
        rank = percentile_score(history, amount) if len(history) >= 20 else None
        if rank is not None:
            percentiles.append(rank / 100.0)
        # 连续净申购天数
        shares = [to_float(row.get("shares")) for row in rows[-(window + 1):]]
        clean = [item for item in shares if item is not None]
        streak = 0
        for index in range(len(clean) - 1, 0, -1):
            if clean[index] > clean[index - 1]:
                streak += 1
            else:
                break
        streaks.append(streak)
        # 单只 ETF 的异动判定（与聚合口径同一套阈值，但分红两级）
        if typical:
            one_ratio = amount / typical
            one_level = 0
            if (one_ratio >= breakout_amount_ratio
                    and (rank or 0.0) >= breakout_percentile * 100.0):
                # 一级只需放量；二级再要求份额净申购。
                # ⚠️ 份额缺失时按**一级**算，不按 0 算 —— "没数据"不等于
                # "没申购"，把它降级成 0 就等于用缺失值当反证。
                one_share = (now / prev - 1.0
                             if now is not None and prev and prev > 0 else None)
                one_level = 2 if (one_share is not None and one_share > 0) else 1
                per_etf.append((code, str(rows[0].get("name") or ""),
                                one_share if one_share is not None else 0.0,
                                one_ratio))
                per_levels.append(one_level)
                # 单只口径的强度：倍数 × 自身分位（与聚合口径同量纲）
                one_strength = one_ratio * ((rank or 0.0) / 100.0)
                best_etf_strength = max(best_etf_strength, one_strength)

    signal.amount = total_amount
    signal.share_change = (safe_ratio(share_now - share_prev, share_prev)
                           if share_prev > 0 else None)
    signal.share_streak = max(streaks) if streaks else 0
    if ratios:
        weight = sum(item[0] for item in ratios) or 1.0
        signal.amount_ratio = sum(amount * ratio for amount, ratio in ratios) / weight
    signal.amount_percentile = (sum(percentiles) / len(percentiles)
                                if percentiles else None)

    # 两级异动：一级 = 历史级放量；二级 = 一级 + 份额净申购。
    # 放量先于净申购（见模块头的 562900 实测），所以一级不是噪声而是
    # "更早但置信度低一档"的信号，两者给不同加分。
    level1 = (signal.amount_ratio is not None
              and signal.amount_ratio >= breakout_amount_ratio
              and (signal.amount_percentile or 0.0) >= breakout_percentile)
    level2 = (level1 and signal.share_change is not None
              and signal.share_change > 0)
    signal.breakout_level = 2 if level2 else (1 if level1 else 0)
    signal.breakout = signal.breakout_level > 0
    signal.any_breakout = bool(per_etf)
    signal.max_etf_level = max(per_levels) if per_levels else 0
    signal.best_etf_strength = best_etf_strength
    signal.breakout_etfs = per_etf[:5]
    if per_etf and not signal.breakout:
        # 单只异动而聚合没异动：明确写出来，否则使用者只会看到"ETF 维度分不高"
        # 而不知道"其实已经有一只 ETF 在放量了"
        best_index = max(range(len(per_etf)),
                         key=lambda i: per_etf[i][2] * per_etf[i][3])
        best = per_etf[best_index]
        best_level = per_levels[best_index] if best_index < len(per_levels) else 1
        signal.reasons.append(
            f"单只 ETF {LEVEL_LABELS[best_level]}：{best[1] or best[0]}"
            f" 份额 {best[2] * 100:+.2f}%、成交额 {best[3]:.2f} 倍"
            "（板块聚合未达门槛）")
    if signal.share_change is not None:
        signal.reasons.append(
            f"{signal.etf_count} 只 ETF 合计份额 "
            f"{signal.share_change * 100:+.2f}%"
            + (f"（连续净申购 {signal.share_streak} 天）"
               if signal.share_streak else ""))
    if signal.amount_ratio is not None:
        signal.reasons.append(
            f"成交额放大 {signal.amount_ratio:.2f} 倍"
            + (f"（近 60 日 {signal.amount_percentile * 100:.0f}% 分位）"
               if signal.amount_percentile is not None else ""))
    if signal.breakout:
        signal.reasons.insert(
            0, f"ETF {LEVEL_LABELS[signal.breakout_level]}："
               + ("历史级放量 + 份额净申购" if signal.breakout_level >= 2
                  else "历史级放量（份额尚未体现净申购）"))
    if signal.share_change is None:
        signal.gaps.append("ETF 份额序列为空（fund_share 未同步或该 ETF 无份额数据）")
    return signal


def breakout_bonus(signal: EtfSignal | None, *, config: MainlineConfig) -> float:
    """ETF 异动对**最终分**的加分（不占任何权重）。

    ## 为什么加分而不是权重

    实测：分析池 324 个概念板块里，能映射到 ETF 的只有 22~35 个
    （取决于行情同步范围）。一个在 90% 样本上不存在的因子占加权平均的 15%，
    等于**从其他维度身上偷权重**：对少数有 ETF 的板块它顶掉别人 15% 的话语权，
    对其余板块它什么也不做 —— 同一个名义权重在不同板块上含义不同，分数不可比。

    加分的语义才对得上数据：ETF 异动是一个**条件事件**（"这个板块出事了"），
    不是一条连续轴。这与第三层龙头共振完全同类，而模块已经把它建模成
    「门控加分 0~20，不占权重」。这里沿用同一范式。

    ## 三个加分项

        level1      历史级放量（不要求净申购）      → `bonus_level1`
        level2      放量 + 份额净申购               → `bonus_level2`（取代 level1）
        resonance   板块内 ≥2 只 ETF 同时异动        → 再叠加 `bonus_resonance`

    **具体分值是可调参数**（`config.etf.bonus_*`），不写死在这里：
    它们直接决定召回率与准确率的取舍，应当由回测/事件复盘来标定，
    而不是由实现者拍一个"看起来合理"的数。

    封顶 `bonus_cap`，避免加分本身变成主导项。
    """
    cfg = config.etf
    if signal is None or not cfg.bonus_enabled:
        return 0.0
    level = signal.level_of()
    if level <= 0:
        return 0.0
    bonus = float(cfg.bonus_level2 if level >= 2 else cfg.bonus_level1)
    if signal.etf_count >= 2 and level >= 2:
        # 共振只在二级上加：多只 ETF 一起放量但都在净赎回，更像集体折价套利，
        # 不足以说明"场外资金在申购"。
        bonus += float(cfg.bonus_resonance)
    return max(0.0, min(float(cfg.bonus_cap), bonus))



def score_etf(signals: dict[str, EtfSignal], *,
              codes: Sequence[str] | None = None) -> dict[str, float]:
    """在**给定板块集合内**把 ETF 原始信号折成 0-100 分（横截面分位）。

    三个子因子（与 `accumulation.SUB_WEIGHTS["etf"]` 对应）：

        share_change  45%   份额净申购率（越大越好）
        amount_ratio  35%   成交额放量倍数
        streak        20%   连续净申购天数（0-3 天映射到 0-100）

    为什么用横截面分位而不是绝对阈值：ETF 份额变化率的分布随市场整体
    申赎强度漂移（牛市里 +2% 排不进前 50）。分位口径与其余维度一致。
    """
    target = list(codes) if codes is not None else list(signals)
    share_col = [signals[code].share_change for code in target if code in signals]
    ratio_col = [signals[code].amount_ratio for code in target if code in signals]
    out: dict[str, float] = {}
    for code in target:
        signal = signals.get(code)
        if signal is None:
            continue
        parts: list[tuple[float, float, bool]] = []
        share_pct = percentile_score(share_col, signal.share_change)
        parts.append((share_pct or 0.0, 0.45, share_pct is not None))
        ratio_pct = percentile_score(ratio_col, signal.amount_ratio)
        parts.append((ratio_pct or 0.0, 0.35, ratio_pct is not None))
        streak = min(100.0, signal.share_streak / 3.0 * 100.0) \
            if signal.share_change is not None else 0.0
        parts.append((streak, 0.20, signal.share_change is not None))
        usable = sum(weight for _, weight, ok in parts if ok)
        total = sum(score * weight for score, weight, ok in parts if ok)
        score = (total / usable) if usable > 0 else 0.0
        # 异动确认给一个下限：横截面分位在"全市场都没申赎"时会集体偏低，
        # 而 breakout 是绝对口径的确认，不该被相对分位压掉。
        if signal.breakout:
            score = max(score, 75.0)
        signal.score = clamp(score)
        out[code] = signal.score
    return out


def ensure_columns(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """补齐 `fund_daily` / `fund_share` 合并时的缺列（测试与降级用）。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({"trade_date": str(row.get("trade_date") or ""),
                    "close": to_float(row.get("close")) or 0.0,
                    "amount": to_float(row.get("amount")) or 0.0,
                    "shares": to_float(row.get("shares")),
                    "name": str(row.get("name") or "")})
    return out


__all__ = [
    "DEFAULT_MAPPING_FILE",
    "MANUAL_MAPPING_FILE",
    "EtfInfo",
    "EtfMapping",
    "EtfSignal",
    "board_etf_signals",
    "breakout_bonus",
    "ensure_columns",
    "LEVEL_LABELS",
    "load_mapping",
    "score_etf",
]
