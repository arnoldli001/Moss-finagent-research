"""主线挖掘：配置加载（`configs/mainline.yaml`，按 mtime 热重载）。

## 为什么配置必须与代码分离

这个模块的阈值都是**研究口径**而不是工程常量：`strong_score=70` 还是 `68`、
IC 窗口取 120 还是 250，属于「今天觉得合适、明天要回测对比」的东西。
写死在代码里就意味着每次调参都要改代码 + 重启服务 + 重跑回测，
参数实验的成本高到没人愿意做。

因此 `configs/mainline.yaml` 是唯一入口，本模块只负责把它读成带默认值的
dataclass。**YAML 缺字段一律回落默认值**（而不是抛错）—— 用户的配置文件
通常只写想改的那几项，缺字段抛错会让"只改一个阈值"变得不可能。

## V2.0 的漏斗式结构（与 V1.0 的最大区别）

    synthesis.layer_weights = {six_dim: 80, accumulation: 20}   # V2.4 起
    最终分 = 第一层×80% + 第二层×20% + 第三层门控加分(0~20)

**第三层不再占权重**：它是 `gate_bonus`，只在龙头共振触发时加分。
V1.0 的三层加权相加（六维40/五维35/龙头25）会把资金流、股东户数、成交量
因子重复加权 2-3 倍 —— 回测胜率虚高、实盘失效，这是 V2.0 要修掉的核心问题。

**层权重的演进（每一步的原因都在文档里，别只看结果）**：

| 版本 | 权重 | 当时的理由 | 后来发现 |
|---|---|---|---|
| V2.0 | 50/50 | 两层等权 | 名义权重：池内 sd 差 3 倍，实际约 26/74 |
| V2.3 | 100/0 | 两窗口四格判据"单调更优" | 加第三个窗口后不成立 |
| **V2.4** | **80/20** | 六格（3 窗口×2 持有期）最差格最好 | — |

**V2.3→V2.4 的真正原因不是"换个数试试"**：`service.py` 合成两层时
**没有检查层的 `available`**，而层没数据时 `weighted_score` 返回
`score = 0`（"没有数据"的表示就是 0 分），于是缺第二层的板块分数被**腰斩**
（旧窗口超过一成候选板块，单日实测 47/59）。`100/0` 之所以"看起来好"，
是因为把出问题的层权重设成了 0 —— **绕过了 bug**。
修好（只在有数据的层之间平均）之后，`100/0` 在六格的最差超额与平均 IC
上都垫底。详见 `docs/MAINLINE_MINING.md` §16.34 / §16.38 / §16.39。

第二层因此从五维缩到**三维**（`accumulation.DIMS`）：

    leverage 35%      杠杆资金（融资余额）—— 第一层未覆盖
    northbound 35%    北向资金            —— 第一层未覆盖
    volume_price 30%  量价形态（仅形态识别）—— 与第一层技术指标部分重叠

删除的两个维度及理由（写在这里避免后人"补回来"）：

    ~~capital_strength 主力资金强度~~ → 第一层 moneyflow 已覆盖
    ~~chip_concentration 筹码集中~~   → 第一层 chips 已覆盖

## 三个数字口径（最容易搞错的地方）

- `*_score` / `*_trigger` 是 **0-100 分**上的阈值（最终预警分、子模型分）；
- `*_weight` 是**百分比**（漏斗两层合计 100、六维合计 100、三维合计 100）；
- `*_ratio` 是 **0-1 小数**（如 `candidate_ratio=0.20`、`resonance_ratio=0.30`）。

把权重当成分数会得到"永远不到阈值"的结论，把 ratio 当成百分数会让候选池
变成全市场。

## 热重载语义

`load_config()` 每次检查文件 mtime，变了就重新解析并换掉缓存对象 ——
与 `src/intraday/service.py` 的 YAML overrides 同一套做法。
进程内**不要**长期持有返回的对象引用（它可能被下一次调用替换掉）。
"""

from __future__ import annotations

import calendar
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import store_rel as _store_rel  # noqa: E402

#: 仓库根（configs/ 与 src/ 同级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 配置路径（环境变量可覆盖，便于测试）
CONFIG_PATH = Path(os.environ.get(
    "MOSS_MAINLINE_CONFIG", PROJECT_ROOT / "configs" / "mainline.yaml"))


# ==================================================================
# 一、漏斗合成
# ==================================================================


@dataclass
class SynthesisConfig:
    """漏斗式合成：第二层的权重由 `layer_weights` 决定（V2.3 起为 100/0），
    第三层只做门控加分。"""

    #: 两层权重（百分比，自动归一化到合计 100）。键名固定，改比例只改数值。
    #: ⚠️ 两层分数**不在同一尺度**上（池内 sd 差约 3 倍），所以"50/50"是名义的；
    #: 要真正等权必须先标准化，见 `scripts/layer_weight_sim.py`。
    layer_weights: dict[str, float] = field(default_factory=lambda: {
        "six_dim": 80.0, "accumulation": 20.0})
    #: 第三层龙头共振触发的门控加分区间（0-100 分制上的加分）
    gate_bonus_min: float = 15.0
    gate_bonus_max: float = 20.0

    #: 动态权重：每 N 个交易日按过去 M 日滚动 IC/ICIR 重算
    rebalance_days: int = 20
    ic_window_days: int = 120
    #: ICIR 加权下限：ICIR 低于它的维度不参与加权（负 ICIR 会把权重算成负数）
    min_icir: float = 0.02
    #: 单一维度权重上限（百分比）：防某个维度 ICIR 偶然极高时独占权重
    max_single_weight: float = 70.0
    #: 回退机制：动态权重的验证期 IC 低于等权时，自动回退等权
    fallback_to_equal: bool = True

    DIMS = ("six_dim", "accumulation")

    def normalized_layers(self) -> dict[str, float]:
        """两层权重归一化到合计 100（防用户把两项写成合计 90 或 110）。"""
        return _normalize(self.layer_weights, self.DIMS)


@dataclass
class FunnelConfig:
    """漏斗的**闸门**：每层放多少板块进下一层。

    这两个数字直接决定告警数量。`candidate_ratio` 从 0.20 放到 0.30，
    第二层要算的板块从 60 个变成 90 个（成本 +50%），而精选名单通常不变 ——
    所以它是"用算力换召回"的旋钮，不是越大越好。
    """

    candidate_ratio: float = 0.20      # 第一层 → 候选池（前 20%）
    candidate_min: int = 8             # 板块很少时（如只放开申万 31 个）也要有池子
    candidate_max: int = 80            # 上限：防止板块目录异常膨胀时把第二层拖死
    select_top: int = 10               # 第二层 → 精选名单（前 5-10 个）
    select_min: int = 5

    def candidates(self, total: int) -> int:
        """按板块总数算候选池大小（取比例并夹在 [min, max] 内）。"""
        if total <= 0:
            return 0
        raw = int(round(total * max(0.0, min(1.0, self.candidate_ratio))))
        return max(min(self.candidate_min, total), min(raw, self.candidate_max,
                                                       total))

    def selected(self, total: int) -> int:
        """按候选池大小算精选名单大小。"""
        if total <= 0:
            return 0
        return max(min(self.select_min, total), min(self.select_top, total))


# ==================================================================
# 二、第一层：开源六维基座
# ==================================================================


@dataclass
class SixDimConfig:
    """第一层：开源六维基座（全市场初筛）。"""

    weights: dict[str, float] = field(default_factory=lambda: {
        "trading": 20.0, "prosperity": 30.0, "moneyflow": 20.0,
        "chips": 10.0, "macro": 5.0, "technical": 15.0})
    windows: dict[str, int] = field(default_factory=lambda: {
        "momentum": 20, "overnight": 20, "prosperity": 250,
        "moneyflow": 5, "chips": 250, "technical": 60})

    #: 需要**反向**参与层合成的维度（取 `100 - score`）。
    #:
    #: ## 依据（实测，`scripts/factor_ic_report.py`，415 天 × 332 板块）
    #:
    #: `trading`（量能）与 `technical`（均线/RSI/量能比）是**动量类**因子，
    #: 而它们的 IC 在 5/10/20/60 四个持有期上**单调为负**：
    #:
    #:     维度          H=5      H=10     H=20     H=60
    #:     trading     -0.041   -0.056   -0.077   -0.109
    #:     technical   -0.036   -0.064   -0.082   -0.079
    #:
    #: 即"量能与技术形态越强、后续表现越差" —— A 股概念板块在 20~60 日尺度上
    #: **均值回归**。买入"刚涨过的板块"系统性跑输。
    #:
    #: 离线实测（在已落库的维度分上合成，无需重打分）：
    #:
    #:     现行六维                H=20 IC -0.0375   IC>0 47%
    #:     砍动量（权重给别人）        H=20 IC +0.0077   IC>0 52%
    #:     反转动量（取 100-score）    H=20 IC +0.0694   IC>0 57%
    #:                                  H=60 IC +0.0821   IC>0 68%
    #:
    #: **反转比砍掉好约 9 倍**，因为它保留了信号的信息量。
    #:
    #: ⚠️ `weighted_score` 会把 `weight <= 0` 的项直接丢掉，所以"负权重"
    #: 在这套实现里表达不了 —— 必须走 `reverse_dims` 显式反转。
    #:
    #: ⚠️ 这是**同一样本内的权重搜索**的结果，上实盘前必须做样本外确认
    #: （Purged Walk-Forward，见 `docs/MAINLINE_MINING.md` §16.12 的局限说明）。
    reverse_dims: tuple[str, ...] = ("trading", "technical")

    #: 景气度**低于**这个分（压制前的水平值分）时，改用「增速变化率」口径。
    #:
    #: ## 为什么阈值放在配置里，而 floor 放在代码里
    #:
    #: `PROSPERITY_FLOOR` 改的是"分数怎么映射"，属于模型定义，所以进代码；
    #: 这个阈值改的是"**哪些板块**走另一条口径"，属于"今天觉得合适、明天要
    #: 对比"的取舍 —— 按本项目的既有分工，它该在 YAML 里。
    #:
    #: ## 为什么是 30
    #:
    #: 实测（`docs/MAINLINE_COAL_SPECIAL_CASE.md`，候选层面、只依赖六维排名）：
    #: 低景气组 = 景气度全市场分位 < 30 的 **29 个板块**，它们的进池比例中位
    #: 只有 2%（分位 > 70 的 41 个板块是 53%）。只给这 29 个换变化率：
    #:
    #:     范围          煤炭进池  低景气组进池  留出 FP/TP  留出召回
    #:     不换              3%         2%         6.30        8%
    #:     29 个（本设定）   30%        18%         6.20 ✅     9% ✅
    #:     全部 321 个       28%        17%         8.44 ❌     7%
    #:
    #: 全局换在留出窗口明显变差（候选池每天固定 64 个名额，作用范围越大、
    #: 被挤掉的真信号越多），所以必须限定范围。
    #:
    #: 设为 0 或负数 = 关闭该功能（全部走水平值）。
    prosperity_delta_below: float = 30.0
    #: 增速变化率回看的交易日数（`Δg = g(t) − g(t−W)`）。60 ≈ 一个季度，
    #: 与财报的更新节奏对齐。
    prosperity_delta_window: int = 60

    DIMS = ("trading", "prosperity", "moneyflow", "chips", "macro", "technical")

    def normalized_weights(self) -> dict[str, float]:
        return _normalize(self.weights, self.DIMS)


@dataclass
class ChipConfig:
    """第一层筹码结构维度的参数（V2.0 起只在这里用，第二层不再重复）。"""

    #: 获利盘占比的回看窗口（交易日）—— 用成交量加权价格分布估算
    profit_ratio_window: int = 250
    #: 股东户数环比下降超过该比例视为筹码集中
    holder_drop_threshold: float = -0.10
    #: 股东户数数据的陈旧阈值（天）：超过则打折
    staleness_days: int = 90
    staleness_penalty: float = 0.5


@dataclass
class MacroConfig:
    """第一层宏观驱动维度的参数。"""

    #: 宏观指标回看窗口（月）
    window_months: int = 36
    #: 行业对宏观因子的敏感度映射文件名（configs/ 下）
    sensitivity_file: str = "mainline_macro_sensitivity.yaml"
    #: 宏观状态得分对全部板块的"共同项"权重（0=完全不用，1=完全由宏观决定）
    #: 设上限是因为宏观对板块是**共同项**，不加约束会让所有板块分数趋同。
    common_weight: float = 0.5


# ==================================================================
# 三、第二层：三维建仓痕迹
# ==================================================================


@dataclass
class LeverageConfig:
    """杠杆资金（融资余额）。"""

    change_window: int = 5              # 融资余额 5 日变化率
    top_rank: int = 5                   # 进入全市场前 N 名
    consecutive_days: int = 5           # 连续 N 日
    low_position_window: int = 60       # 板块指数近 N 日低位区域
    low_position_quantile: float = 0.35  # 位于区间下 35% 视为低位


@dataclass
class NorthboundConfig:
    """北向资金。

    ⚠️ **数据现实**（2026-09 实测，别再重复踩）：

    - 交易所自 2024-08 起停止披露北向**日度个股净买入**，只剩沪深股通汇总
      （`moneyflow_hsgt`），而汇总是**没有个股归属**的；
    - 个股**持股**走 `hk_hold(trade_date=...)`：**2022-01 ~ 2024-08-16 是日频**
      （单日 3000~4100 只），之后只在**季末**披露快照；
    - 非季末日的 `hk_hold` 调用**不会返回空，而是返回港股** —— 取数层已按
      `.SH/.SZ/.BJ` 过滤，否则会静默污染整列。

    因此本维度走「**持股数量变化**」而不是「净买入」：

        板块北向净买入(代理) = Σ 成分股 Δ持股数 × 当期均价
                                （Δ 小于 0 时即净卖出）

    这条路径在 2024-08 之后自动退化为"季度变化"，并在 `gaps` 里如实标注，
    而不是拿 0 冒充"没有北向动作"。
    """

    consecutive_days: int = 3             # 连续净买入天数门槛
    min_ratio_to_float_mv: float = 0.001  # 累计金额占板块流通市值比例门槛（0.1%）
    #: 持股变化折算净买入金额时用的价格窗口（交易日）
    price_window: int = 5


@dataclass
class VolumePriceConfig:
    """量价形态（**只保留形态识别，不用原始成交量因子**）。

    V2.0 明确：通用均线排列/RSI/成交量变化已在第一层 technical 覆盖，
    这里只做"地量后放量"与"底分型反包"两种**形态**判定。
    """

    new_low_window: int = 60            # 创近 N 日新低（地量判定的前提）
    shrink_ratio: float = 0.5           # 缩量至均量的比例
    shrink_window: int = 60             # 均量回看窗口
    expand_ratio: float = 1.2           # 放量至均量的比例
    expand_min_days: int = 3
    expand_max_days: int = 5
    max_rise_pct: float = 5.0           # 「温和上行」期间累计涨幅上限（%）


@dataclass
class EtfConfig:
    """ETF 资金异动（第二层第四个维度 + 提名触发器）。

    ## 为什么加这一维

    2026 年 6 月底到 7 月，「农业ETF易方达」`562900.SH` 多次出现历史级成交额，
    场外资金在持续申购进场，但主线模块对农业**完全没有反应** —— 三个层里
    没有任何一项在看 ETF：

        第一层 技术/量能    看板块指数自身的成交（成员股加总）
        第二层 杠杆/北向    看个股的融资余额与外资持股
        第二层 量价形态    看板块指数的形态

    ETF 份额变化是**场外资金申购**，与上面三者来源完全不同，是真正的增量信息；
    而且它在板块指数还没动的时候就能看到"钱在往里进"，正是"启动前"。
    """

    enabled: bool = True
    mapping_file: str = "mainline_etf_mapping.yaml"
    #: 成交额放量倍数的回看窗口（交易日）
    window: int = 60
    #: "放量"的倍数门槛。
    #:
    #: ⚠️ **1.5 是按一个具体事件定的，不是标定结果。**
    #: 农业 `562900` 在启动窗口的 `amount` 倍数是 1.54 / 1.98 / 1.87 / 2.17。
    #: 原门槛 2.0 把 06-26（1.54）到 07-01（1.87）整段挡在外面 ——
    #: 而那正是板块启动的第一周。全历史扫描（`scripts/etf_threshold_scan.py`）：
    #:
    #:     门槛   板块-日   相对 1.6   农业真值召回
    #:     1.40    4483      1.07x     4/4
    #:     1.50    4333      1.04x     4/4   ← 选它
    #:     1.60    4171      1.00x     3/4
    #:     2.00    3296      0.79x     2/4
    #:
    #: 1.5 相对 1.6 只多 4% 信号量，却多召回一天，是明显的拐点。
    #:
    #: **同时否掉了"分位 ≥95% 即算"的旁路**：它更少（0.73x）也更差（3/4），
    #: 说明分位才是两条条件里更紧的那条 —— 该松的是倍数，不是分位。
    #: 详见 `docs/MAINLINE_MINING.md` §9.3.1 / §9.3.2。
    breakout_amount_ratio: float = 1.5
    #: 当日成交额在**自身**历史分布中的分位门槛（0.9 = 近 60 日 90 分位以上）
    breakout_percentile: float = 0.90
    #: 历史回填长度（交易日）
    history_days: int = 400
    #: ETF 异动是否可以作为**提名**触发（与龙头共振提名并列的第二条入口）
    nominate_on_breakout: bool = True

    # ---------- 加分（不占权重）----------
    #
    # 为什么是加分而不是第二层的一个权重：分析池 324 个概念板块里能映射到
    # ETF 的只有 22~35 个，一个在 90% 样本上不存在的因子占加权平均的 15%，
    # 等于从其他维度身上偷权重（详见 `etf.breakout_bonus` 的说明）。
    #
    # ⚠️ **下面四个数是可调参数，不是标定结果。** 它们直接决定召回率与
    # 准确率的取舍，应当由回测/事件复盘来定，而不是实现时拍一个"看起来
    # 合理"的数。改这里就能调，不需要动代码。
    bonus_enabled: bool = True
    #: 一级异动（历史级放量，不要求净申购）的加分
    bonus_level1: float = 6.0
    #: 二级异动（放量 + 份额净申购）的加分，**取代**一级
    bonus_level2: float = 12.0
    #: 多只 ETF 同时二级异动的额外加分
    bonus_resonance: float = 3.0
    #: 加分封顶（与第三层门控加分 0~20 同量级，避免加分变成主导项）
    bonus_cap: float = 20.0


@dataclass
class AccumulationConfig:
    """第二层：三维建仓痕迹（子权重合计 100）。

    ## 为什么从四维退回三维，ETF 改成加分

    V2.0 定的是三维（杠杆 35 / 北向 35 / 量价 30）。加入 `etf` 之后一度改成
    **30 / 30 / 25 / 15**，理由是 ETF 份额衡量**场外资金申购**，与原有三维
    来源不重叠，满足"每层只处理上一层未覆盖的增量信息"的去重原则。
    这个理由本身成立，但**加权平均这种聚合方式用错了**：

        实测（`scripts/_diag_etf_coverage.py` + `build_etf_mapping.py`）
        分析池 324 个概念板块 → 能映射到 ETF 的只有 22~35 个（7%~11%）

    一个在 90% 样本上不存在的因子占加权平均的 15%，实际效果不是"补充信息"
    而是**从别的维度身上偷权重**：对有 ETF 的板块它顶掉其余三维 15% 的
    话语权，对没有的板块它什么都不做（`weighted_score` 会把 `available=False`
    的项从分母剔除并重新归一化）—— 于是同一个名义权重在不同板块上含义不同，
    **第二层的分在不同板块间不可比**。

    改法：`etf` 权重置 0（维度仍然计算并保留在 payload 里供面板/回测使用），
    异动改为**最终分上的加分**（`etf.breakout_bonus`，0~20，不占权重），
    与第三层"龙头共振门控加分"同一范式。这样：
      - 没有 ETF 的板块加分为 0，是**中性**的，不会扭曲其余三维；
      - 有 ETF 异动的板块额外加分，是**增量**信息，正是它该起的作用。

    ## V2.2：三维权重按 IC 重排（`northbound` 主导）

    实测逐维度 IC（H=20 / H=60，`scripts/factor_ic_report.py`）：

        northbound    +0.0355 / +0.0684   IC>0 69%   ← 唯一随持有期增强的维度
        volume_price  +0.0104 / +0.0041   IC>0 51%
        leverage      -0.0208 / +0.0080   IC>0 50%   ← 无信号

    离线实测（在已落库的维度分上合成）：

        现行 40/35/25           H=60 IC +0.0407   IC>0 60%
        只用 northbound          H=60 IC +0.0684   IC>0 69%
        northbound+volume_price  H=60 IC +0.0481   IC>0 62%（t 非重叠 3.27）

    所以把权重从"杠杆主导"改为**北向主导**（25/50/25）：
    `leverage` 是唯一 IC 为负的维度，降权；`northbound` 是唯一随持有期
    增强的维度，加权。保留 `volume_price` 而不是只留 northbound ——
    两者的组合在非重叠 t 值上是全表唯一超过 2 的（3.27），
    而"只用 northbound"虽然 IC 更高，但样本只有 15% 的可用率，
    单独押注它会让第二层在多数日子无分可算。

    要回到 V2.0 的原始口径：把这里的 `etf` 键删掉即可
    （`normalized_weights` 会自动把其余三项重新归一到合计 100）。
    `etf.enabled=false` 则只关掉这一维与加分的计算、保留配置。
    """

    weights: dict[str, float] = field(default_factory=lambda: {
        "leverage": 25.0, "northbound": 50.0, "volume_price": 25.0,
        "etf": 0.0})
    leverage: LeverageConfig = field(default_factory=LeverageConfig)
    northbound: NorthboundConfig = field(default_factory=NorthboundConfig)
    volume_price: VolumePriceConfig = field(default_factory=VolumePriceConfig)

    DIMS = ("leverage", "northbound", "volume_price", "etf")

    def normalized_weights(self) -> dict[str, float]:
        return _normalize(self.weights, self.DIMS)


# ==================================================================
# 四、第三层：龙头共振确认（门控）
# ==================================================================


@dataclass
class SeatConfig:
    """龙虎榜席位追踪参数。"""

    min_stocks: int = 3
    min_net_buy: float = 5_000_000.0
    institution_weight: float = 1.0
    branch_weight: float = 0.6
    lookback_days: int = 5


@dataclass
class MemberPoolConfig:
    """板块成分股池的过滤规则（**成交额口径，已被 `RelevanceConfig` 取代**）。

    ⚠️ **默认已关闭**（`configs/mainline.yaml` 里 `pool.enabled: false`）。
    现行口径是 `relevance`：逐股票判定"该题材是否属于它自身最相关的前 3 题材"
    + 总市值门槛。两套同时开会**叠加裁剪**，得到"为什么只剩这么几只票"的
    不可解释结果 —— 所以要切换口径就显式改 YAML，而不是两套都留着。

    保留本配置是因为它在**回测**里仍有价值：成交额口径只用当日及之前的数据，
    没有前视偏差（`relevance` 的走势相关性同样只用历史，但主营描述与概念
    归属都是当前快照）。要对比两种口径的效果时把它打开即可。

    ---

    ## 原设计说明（成交额口径）

    概念板块的成分股来自同花顺 `ths_member`，**一只股票可以属于几十个概念** ——
    其中大量是"蹭概念"的：大市值股因为被指数/主题基金持有而被挂进去，
    小市值股因为名字沾边被挂进去。实测（2024-06-12）：

        CRO概念  77 只成分股：成交额 top30% 贡献了 |净流入| 的 84%
        医疗服务 56 只成分股：top30% 贡献 83%
        农业种植 93 只成分股：top30% 贡献 69%

    也就是说 **70% 的成分股只贡献 16~31% 的资金动作**。

    ## 相关度用「成交额」而不是「主营业务」

    需求提出的是"主营业务相关度最高的前 30%"。用主营构成的困难在于**名称对不上**：
    `fina_mainbz` 返回的业务条目是"化学业务/测试业务/生物学业务"，
    而板块叫"CRO概念" —— 最核心的那只票恰恰匹配不上，纯名称匹配会把它筛掉。

    因此本口径用**成交额占比**作为"市场验证过的相关度"：

    - 真属于这个概念的票会被资金集中交易，蹭概念的票通常成交额不突出；
    - 它只用当日及之前的数据，**没有前视偏差**；
    - 它自动更新，不需要为 2000+ 个概念维护词表。

    ## 人工覆盖优先

    `configs/mainline_pools.yaml` 里的 `include` / `exclude` 优先级最高。
    """

    enabled: bool = True
    #: 相关度口径：turnover=按成交额分位 / mainbz=按主营业务匹配 / both=两者并集
    mode: str = "turnover"
    #: 只保留相关度最高的前百分之多少（默认 0.30）
    top_pct: float = 0.30
    #: 成分股数少于该值时不裁剪（小板块再裁就没剩几只了）
    min_members: int = 8
    #: 裁剪后至少保留几只
    keep_min: int = 5
    #: 流通市值下限（元）；0 = 不启用。默认 0（**不设市值门槛**，理由见下）
    min_circ_mv: float = 0.0
    #: 永远保留市值/成交额排名前 N 的股票（龙头不该被裁掉）
    always_keep_top: int = 3
    #: 排除 ST / *ST / 退市股（它们的资金动作与板块主线无关）
    exclude_st: bool = True
    #: 规则文件（人工增删名单）
    rule_file: str = "mainline_pools.yaml"

    #: 合法口径
    MODES = ("turnover", "mainbz", "both")


@dataclass
class RelevanceConfig:
    """成分股**提纯**（相关性 + 市值门槛）—— 取代 `MemberPoolConfig` 的成交额口径。

    ## 为什么另起一套而不是改 `pool`

    `pool` 的口径是"**成交额**前 30% 作为市场验证过的相关度"。它有两个致命缺陷：

    1. **裁掉了"正在被吸筹但还没成交"的票** —— 而"启动前"恰恰是本模块要抓的
       场景。地量建仓期的票成交额必然低，正是最该保留的；
    2. **比例裁剪与板块成分股数强相关** —— 236 只成分股的 PCB 概念砍掉 70%
       仍留 70 只，而 20 只成分股的小概念砍到只剩 6 只，两者口径完全不可比。

    新口径不做比例裁剪，而是**逐股票**判定：该题材必须落在这只股票自身
    「最相关的前 N 题材」之内。一只股能在几个板块里出现，取决于它自身有多少
    个真相关的题材，而不是板块有多大。

    ## 相关性的两个信号（缺一不可）

    - `corr_weight`：**走势相关性**权重（市场把这只股当什么在炒）。
      实测三花智控与"人形机器人"+0.680、"家电零部件"（真实主营）仅 +0.515 ——
      只看主营会把"人形机器人"砍掉，与使用者意图正好相反；
    - 其余权重给**主营业务相关性**（公司实际做什么，由 LLM 判定）。
      反向的坑：三花与"近期解禁"+0.567 靠巧合排第 7，只有主营判断能剔掉它。

    相关性离线算一次（`ml_member_corr` + `ml_stock_theme`），市值门槛则**必须**
    在运行期按 `trade_date` 现取，否则回测会用今天的市值筛过去的成分股。
    """

    enabled: bool = True
    #: 过滤哪些板块类型：`concept`=同花顺概念（要滤）；`sw_l1`=申万一级行业
    #: （是行业分类不是题材，不滤）。实测 `ml_board.kind` 只有这两种。
    kinds: list[str] = field(default_factory=lambda: ["concept"])
    #: 相关性门槛：题材必须落在个股自身归属题材的前 N 名
    top_themes: int = 3
    #: **总市值**下限（元）：低于它的成分股删除。0 = 不启用市值门槛
    min_total_mv: float = 3_000_000_000.0
    #: 排除 ST / *ST / 退市股。ST 股涨跌幅限制（±5%）与正常股不同，
    #: 资金动作与板块主线无关。名称按**运行日**从行情仓取（ST 状态会变）。
    exclude_st: bool = True
    #: 走势相关性在综合分里的权重（其余归主营业务相关性）
    corr_weight: float = 0.6
    #: 走势相关性回看交易日数
    corr_window: int = 240
    #: 交给 LLM 的候选题材上限（按相关性取前 N 个，省 token）
    max_candidates: int = 20
    #: LLM 路由层。实测（2026-09-20，真实打分 prompt，16 只并发 8）：
    #:
    #:     decision  deepseek-v4-pro    单只 40.1s  质量最好，全量约 208 分钟
    #:     reasoning deepseek-flash     单只 15.9s  质量接近，全量约  83 分钟
    #:     medium    deepseek-flash     单只 70.8s  （同模型，未见更快）
    #:     light     qwen2.5:1.5b 本地   单只  2.2s  **不可用**
    #:
    #: ⚠️ **`light` 层实测不可用**：本地 1.5B 模型对每个题材都给 85 分、
    #: 理由是从题面复制的模板（"主营核心业务直接属于该题材"），
    #: 而且随机只答 1~6 个（候选有 20 个），漏答严重 —— 打分完全失去区分度。
    #: 记录在此，避免后人"为了省钱换成 light"。
    llm_tier: str = "reasoning"
    #: 打分并发。实测 16 路稳定推进（约 56 只/分钟）；再高容易撞限流。
    concurrency: int = 16
    #: **业务判定对"相关性直通"的否决权**（Task 1）。
    #:
    #:     关闭（默认）：`corr >= corr_direct` → 直接归属，业务分**不参与**。
    #:     打开：LLM 判过这个题材且 `< BUSINESS_PASS` → **拦下**，相关性不得覆盖。
    #:
    #: ## ⚠️ 为什么默认关闭（2026-09-23 决定，不是遗漏）
    #:
    #: 这条规则**实测跑过一次全流程**（`purify_members --all --replace --no-llm`
    #: + 903 天全量重打分），结论写在 `docs/MAINLINE_MINING.md` §16.74：
    #:
    #:     聚合指标变好（20 日均值 +2.56% → +2.72%、大跌占比 7.4% → 6.9%、
    #:     真值漏报 4 → 3），但**启动集 FP/TP 在留出窗口变差（1.27 → 1.38）**。
    #:
    #: 用户明确说过「重点是误报影响大」，而工具里**事先写死**的判据是
    #: 「③④变差就该回退」→ **按规则回退**。所以现行 `ml_member_pure`
    #: 就是"未否决"的结果，默认值必须与它一致，否则代码与数据再次错位。
    #:
    #: ## 打开它的影响面（要改就得连着做）
    #:
    #:     全池约 5322 条成员被剔除（占已纳入的 22%）
    #:     → 龙头共振与板块资金流都会变
    #:     → 必须 `python scripts/rescore_mainline.py --force` 重算历史分数
    #:     → 再重算拥挤度指标（成员变了 flow_ratio 会变）
    #:
    #: ## 打开它的三个已知副作用（都不是 bug，是口径）
    #:
    #: 1. **`ml_member_pure.source` 的含义会变**：关闭时"相关性命中"的票
    #:    记 `source='corr'`；打开后被拦的票记 `source='llm'` 且 `relevant=0`。
    #:    展示层（`datastore._admission`）按 `(source, score)` 推入池通道，
    #:    两种状态都能正确渲染 —— 但**下游若有人直接读 `source` 判"是否相关性
    #:    入池"，语义会跟着变**。
    #: 2. **被拦的票不会消失**，只是 `relevant=0`；`ml_member`（原始名单）
    #:    一条都不动 —— "用哪些票算分"与"这个板块有哪些票"是两件事。
    #: 3. **`score is None`（LLM 没判过）永远不罚**：那占 corr 成员的 52%，
    #:    一起罚会误伤"只是没排上 LLM 候选"的真成分股。这条与开关无关，恒成立。
    business_veto: bool = False


@dataclass
class LeaderConfig:
    """龙头共振确认（三维交集法 + 门控加分）。"""

    top_n: int = 3
    capital_window: int = 5
    momentum_window: int = 10
    volume_window: int = 5
    #: 龙头股 N 日主力净流入 / 板块 N 日主力净流入 的门槛（>30% 触发共振）
    resonance_ratio: float = 0.30
    #: 门控加分在区间内的取值：共振越强加得越多（见 service 的 _gate_bonus）
    bonus_min: float = 15.0
    bonus_max: float = 20.0
    #: 龙虎榜席位确认也算共振（辅助确认，权重低于资金集中度）
    seat_counts_as_resonance: bool = True
    seat: SeatConfig = field(default_factory=SeatConfig)


# ==================================================================
# 五、告警
# ==================================================================


@dataclass
class AlertScopeConfig:
    """告警的**范围限制**：哪些板块不发主线告警、哪些只在特定月份发。

    配置来源是独立文件 `configs/mainline_alert_exclusions.yaml`（不是
    `mainline.yaml`）—— 因为这份名单是"业务取舍的台账"，每一条都要带
    理由与日期，混进主配置会让它变成一堆散落的代码。

    ⚠️ 与 `sector_blacklist.yaml` 的区别：那是**池级**排除（板块不进池、
    不同步数据）；这里是**告警级**排除 —— 板块照常打分、照常出现在
    拥挤度看板，只是 `_decide_level` 不再为它产生告警。
    """

    path: str = "configs/mainline_alert_exclusions.yaml"
    #: `{板块代码: 理由}` —— 完全不发告警
    excluded: dict[str, str] = field(default_factory=dict)
    #: `{板块代码: ((起, 止), ...)}` —— **只在这些月-日区间内**告警
    #:
    #: 区间写法 `"MM-DD"`，例如 `("02-06", "04-15")`；支持跨年（如
    #: `("12-20", "01-15")`）。允许告警的实际区间 = `[起 − lead_days, 止]`。
    seasonal: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    #: `{板块代码: ((起, 止), ...)}` —— 区间外**只放行 strong**
    strong_only: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    #: `{板块代码: 理由}` —— **全年只放行 medium + strong**（屏蔽 weak 观察档）
    #:
    #: ## 为什么需要它（而不是复用 `strong_only`）
    #:
    #: 档位是三级：🔴 `strong` 强信号 / 🟡 `medium` 中信号 / 🟢 `weak` 弱信号。
    #: `strong_only` 的语义是"**只**放行 strong"，把 medium 也一起挡掉；
    #: 用户对猪肉/中船系的指示是「**只留中强信号**」—— 要挡的是 weak 观察档，
    #: medium 要留。两者差得很远（猪肉 52 条告警里：strong_only 留 17 条、
    #: medium_up 留 26 条），所以必须是**两个模式**，不能借用。
    #:
    #: ⚠️ 用户这句话有两种读法（"中强"=「中+强」还是「中强」一个词），
    #: 这里取字面读法（= 中 + 强），因为档位的名字就是强/中/弱。
    #: 若其实只要 strong，把板块从这一节挪到 `strong_only`（不带 windows 时
    #: 会全天生效）即可 —— 实测猪肉会从留 26 条变留 17 条、中船系 34 → 18。
    medium_up: dict[str, str] = field(default_factory=dict)
    #: `{板块代码: (分数下限, 理由)}` —— **针对单个板块**抬高告警分数线。
    #:
    #: ## 为什么需要"按板块"而不是抬全局阈值
    #:
    #: 全局抬阈值这条路本项目已经量过并否掉了：每少 5~8pp 误报要付出
    #: 7~27pp 真值覆盖（比例 1.2~3.3）。而误报集中在少数板块上
    #: （`MAINLINE_FALSE_POSITIVE_BOARDS.md`），**按板块**抬只影响那几个，
    #: 不动其余 130+ 个板块的政策。
    #:
    #: ## ⚠️ 但它**不是万灵药**，用之前必须先看分档前沿
    #:
    #: 实测（2026-09-22，冻结快照）：养鸡 885808 提到 `total ≥ 78` 能把
    #: FP/TP 从 3.00 压到 **1.67**（保留 55% 命中、砍掉 70% 误报）；
    #: 但**环氧丙烷 885903 恰恰相反** —— 它的命中集中在低分档、误报却在
    #: 高分档，任何抬阈值都让 FP/TP **变差**（≥78 → 3.50，≥80 → 0 命中）。
    #: 所以配置里必须写清"这一条是量过前沿才定的"，而不是拍脑袋。
    min_score: dict[str, tuple[float, str]] = field(default_factory=dict)
    #: `{板块代码: (分数上限, 理由)}` —— **针对单个板块**给告警分数**封顶**。
    #:
    #: ## 它要解决什么（用户 2026-09-22 的假设）
    #:
    #: 用户原话：「因为高分数过高而误报率高的题材，这种应该是概念大涨一波的
    #: 顶部，报出了分数…如果是那就加一个高分门限**上限**，阻止高位报主线。」
    #:
    #: ## ⚠️ 实测：假设**只在少数板块成立**，不能当通则
    #:
    #: 全池按分数分档看未来 20 日收益（冻结快照 4765 条）：
    #:
    #:     [0,65)  +2.23%  亏损率 45%        [80,85)  +2.26%  亏损率 42%
    #:     [70,75) +2.39%  亏损率 39%        [85,90)  +2.54%  亏损率 41%
    #:     [75,80) +1.76%  亏损率 44%        [90,∞)   **+4.55%  亏损率 27%**
    #:
    #: **最高分档的收益最好、亏损率最低** —— 所以"高分=顶部"在全池层面是**错的**，
    #: 给分数封顶会系统性地删掉最好的告警。
    #:
    #: 但逐板块扫描（高分档 ≥3 条的 112 个板块）里，**22 个（20%）确实呈现
    #: 高分档收益为负**：汽车芯片（高分档 −2.39% vs 低分档 +5.11%）、
    #: 稀土永磁（14 条，−1.09% vs +1.54%）、中船系（10 条，−1.65% vs +0.85%）、
    #: 旅游概念、云游戏、AI PC、白酒概念、参股券商、啤酒… 对这一小批，
    #: 封顶是**有证据支持**的。
    #:
    #: 所以配置里每一条都必须写明是**量过该板块自己的分档收益**才定的；
    #: 不许因为"全池高分档看起来误报多"就批量加 —— 那是把通则在数据上
    #: 站不住的假设当成了结论。
    max_score: dict[str, tuple[float, str]] = field(default_factory=dict)
    #: **左侧冗余的自然日数**（默认 14 天 = 两周）。
    #:
    #: ## 语义（用户 2026-09-22 两次澄清后定稿）
    #:
    #: 「窗口日期往前两周，比如历史上高度炒作时间是 **2.6-4.15**，
    #:   那窗口就是 2.6 − 14 个自然日，也就是 **1.23 就要放开**告警限制。」
    #:
    #: 所以冗余是加在**窗口起点**上，而不是加在"月初"上：
    #: 允许区间 = `[起点 − 14 天, 终点]`。
    #:
    #: 第一版把它理解成"允许月份的月初往前推 14 天"，那是**月份粒度**的近似，
    #: 与"窗口是一段具体的月-日区间"（2.6–4.15）不是一回事：月份粒度会
    #: 把 2 月 1–5 日也算进来（多放行），又会漏掉"窗口止于月中"的情况。
    #:
    #: ## 为什么必须留
    #:
    #: 主线告警**经常提前一两周报**（这是设计目标，不是缺陷）。窗口边界若
    #: 卡死，会把"旺季前两周发的、本来正确的那条"压掉 —— 用规则制造漏报。
    lead_days: int = 14
    #: 读配置失败时的说明（进 `gaps`，不静默）
    load_note: str = ""

    def _in_any_window(self, raw: str,
                       spans: tuple[tuple[str, str], ...]) -> bool:
        """告警日是否落在任一窗口内（含**起点左侧 `lead_days` 冗余**）。"""
        try:
            today = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            return False
        for start_md, end_md in spans:
            try:
                month_s, day_s = (int(x) for x in start_md.split("-"))
                month_e, day_e = (int(x) for x in end_md.split("-"))
            except (ValueError, AttributeError):
                continue
            # 跨年窗口要跨年份试三次（去年/今年/明年）才能覆盖所有情况
            for year in (today.year - 1, today.year, today.year + 1):
                try:
                    start = date(year, month_s, day_s)
                    end = date(year, month_e, day_e)
                except ValueError:
                    continue
                if end < start:                     # 跨年：止落在下一年
                    try:
                        end = date(year + 1, month_e, day_e)
                    except ValueError:
                        continue
                if start - timedelta(days=int(self.lead_days)) <= today <= end:
                    return True
        return False

    @staticmethod
    def _describe(spans: tuple[tuple[str, str], ...]) -> str:
        return "、".join(f"{a}~{b}" for a, b in spans)

    def restriction(self, code: str, trade_date: str) -> tuple[str, str]:
        """该板块在该日**是否被限制告警**。

        返回 `(模式, 理由)`，模式 ∈ `{"", "block", "strong_only", "medium_up"}`；
        空串表示不限制。

        判定顺序：**完全关闭 > 季节性关闭 > 区间外只发强信号 > 全年只留中强**。
        四个名单理论上互斥，但顺序写死可以避免"同一板块同时出现在两张表里"
        时出现不确定行为。`trade_date` 用 `YYYYMMDD`。

        ⚠️ 季节性判定带**起点左侧两周冗余**（见 `lead_days`）：
        告警可能提前报，卡死窗口起点会制造漏报。

        ⚠️ 本方法只说"被哪种规则限制"，**要不要真的拦掉某一档**由
        `permits()` 决定 —— 调用方一律走 `permits()`，不要自己比字符串，
        否则新增模式时必然会漏掉某一处（本文件被漏过不止一次）。
        """
        reason = self.excluded.get(code)
        if reason:
            return "block", f"该板块已关闭主线告警：{reason}"
        raw = str(trade_date)
        spans = self.seasonal.get(code)
        if spans and not self._in_any_window(raw, spans):
            return "block", (f"季节性炒作题材：只允许在 "
                             f"{self._describe(spans)}（各起点前 "
                             f"{self.lead_days} 天）告警，本次不发")
        strong_spans = self.strong_only.get(code)
        if strong_spans and not self._in_any_window(raw, strong_spans):
            return "strong_only", (
                f"季节性炒作题材：旺季为 {self._describe(strong_spans)}"
                f"（各起点前 {self.lead_days} 天），本次只放行 strong 强信号")
        reason = self.medium_up.get(code)
        if reason:
            return "medium_up", f"该板块只保留中/强信号（屏蔽 weak 观察档）：{reason}"
        entry = self.min_score.get(code)
        if entry:
            return "min_score", (f"该板块的告警分数线单独抬到 {entry[0]:g}"
                                 f"（全池默认见 `alert.strong_score`/"
                                 f"`medium_score`）：{entry[1]}")
        entry = self.max_score.get(code)
        if entry:
            return "max_score", (f"该板块的告警分数**封顶**在 {entry[0]:g}"
                                 f"（高分档被证实是该板块的阶段顶）：{entry[1]}")
        return "", ""

    def permits(self, code: str, trade_date: str, level: str,
                score: float | None = None) -> bool:
        """范围闸门的**唯一判据**：该板块在该日、该档位、该分数允不允许发告警。

        ## 为什么要有这个函数

        原来"模式字符串"的比对散落在 5 处（`service._decide_level`、
        `apply_alert_scope`、`alert_scope_effect`、`board_false_positive_report`），
        加一个新模式就得改 5 个地方 —— 漏掉任何一处都是**静默不一致**：
        重打分与后处理给出不同的表，而且不报错。本项目已经因为同类问题
        吃过亏（`six_dim` 的 `reverse_dims` 在加载器里被静默丢掉），
        所以把判定收敛到这里，调用方只问"放不放行"。

        `level` 用字符串（`"strong"` / `"medium"` / `"weak"` / `"none"`），
        这样脚本层不必导入 `SignalLevel` 枚举。未知档位按**不通过**处理 ——
        宁可少报一条，也不要让没识别的档位绕过闸门。

        ## `score` 的处理（`min_score` / `max_score` 需要它）

        `score` 是 `total`。配了分数限制的板块**必须**传分数，否则无法判定 ——
        这种情况按**不通过**处理并记一条 warning：静默放行会让"按板块调分数"
        看起来生效其实没生效，而静默拦截至少方向保守、且能从日志里发现。
        """
        mode, _why = self.restriction(code, trade_date)
        if mode == "block":
            return False
        if mode == "strong_only" and level != "strong":
            return False
        if mode == "medium_up" and level not in ("strong", "medium"):
            return False
        low = self.min_score.get(code)
        high = self.max_score.get(code)
        if low or high:
            if score is None:
                logger.warning(
                    "范围闸门：%s 配了分数限制但调用方没传分数，"
                    "本次按**不放行**处理（调用方需要补 score）", code)
                return False
            value = float(score)
            if low and value < float(low[0]):
                return False
            if high and value > float(high[0]):
                return False
        return True

    def blocked_reason(self, code: str, trade_date: str) -> str:
        """只关心"是否被完全拦下"时的便捷入口（兼容旧调用）。"""
        mode, why = self.restriction(code, trade_date)
        return why if mode == "block" else ""


@dataclass
class AlertRuleConfig:
    """告警触发规则（需求 3.6）。"""

    strong_score: float = 70.0          # 🔴 最终评分 ≥ 70
    #: 且至少 N 个维度得分超过**各自阈值**的 `strong_dim_ratio`（V2.0 = 2 个）
    strong_min_dims: int = 2
    strong_dim_ratio: float = 0.70
    medium_score: float = 55.0          # 🟡 最终评分 ≥ 55
    medium_requires_resonance: bool = True  # 且龙头共振触发
    weak_extreme_zscore: float = 3.0    # 🟢 单一维度极端异常（z-score 口径）
    # ---------- 🆕 突破触发（"越过自己的长期震荡上沿"） ----------
    #
    # 为什么加它：用户报障"多个板块的告警滞后真值日、基本在顶部才报"。
    # 逐日核对后看到的机制是：**绝对阈值 + 加分决定能不能报**，
    # 而不是"这个板块是不是刚突破它自己的震荡区间"。
    #
    # 实测（农业种植 885812，V2.2/V2.4 同口径）：20260623 那天它的六维排名是
    # **全市场第 8**（板块确实启动了），但 `total` 只有 62.3、**低于中信号线**，
    # 所以不报；真正报出来靠的是 20260626 的**门控加分 +15**（total 73.5）。
    #
    # 绝对阈值是**全市场统一**的，于是：全市场普涨时真领涨的板块可能只排到
    # 60 分档 → 漏报；等它"足够极端"时行情已到中后段 → 滞后。
    #
    # 所以这里并行加一条**只看它自己**的触发：
    #     ceiling = 该板块过去 `lookback_days` 天 `total` 的 `quantile` 分位
    #     breakout = total > ceiling 且 total ≥ min_score
    # `min_score` 是必须的全局下限，否则一个长期在 30 分徘徊的板块
    # 冲上 35 分也会报（"矮子里拔将军"）。
    breakout_enabled: bool = True
    #: 回看多少个交易日算"长期震荡"（120 ≈ 半年）
    breakout_lookback_days: int = 120
    #: 上沿取历史 `total` 的哪个分位
    breakout_quantile: float = 0.90
    #: 全局下限：低于它的"突破"不报
    breakout_min_score: float = 50.0
    #: 历史至少要有多长才判突破（不足则这条路径不生效，避免开盘初期乱报）
    breakout_min_samples: int = 60
    #: 「刚越过去」的判定：最近这么多天必须**都在上沿之下**。
    #: 没有这一条，持续走强的板块几乎天天在自己的 90 分位之上 →
    #: 天天报（实测占候选池 11~20%）。用户要的是"**过了**上限"= 穿越。
    breakout_fresh_days: int = 10
    #: 「连续 N 个信号周期内触发」作为确认条件（区分试盘与真启动）
    confirm_periods: int = 2
    #: 同一板块同等级告警冷却（交易日）
    cooldown_days: int = 5
    #: 各维度的"满分基准阈值"：维度分 ≥ ratio×该值 才算该维度达标。
    #: 缺项维度用 `default_dim_threshold`。
    default_dim_threshold: float = 60.0
    dim_thresholds: dict[str, float] = field(default_factory=dict)

    # ---------- 龙头提名通道（"雪中送炭"） ----------
    #
    # 为什么需要它：2024-06 的医药是最好的反例 —— 医疗服务板块的龙头资金集中度
    # 达到 110%~162%（药明康德逆势吸筹），但它的第一层六维分只有 43、排 61/93，
    # **连候选池都没进**，于是第三层根本没被评估，那 20 分门控加分被直接丢弃。
    #
    # 根因不是门槛高低，而是**评估范围**：第一层六维本质是趋势/状态模型，
    # 在板块刚开始转折时必然给低分（医药当时景气度 18、技术 36）。而"龙头先动、
    # 板块还没动"恰恰是本模块声称要抓的"启动前"场景。
    #
    # 因此给第三层开一条**独立提名通道**：即使没进候选池，只要龙头共振足够强
    # 且第一层不是彻底没救，就把该板块送进观察/告警，并明确标注 `promoted=True`
    # （它的分数没经过第二层，与其他板块不可直接比较）。
    nominate_enabled: bool = True
    #: 提名所需的集中度门槛（0.5 = 龙头吸走板块全部资金动作的一半）
    nominate_min_concentration: float = 0.45
    #: 提名所需的第一层分下限（防止把"跌了很久刚好有人抢反弹"的板块全捞进来）
    nominate_min_score: float = 35.0
    #: **共振通道**的单次提名上限（再多就变成刷屏，提名的意义是"值得看一眼"
    #: 而不是"全都看"）。
    nominate_limit: int = 8
    #: **ETF 异动通道**的单次提名上限（默认 3）。
    #:
    #: 为什么是**两条独立预算**而不是从 `nominate_limit` 里分：
    #: 两条通道回答的是两个不同的问题 ——
    #:   共振通道："资金是不是集中在少数龙头股上"
    #:   ETF 通道："场外资金是不是在申购这个板块"
    #: 它们不是同一个排行榜的两段，共用一个名额池时必然互相饿死。实测
    #: （2026-07-06）：两者共用 8 个名额时，共振那边当天就有 8 个以上达标，
    #: **ETF 通道实际拿到 0 个名额** —— 农业种植凭 ETF 二级异动加了 15 分、
    #: 总分 67.99，却一条告警都没有。
    #:
    #: 为什么这个"饿死"是致命的而不是"少看几条"：`_decide_level` 规定候选池外
    #: 的板块**只能**走提名通道（池外没算第二层，分数与池内不是同一把尺子）。
    #: 所以名额被占满 = 这个通道彻底失效，而"第一层看不到的拐点"恰恰是它存在
    #: 的唯一理由 —— 农业第一层排 22、进不了候选池，正是这一类。
    nominate_etf_limit: int = 3

    def threshold_for(self, dim_key: str) -> float:
        value = self.dim_thresholds.get(dim_key)
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self.default_dim_threshold
        return number if number > 0 else self.default_dim_threshold


# ==================================================================
# 六、回测
# ==================================================================


@dataclass
class BacktestConfig:
    """Purged Walk-Forward 回测参数。"""

    train_days: int = 120
    validate_days: int = 20
    purge_days: int = 10
    holding_days: list[int] = field(default_factory=lambda: [5, 10, 20])
    top_n: int = 10
    min_boards: int = 20
    #: 评估目标阈值（报告里标注达标与否，不参与计算）
    targets: dict[str, float] = field(default_factory=dict)
    hit_threshold: float = 0.05         # 触发后 20 日涨幅 > 5% 记为兑现
    max_gain_window: int = 60           # 告警后 N 日内的最大涨幅
    annual_trading_days: int = 244
    #: 分场景验证的案例（写在 YAML 里，代码不硬编码日期）
    scenes: list[dict[str, Any]] = field(default_factory=list)
    #: 回测采样步长（交易日）。**默认 1 = 逐日评估**。
    #: 调大它纯粹是"用精度换时间"：一天完整漏斗约 1.2~2 秒，1150 个交易日要
    #: 25~40 分钟；`step_days=5` 把 IC 样本从 1150 降到 230（仍够算 ICIR），
    #: 耗时降到 1/5。这个取舍必须由使用者显式做，不该由代码替他决定。
    step_days: int = 1
    #: 跨层因子相关性上限：超过它就打印高相关因子对（需求 4.4）
    correlation_limit: float = 0.70


# ==================================================================
# 七、期货先行信号
# ==================================================================


@dataclass
class MomentumSignalConfig:
    zscore_window: int = 60
    zscore_trigger: float = 1.5
    ret_10d_trigger: float = 0.05


@dataclass
class CorrelationSignalConfig:
    window: int = 20
    low_threshold: float = 0.30
    high_threshold: float = 0.60
    lookback_days: int = 5


@dataclass
class OpenInterestConfig:
    window: int = 5
    change_trigger: float = 0.10


@dataclass
class FuturesAlertConfig:
    domestic_gain_pct: float = 0.05
    domestic_oi_change: float = 0.10
    foreign_daily_gain: float = 0.03
    chain_min_varieties: int = 3


@dataclass
class FuturesBacktestConfig:
    min_median_lead_days: int = 5
    min_median_max_gain: float = 0.15
    max_gain_window: int = 60


@dataclass
class RecalibrationConfig:
    window_days: int = 250
    interval_days: int = 90


@dataclass
class FuturesConfig:
    enabled: bool = True
    #: 品种↔板块映射表文件名（configs/ 下）
    mapping_file: str = "mainline_futures_mapping.yaml"
    #: 商品指数基准（用于无映射品种的兜底对照）
    momentum: MomentumSignalConfig = field(default_factory=MomentumSignalConfig)
    correlation: CorrelationSignalConfig = field(
        default_factory=CorrelationSignalConfig)
    open_interest: OpenInterestConfig = field(default_factory=OpenInterestConfig)
    term_structure: dict[str, Any] = field(
        default_factory=lambda: {"enabled": True})
    alerts: FuturesAlertConfig = field(default_factory=FuturesAlertConfig)
    backtest: FuturesBacktestConfig = field(
        default_factory=FuturesBacktestConfig)
    recalibration: RecalibrationConfig = field(
        default_factory=RecalibrationConfig)
    #: 内盘品种的交易所列表（发现品种名录用）
    exchanges: list[str] = field(default_factory=lambda: [
        "SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"])


# ==================================================================
# 八、板块类型差异化参数
# ==================================================================


@dataclass
class BoardProfile:
    key: str
    label: str
    decay_days: int


@dataclass
class BoardProfilesConfig:
    default: BoardProfile = field(
        default_factory=lambda: BoardProfile("default", "通用", 20))
    profiles: dict[str, BoardProfile] = field(default_factory=dict)
    overrides: dict[str, list[str]] = field(default_factory=dict)

    def profile_for(self, board_name: str) -> BoardProfile:
        """按板块名匹配类别（**包含匹配**，取最长关键字优先）。"""
        best: tuple[int, BoardProfile] | None = None
        for key, keywords in self.overrides.items():
            for keyword in keywords:
                if keyword and keyword in board_name:
                    profile = self.profiles.get(key)
                    if profile is None:
                        continue
                    if best is None or len(keyword) > best[0]:
                        best = (len(keyword), profile)
        return best[1] if best else self.default


# ==================================================================
# 九、数据源与运行
# ==================================================================


@dataclass
class DataConfig:
    """数据源与本地缓存的运行参数。"""

    #: 本地主线数据仓（**独立于 15 GiB 行情仓**，见 datastore.py）
    cache_path: str = field(
        default_factory=lambda: _store_rel("mainline_cache"))
    eastmoney_min_interval: float = 1.05
    eastmoney_timeout: float = 15.0
    catalog_ttl_hours: float = 24.0
    snapshot_ttl_seconds: float = 300.0
    history_days: int = 400
    min_members: int = 5
    max_members: int = 400
    #: 板块名排除关键字：这些是**宽基/风格指数**，不是题材主线。
    #: 实测同花顺概念名录里混着「同花顺A50全成分」「同花顺中证30全成分」
    #: 「沪深300样本股」这类板块 —— 它们成分股数合规（`min_members` 拦不住）、
    #: 走势与大盘几乎重合，一旦进榜就会长期占据主线头部，
    #: 把真正的题材板块挤出去（而且看起来"分数很高"，不会引起怀疑）。
    exclude_keywords: list[str] = field(default_factory=lambda: [
        "全成分", "成份", "成分股", "样本股", "指数成分", "等权", "加权",
        "标的证券"])
    #: 每轮打分最多算多少个板块（防御性上限：目录异常时不要让接口挂住）
    max_boards: int = 320
    #: 东财 datacenter 北向持股接口的每页条数与限速（免费接口，必须客气）
    northbound_page_size: int = 500
    northbound_min_interval: float = 0.35


@dataclass
class UniverseConfig:
    """标的池来源。

    ## 为什么默认复用「板块拥挤度」的板块池

    拥挤度模块已经把同花顺 2517 个指数整理成 `sector_crowding_list`
    （哪些可见、哪些隐藏、哪些人工置顶），使用者在那个界面上做的勾选
    **就是研究判断本身**。主线挖掘再自己拉一份 `ths_index` 只会得到两个
    慢慢漂移的"板块池"，而且使用者在拥挤度里做的增删在主线上不生效。

    实测（2026-09）`visible=1` 共 **445 个**板块。

    ## 为什么要在这里再排一遍"非概念"

    拥挤度的 `non_concept_name_patterns` 只作用于**告警与前端默认筛选**
    （它的注释写明了"不影响数据落库"），所以 `visible=1` 里仍然混着
    `同花顺金仓100`、`同花顺漂移100` 这类**策略/风格指数** ——
    它们不是题材概念，行情与宽基指数几乎重合，进主线榜单会长期占据头部
    并把真正的题材挤出去（而且"分数很高"，不会引起怀疑）。
    因此主线侧必须自己再排一次。
    """

    source: str = "crowding"       # crowding=复用拥挤度板块池 / catalog=自建名录
    include_sw_l1: bool = False
    include_concept: bool = True
    #: 概念板块的名录源：ths=同花顺（ths_index）/ dc=东方财富（dc_index）
    concept_source: str = "ths"
    sw_l1_range: tuple[str, str] = ("801010", "801980")
    benchmark: str = "000300.SH"
    #: 板块黑名单文件（`configs/` 下的文件名）。清单冻结，只登记冻结时点
    #: `visible=0` 的板块；之后新增的概念板块不在其中，会自动入池并同步数据。
    #: 细节见 `load_sector_blacklist`。
    blacklist_file: str = "sector_blacklist.yaml"
    #: 按回测 **20 日胜率**生成的题材剔除清单（`configs/` 下的文件名）。
    #:
    #: ⚠️ 它是**池级**剔除，与 `blacklist_file` 同一语义（也不再多同步数据）——
    #: 两者一起在 `store.boards()` 里生效。清单由
    #: `scripts/build_theme_exclusions.py` 生成，文件读不到时**不额外剔除**
    #: （见 `load_theme_exclusions`）。判据与生成方式写在 `theme_gate` 的模块文档里。
    theme_exclude_file: str = "mainline_theme_exclusions.yaml"
    #: 板块名排除正则（不含题材含义的策略 / 风格 / 行业 / 地区指数）。
    #: 与 `sector_crowding/config.yaml` 的同名规则**保持一致** ——
    #: 两个模块对"什么算概念"必须给出同一个答案，否则同一个板块在拥挤度里
    #: 被排除、在主线上却进榜，使用者无法判断该信哪个。
    exclude_name_patterns: list[str] = field(default_factory=lambda: [
        # 「同花顺」是**数据商前缀而不是题材名**，带这个前缀的全是策略/风格/
        # 宽基指数（同花顺A50、A50全收益、金仓30全收益、金仓100、全50、
        # 漂移100…）。它们与宽基指数几乎重合，进榜单会长期占据头部并把
        # 真正的题材挤出去。一条 `^同花顺` 覆盖全部，比逐个列举稳。
        r"^同花顺",
        r"全收益$",
        r"^昨日涨幅",
        r"^昨日跌幅",
        r"^昨日换手",
        r"样本股$",
        r"成份股$",
        r"成分股$",
        r"业指数$",      # 行业指数（同花顺 I 类）
        r"市指数$",      # 地区指数（同花顺 R 类）
    ])

    def is_excluded(self, name: str) -> bool:
        """板块名是否命中排除规则（正则 `search` 语义；坏正则跳过不抛错）。"""
        import re

        text = str(name or "")
        for pattern in self.exclude_name_patterns:
            try:
                if re.search(pattern, text):
                    return True
            except re.error:  # 用户写错正则不该让整轮打分崩
                continue
        return False

    #: 按**代码前缀**排除的板块。默认 `871` —— 那是 GICS 式港股/美股行业分类
    #: 的命名空间（`商业银行`/`航空航天与国防`/`客运航空公司`/
    #: `半导体产品与设备`/`互动媒体与服务`…），不是 A 股概念板块。
    #:
    #: 用前缀而不是逐个列代码：`871xxx` 是一整个体系，将来新增的同体系指数
    #: 也该一起排除。这与"新增概念要自动入池"不冲突 —— 新 A 股概念是
    #: `875/885/886xxx`，不会落进这个前缀。
    exclude_code_prefixes: list[str] = field(default_factory=lambda: ["871"])

    #: 按**具体代码**排除的板块：地域性概念（`海峡两岸`/`雄安新区`/
    #: `一带一路`…）与统计性/风格板块（`次新股`家族、`高股息`家族、`摘帽`）。
    #:
    #: 为什么用显式代码而不是名字正则：地域与统计类名字没有可靠的通配规律
    #: （"智慧城市"含"市"却是真概念，"上海自贸区"含"上海"确是地域），
    #: 一条正则必然误伤。冻结清单的语义见 `load_sector_blacklist`。
    #:
    #: ⚠️ 这份清单只影响**分析池**（谁进 `ml_board` 参与排名），
    #: **不影响拥挤度的数据更新** —— 地域板块在拥挤度界面仍可查看。
    #: 要让某个板块彻底停止更新数据，把它加进 `blacklist_file`。
    exclude_codes: list[str] = field(default_factory=list)

    def is_excluded_code(self, code: str) -> bool:
        """板块代码是否命中前缀或显式排除清单。"""
        text = str(code or "").strip()
        if not text:
            return False
        head = text.split(".")[0]
        for prefix in self.exclude_code_prefixes:
            if str(prefix or "").strip() and head.startswith(str(prefix).strip()):
                return True
        return text in {str(item).strip() for item in self.exclude_codes}


#: 黑名单缓存：`{文件名: (mtime, 结果)}`。
#:
#: **必须带 mtime**：删掉清单里的某个代码是"恢复跟踪某板块"的正常操作
#: （见 `sector_blacklist.yaml` 头部的指引），而服务是常驻的 ——
#: 只按文件名缓存会让"改完清单跑同步"在活着的进程里毫无效果，
#: 且不报错（板块就是不进池）。带上 mtime 后改文件即刻生效。
_BLACKLIST_CACHE: dict[str, tuple[float | None, frozenset[str] | None]] = {}


def _blacklist_path(filename: str) -> Path:
    """黑名单文件路径（与 `load_yaml_config` 的 base 规则保持一致）。"""
    return PROJECT_ROOT / "configs" / str(filename or "")


def load_removed_concepts(filename: str = "sector_blacklist.yaml"
                          ) -> frozenset[str] | None:
    """**用户在批次台账里明确点名剔除**的板块代码（不是整份黑名单）。

    ## 为什么需要它（而不是直接用 `load_sector_blacklist`）

    整份 `sector_blacklist.yaml` 有 600+ 条，其中大头是**历史遗留**：
    已淘汰的 `865xxx` 概念体系、GICS 行业分类、地域/统计板块。
    直线挖掘把它们排除在分析池外是**一开始就有的设计**；但**拥挤度模块
    刻意保留它们** —— `refresh` 的注释写着「数据先全量落库…这样以后想看
    行业拥挤度不用重跑 6 年」。

    所以"把拥挤度的读取也按整份黑名单过滤"是**过度过滤**：
    实测会把最新一周 1878 个板块砍到 1541，多砍掉的 192 个里绝大多数是
    行业指数。用户要的是"我点名剔掉的那些不进拥挤度检测与前端" ——
    那就只过滤**他点名的那批**。

    ## 代码从哪来

    批次台账 `concept_removal_batches.yaml` 存的是**用户的原始措辞**
    （第六、八批只给了名称、没有代码）；名称→代码的反查发生在**落库时**，
    结果写在 `sector_blacklist.yaml` 的**分批段**里（段头形如
    `# ---- … 第 6 批（18 个）----` 或 `第四批`）。所以这里按**段头**切分，
    只收带"第 N 批"标记的段 —— 那正是"用户点名"的集合。
    这样也顺便避免了二次反查（板块已出池，`ml_board` 里查不到它的名字了）。

    读不到文件 → `None`（表达"不可用"，调用方**不过滤**，与其它三处口径一致）。
    """
    target = Path(filename)
    if not target.is_absolute():
        # 与 `_blacklist_path` 同一规则：相对路径按 `PROJECT_ROOT/configs/` 解析
        target = PROJECT_ROOT / "configs" / str(filename or "")
    if not target.exists():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读不到板块黑名单（%s），本次不按批次过滤", brief(exc, BRIEF_TIGHT))
        return None
    out: set[str] = set()
    in_batch = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            if re.search(r"第\s*(\d+|[一二三四五六七八九十]+)\s*批", line):
                in_batch = True
            continue
        if in_batch:
            match = re.search(r'code:\s*"([^"]+)"', line)
            if match:
                out.add(match.group(1))
    return frozenset(out)


def load_sector_blacklist(filename: str = "sector_blacklist.yaml", *,
                          required: bool = True) -> frozenset[str] | None:
    """读板块黑名单代码集合；**文件不可用时返回 None**（不是空集合）。

    ## `required` 区分两类清单（这是两者唯一的差别）

    * `required=True`（默认，`sector_blacklist.yaml`）—— 这是**池子的定义**。
      读不到就没有池子边界，必须让调用方能区分"没有黑名单"与"读不到黑名单"，
      所以返回 `None` 并打警告。
    * `required=False`（`mainline_theme_exclusions.yaml`）—— 这是**在池子之上
      再收紧一道**的清单。读不到、或它本身就是空的（一个题材都还没被剔除）
      都是**正常状态**，返回**空集合**（= 不额外剔除）且不打"回退 visible=1"
      的警告 —— 那条警告对这类清单是纯粹的误导。

    ## 为什么用"显式冻结清单"而不是 `WHERE visible = 0`

    用 `visible = 1` 选池有两个隐患，换成显式清单后都消失：

    1. 将来新增的板块若默认 `visible = 0`，会被**静默漏掉且不报错** ——
       而新概念恰恰是最该跟踪的对象；
    2. 分不清"这个板块是被有意排除的"还是"它只是还没同步"，
       两者的排查方向完全相反。

    清单因此是**冻结**的：只登记冻结时点 `visible=0` 的那批代码。
    之后新出现的板块不在清单里，自动入池。

    ## 为什么失败要返回 None 而不是空集合

    空集合的含义是"不排除任何板块"，会让池子从 445 涨回 933，
    **所有排名与告警数量随之改变，而且不会报错**。返回 None 让调用方
    能区分"没有黑名单"和"读不到黑名单"，并回退到旧的 `visible = 1` 行为。

    ## 缓存随 mtime 失效

    进程常驻，所以不能只在首次读盘：删掉某个代码正是"恢复跟踪"的操作，
    缓存不带 mtime 会让它无效且无声。
    """
    # 缓存键要带 `required`：同一份文件在两种语义下结果不同（None vs 空集合），
    # 只用文件名做键会让后一次调用拿到前一种语义的结果。
    key = f"{filename or ''}|{bool(required)}"
    path = _blacklist_path(str(filename or ""))
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    cached = _BLACKLIST_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    result: frozenset[str] | None = None
    if filename:
        try:
            raw = load_yaml_config(str(filename))
        except Exception as exc:  # noqa: BLE001 配置层不该让调用方崩
            logger.warning("板块黑名单读取异常（按不可用处理）：%s",
                           type(exc).__name__)
            raw = None
        if isinstance(raw, dict):
            codes = []
            for item in raw.get("codes") or []:
                if isinstance(item, dict):
                    code = str(item.get("code") or "").strip()
                else:
                    code = str(item or "").strip()
                if code:
                    codes.append(code)
            if codes:
                result = frozenset(codes)
            elif not required:
                # 生成出来的清单可以是空的（一个题材都还没被剔除）——正常状态
                result = frozenset()
            else:
                logger.warning("板块黑名单 %s 里没有任何代码（按不可用处理）", filename)
    if result is None and not required:
        # 非必需清单：读不到 = "不额外剔除"，是正常状态，不是故障
        result = frozenset()
    if result is None:
        # ⚠️ `required=True` 的契约就是"读不到返回 None"，调用方据此回退到
        # `visible=1` 选池。这里**绝不能**把它收敛成空集合 —— 那等于宣称
        # "不排除任何板块"，池子会从 445 无声涨回 933。
        logger.warning("板块黑名单不可用（%s）：回退为 visible=1 选池",
                       filename or "(未配置)")
    _BLACKLIST_CACHE[key] = (mtime, result)
    return result


def reset_blacklist_cache() -> None:
    """清空黑名单缓存（改完清单文件后调用，免得要重启进程）。"""
    _BLACKLIST_CACHE.clear()


def load_theme_exclusions(filename: str = "mainline_theme_exclusions.yaml"
                          ) -> frozenset[str]:
    """按回测「20 日胜率」生成的**题材剔除清单**的板块代码集合。

    ## 为什么直接复用 `load_sector_blacklist`

    两份清单在"池级剔除"这件事上**语义完全一样**（不在打分池 → 不打分 →
    不发告警 → 不再同步行情），文件结构也一样（`codes:` + `code` 键）。
    差别只在**成因**与**谁来维护**：

    * `sector_blacklist.yaml` —— 人工冻结的历史遗留清单（已淘汰的 `865xxx`
      概念体系、宽基/风格指数、用户点名剔除的批次）；
    * 本文件 —— `scripts/build_theme_exclusions.py` 按历史 20 日胜率
      **自动生成**的台账（判得准、且实测准确率不过关的题材）。

    所以复用同一个解析器与同一套 mtime 缓存，不另写一份 —— 两份解析器迟早漂移。

    ## 读不到时返回空集合（**不过滤**），与黑名单的 `None` 不同

    黑名单是"池子的定义"，读不到会让池子从 445 涨回 933、**必须让调用方知道**；
    这份清单是"在池子之上再收紧一道"，读不到时保持原行为最安全
    （空集合 = 不额外剔除任何东西），所以这里把 `None` 收敛成空集合。
    """
    return load_sector_blacklist(filename, required=False)


@dataclass
class NotifyConfig:
    """推送配置。

    webhook **不在此文件里**，只从环境变量读：
    `MOSS_MAINLINE_DINGTALK_WEBHOOK` / `MOSS_MAINLINE_FEISHU_WEBHOOK`。
    写进 YAML 就等于把凭据提交进仓库。
    """

    enabled: bool = True
    min_level: str = "medium"
    timeout: float = 10.0

    @property
    def dingtalk_webhook(self) -> str:
        return os.environ.get("MOSS_MAINLINE_DINGTALK_WEBHOOK", "").strip()

    @property
    def feishu_webhook(self) -> str:
        return os.environ.get("MOSS_MAINLINE_FEISHU_WEBHOOK", "").strip()


@dataclass
class MainlineConfig:
    """主线挖掘完整配置。"""

    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)
    funnel: FunnelConfig = field(default_factory=FunnelConfig)
    six_dim: SixDimConfig = field(default_factory=SixDimConfig)
    chip: ChipConfig = field(default_factory=ChipConfig)
    macro: MacroConfig = field(default_factory=MacroConfig)
    accumulation: AccumulationConfig = field(default_factory=AccumulationConfig)
    etf: EtfConfig = field(default_factory=EtfConfig)
    pool: MemberPoolConfig = field(default_factory=MemberPoolConfig)
    relevance: RelevanceConfig = field(default_factory=RelevanceConfig)
    leader: LeaderConfig = field(default_factory=LeaderConfig)
    alert: AlertRuleConfig = field(default_factory=AlertRuleConfig)
    #: 告警范围限制（池级排除见 `sector_blacklist.yaml`，那是另一回事）
    alert_scope: AlertScopeConfig = field(default_factory=AlertScopeConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    futures: FuturesConfig = field(default_factory=FuturesConfig)
    board_profiles: BoardProfilesConfig = field(
        default_factory=BoardProfilesConfig)
    data: DataConfig = field(default_factory=DataConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    disclaimer: str = ""
    source_path: str = ""
    load_error: str = ""

    # ---------- 派生 ----------

    @property
    def min_history_days(self) -> int:
        """算完一轮信号所需的最短历史长度。"""
        ic = self.synthesis.ic_window_days
        return max(ic + self.synthesis.rebalance_days,
                   self.backtest.train_days + self.backtest.purge_days
                   + self.backtest.validate_days,
                   max(self.backtest.holding_days, default=20),
                   self.six_dim.windows.get("prosperity", 250) // 3,
                   self.chip.profit_ratio_window)

    @property
    def cache_file(self) -> Path:
        """本地数据仓的绝对路径（相对路径按仓库根解析）。"""
        path = Path(self.data.cache_path)
        return path if path.is_absolute() else PROJECT_ROOT / path


# ==================================================================
# 解析
# ==================================================================


def _normalize(weights: dict[str, float], keys: tuple[str, ...]) -> dict[str, float]:
    """把权重归一化到合计 100（缺项补 0；全 0 时退化为等权）。"""
    values = {key: max(0.0, _as_float(weights.get(key), 0.0)) for key in keys}
    total = sum(values.values())
    if total <= 0:
        equal = 100.0 / max(len(keys), 1)
        return {key: equal for key in keys}
    return {key: value * 100.0 / total for key, value in values.items()}


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # NaN


def _load_alert_scope(path: str) -> AlertScopeConfig:
    """读 `mainline_alert_exclusions.yaml` → `AlertScopeConfig`。

    ⚠️ 读不到时**必须留下 `load_note`**：静默降级成"没有任何限制"会让
    用户以为关掉的板块重新开始告警，而界面上看不出任何异常 —— 这正是
    本项目反复遇到的"静默 no-op"。缺文件、YAML 语法错、条目缺 `code`
    三种情况都要能区分。
    """
    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parents[2] / target
    scope = AlertScopeConfig(path=path)
    if not target.exists():
        scope.load_note = f"告警范围名单不存在（{target}），本次**不作任何限制**"
        logger.warning("告警范围名单缺失：%s", target)
        return scope
    try:
        import yaml

        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 配置坏掉不该让整个服务起不来
        scope.load_note = f"告警范围名单解析失败：{brief(exc, BRIEF_TIGHT)}"
        logger.warning("告警范围名单解析失败：%s", brief(exc, BRIEF_TIGHT))
        return scope
    # 左侧冗余天数：文件里没写就用默认 14（宁可宽松，不要因为配置漏写
    # 而把"提前报的旺季告警"压掉）
    try:
        scope.lead_days = int(raw.get("lead_days", scope.lead_days))
    except (TypeError, ValueError):
        scope.lead_days = AlertScopeConfig().lead_days
    for item in (raw.get("excluded") or []):
        if isinstance(item, dict) and item.get("code"):
            scope.excluded[str(item["code"])] = str(item.get("reason") or "未写明理由")
    def _spans(item: dict) -> tuple[tuple[str, str], ...]:
        """把一条配置解析成 `((起, 止), ...)`。

        `windows` 是**主写法**（月-日区间，支持跨年）；`months` 是便捷写法，
        展开成"整月窗口"（`[3, 9]` → `("03-01","03-31")`、`("09-01","09-30")`）。
        两种都支持是为了让"我只知道大概哪几个月"和"我有具体的历史窗口"
        都能表达，而不必逼调用方自己算月末。
        """
        spans: list[tuple[str, str]] = []
        for entry in (item.get("windows") or []):
            if not isinstance(entry, dict):
                continue
            start = str(entry.get("start") or "").strip()
            end = str(entry.get("end") or "").strip()
            if len(start) == 5 and start[2] == "-" and \
                    len(end) == 5 and end[2] == "-":
                spans.append((start, end))
        for value in (item.get("months") or []):
            try:
                month = int(value)
            except (TypeError, ValueError):
                continue
            if not 1 <= month <= 12:
                continue
            last = calendar.monthrange(2025, month)[1]
            spans.append((f"{month:02d}-01", f"{month:02d}-{last:02d}"))
        return tuple(spans)

    for item in (raw.get("seasonal") or []):
        if isinstance(item, dict) and item.get("code"):
            spans = _spans(item)
            if spans:
                scope.seasonal[str(item["code"])] = spans
    for item in (raw.get("strong_only") or []):
        if isinstance(item, dict) and item.get("code"):
            spans = _spans(item)
            if spans:
                scope.strong_only[str(item["code"])] = spans
    for item in (raw.get("medium_up") or []):
        if isinstance(item, dict) and item.get("code"):
            scope.medium_up[str(item["code"])] = str(item.get("reason")
                                                     or "未写明理由")
    def _score_limits(raw: dict, key: str) -> dict[str, tuple[float, str]]:
        """解析 `min_score` / `max_score` 两节（结构相同，只有语义相反）。"""
        out: dict[str, tuple[float, str]] = {}
        for item in (raw.get(key) or []):
            if not (isinstance(item, dict) and item.get("code")):
                continue
            try:
                threshold = float(item.get("score"))
            except (TypeError, ValueError):
                logger.warning("告警范围名单：%s 的 %s 不是数字（%r），已跳过",
                               item.get("code"), key, item.get("score"))
                continue
            out[str(item["code"])] = (threshold,
                                      str(item.get("reason") or "未写明理由"))
        return out

    scope.min_score = _score_limits(raw, "min_score")
    scope.max_score = _score_limits(raw, "max_score")
    scope.load_note = (f"告警范围：{len(scope.excluded)} 个板块完全关闭、"
                       f"{len(scope.seasonal)} 个季节性关闭、"
                       f"{len(scope.strong_only)} 个旺季外只发强信号、"
                       f"{len(scope.medium_up)} 个全年只留中强信号、"
                       f"{len(scope.min_score)} 个抬高分数线、"
                       f"{len(scope.max_score)} 个封顶分数线")
    return scope


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _build(cls: type, raw: dict[str, Any]) -> Any:
    """按 dataclass 字段名从 dict 取值构造（未知键忽略，缺键用默认值）。

    刻意不做嵌套 dataclass 的递归 —— 嵌套节在各自的 `_parse_*` 里显式构造，
    这样 YAML 写错层级时（把 `momentum` 写到 `futures` 外面）能被看见，
    而不是被一个"万能递归"悄悄吞掉。
    """
    import dataclasses

    kwargs: dict[str, Any] = {}
    for item in dataclasses.fields(cls):
        if item.name in raw:
            kwargs[item.name] = raw[item.name]
    return cls(**kwargs)


def parse(raw: dict[str, Any], *, source_path: str = "") -> MainlineConfig:
    """把 YAML dict 解析成 `MainlineConfig`（纯函数，便于单测）。"""
    cfg = MainlineConfig(source_path=source_path)
    synth = _section(raw, "synthesis")
    if synth:
        cfg.synthesis = _build(SynthesisConfig, synth)
        # V1.0 的 `base_weights`（六维40/五维35/龙头25）**刻意不迁移数值**。
        # 那组权重编码的正是 V2.0 要修掉的缺陷（三层加权相加把资金流、股东户数、
        # 成交量重复加权 2-3 倍）。把 40:35 按比例搬过来等于把缺陷带进新口径，
        # 而且搬完还"看起来是配置驱动的"。所以改用**代码默认的层权重**
        # （V2.3 = 六维 100 / 建仓痕迹 0，见 `SynthesisConfig`）。
        if "base_weights" in synth and "layer_weights" not in synth:
            default_layers = SynthesisConfig().normalized_layers()
            logger.warning(
                "检测到 V1.0 的 synthesis.base_weights，已按当前口径改用"
                "默认层权重 %s（不迁移旧比例）", default_layers)

    funnel = _section(raw, "funnel")
    if funnel:
        cfg.funnel = _build(FunnelConfig, funnel)

    six = _section(raw, "six_dim")
    if six:
        base = SixDimConfig()
        # ⚠️ 这里必须把**每一个**字段显式传进来：`SixDimConfig(...)` 是整体替换，
        # 漏掉的字段会静默退回 dataclass 默认值 —— 之前 `reverse_dims` 就是这样
        # 被 YAML 忽略的（默认值恰好与 YAML 一致，所以一直没被发现）。
        cfg.six_dim = SixDimConfig(
            weights=dict(six.get("weights") or base.weights),
            windows={**base.windows, **(six.get("windows") or {})},
            reverse_dims=tuple(six.get("reverse_dims") or base.reverse_dims),
            prosperity_delta_below=float(
                six.get("prosperity_delta_below", base.prosperity_delta_below)),
            prosperity_delta_window=int(
                six.get("prosperity_delta_window", base.prosperity_delta_window)))

    chip = _section(raw, "chip")
    if chip:
        cfg.chip = _build(ChipConfig, chip)

    macro = _section(raw, "macro")
    if macro:
        cfg.macro = _build(MacroConfig, macro)

    etf = _section(raw, "etf")
    if etf:
        cfg.etf = _build(EtfConfig, etf)

    pool = _section(raw, "pool")
    if pool:
        cfg.pool = _build(MemberPoolConfig, pool)

    relevance = _section(raw, "relevance")
    if relevance:
        cfg.relevance = _build(RelevanceConfig, relevance)
        # `_build` 只按字段名取值、不做类型转换，而 YAML 里 `3.0e9` 这类
        # 科学计数法会被解析成**字符串**（PyYAML 的 float 解析器不认 `3.0e9`，
        # 只认 `3.0e+9`），比较时会直接抛 TypeError。这里显式转一次。
        cfg.relevance.min_total_mv = _as_float(
            relevance.get("min_total_mv"), 3_000_000_000.0)
        cfg.relevance.corr_weight = _as_float(
            relevance.get("corr_weight"), 0.6)
        cfg.relevance.top_themes = int(_as_float(
            relevance.get("top_themes"), 3))
        cfg.relevance.corr_window = int(_as_float(
            relevance.get("corr_window"), 240))
        cfg.relevance.max_candidates = int(_as_float(
            relevance.get("max_candidates"), 20))
        cfg.relevance.concurrency = int(_as_float(
            relevance.get("concurrency"), 16))
        cfg.relevance.enabled = bool(relevance.get("enabled", True))
        cfg.relevance.exclude_st = bool(relevance.get("exclude_st", True))
        # ⚠️ 默认 **False**：现行 `ml_member_pure` 就是"未否决"的产物
        #    （Task 1 实测按预设判据回退，见 `RelevanceConfig.business_veto`）。
        #    这里用 `bool(...)` 显式转一次，防止 YAML 写成字符串 `"false"` 时
        #    被当成真值（非空字符串恒为 True）。
        cfg.relevance.business_veto = bool(
            relevance.get("business_veto", False))
        kinds = relevance.get("kinds")
        if isinstance(kinds, (list, tuple)) and kinds:
            cfg.relevance.kinds = [str(item) for item in kinds]

    # 第二层：V2.0 叫 accumulation；同时接受 V1.0 的 five_dim 键名做迁移
    acc_raw = _section(raw, "accumulation") or _section(raw, "five_dim")
    if acc_raw:
        declared = dict(acc_raw.get("weights") or {})
        defaults = AccumulationConfig().weights
        # 逐维取：YAML 里写了的用它，没写的用默认值。
        # 这样**给 DIMS 新增一个维度时不必改每个旧 YAML** —— 旧文件里的
        # `etf` 缺失会自动拿到默认权重，而不是静默变成 0（权重 0 意味着
        # "这一维永不参与打分"，与"没配置"完全不是一回事）。
        # V1.0 已删除的维度（capital_strength / chip_concentration）不在 DIMS 里，
        # 自然被丢掉 —— 迁移不会把重复加权的缺陷带进 V2.0。
        cfg.accumulation = AccumulationConfig(
            weights={key: (declared[key] if declared.get(key) is not None
                           else defaults[key])
                     for key in AccumulationConfig.DIMS},
            leverage=_build(LeverageConfig, _section(acc_raw, "leverage")),
            northbound=_build(NorthboundConfig, _section(acc_raw, "northbound")),
            volume_price=_build(VolumePriceConfig,
                                _section(acc_raw, "volume_price")))

    leader = _section(raw, "leader")
    if leader:
        cfg.leader = LeaderConfig(
            top_n=int(_as_float(leader.get("top_n"), 3)),
            capital_window=int(_as_float(leader.get("capital_window"), 5)),
            momentum_window=int(_as_float(leader.get("momentum_window"), 10)),
            volume_window=int(_as_float(leader.get("volume_window"), 5)),
            resonance_ratio=_as_float(leader.get("resonance_ratio"), 0.30),
            bonus_min=_as_float(leader.get("bonus_min"), 15.0),
            bonus_max=_as_float(leader.get("bonus_max"), 20.0),
            seat_counts_as_resonance=bool(
                leader.get("seat_counts_as_resonance", True)),
            seat=_build(SeatConfig, _section(leader, "seat")))

    alert = _section(raw, "alert")
    if alert:
        cfg.alert = _build(AlertRuleConfig, alert)

    # 告警范围限制来自**独立文件**（业务取舍的台账，带理由与日期）。
    # 读不到就留空名单并在 `load_note` 里写明 —— 静默变成"没有限制"是危险的：
    # 用户以为关掉的板块会重新开始告警。
    cfg.alert_scope = _load_alert_scope(cfg.alert_scope.path)

    backtest = _section(raw, "backtest")
    if backtest:
        cfg.backtest = _build(BacktestConfig, backtest)
        scenes = backtest.get("scenes")
        if isinstance(scenes, list):
            cfg.backtest.scenes = [item for item in scenes
                                   if isinstance(item, dict)]

    futures = _section(raw, "futures")
    if futures:
        cfg.futures = FuturesConfig(
            enabled=bool(futures.get("enabled", True)),
            mapping_file=str(futures.get("mapping_file")
                             or "mainline_futures_mapping.yaml"),
            momentum=_build(MomentumSignalConfig,
                            _section(futures, "momentum")),
            correlation=_build(CorrelationSignalConfig,
                               _section(futures, "correlation")),
            open_interest=_build(OpenInterestConfig,
                                 _section(futures, "open_interest")),
            term_structure=dict(futures.get("term_structure")
                                or {"enabled": True}),
            alerts=_build(FuturesAlertConfig, _section(futures, "alerts")),
            backtest=_build(FuturesBacktestConfig,
                            _section(futures, "backtest")),
            recalibration=_build(RecalibrationConfig,
                                 _section(futures, "recalibration")))
        exchanges = futures.get("exchanges")
        if isinstance(exchanges, list) and exchanges:
            cfg.futures.exchanges = [str(item) for item in exchanges]

    profiles = _section(raw, "board_profiles")
    if profiles:
        default_raw = _section(profiles, "default")
        parsed: dict[str, BoardProfile] = {}
        for key, value in profiles.items():
            if key in ("default", "board_overrides") or not isinstance(value, dict):
                continue
            parsed[key] = BoardProfile(
                key=key, label=str(value.get("label") or key),
                decay_days=int(_as_float(value.get("decay_days"), 20)))
        cfg.board_profiles = BoardProfilesConfig(
            default=BoardProfile(
                "default", str(default_raw.get("label") or "通用"),
                int(_as_float(default_raw.get("decay_days"), 20))),
            profiles=parsed,
            overrides={key: [str(word) for word in (value or [])]
                       for key, value in (
                           _section(profiles, "board_overrides")).items()
                       if isinstance(value, list)})

    data = _section(raw, "data")
    if data:
        cfg.data = _build(DataConfig, data)

    universe = _section(raw, "universe")
    if universe:
        cfg.universe = _build(UniverseConfig, universe)
        span = universe.get("sw_l1_range")
        if isinstance(span, (list, tuple)) and len(span) == 2:
            cfg.universe.sw_l1_range = (str(span[0]), str(span[1]))

    notify = _section(raw, "notify")
    if notify:
        cfg.notify = _build(NotifyConfig, notify)

    cfg.disclaimer = str(raw.get("disclaimer") or "").strip()
    return cfg


# ==================================================================
# 加载（mtime 热重载）
# ==================================================================

_CACHE: dict[str, Any] = {"mtime": None, "path": "", "config": None}


def load_config(path: str | Path | None = None, *, force: bool = False
                ) -> MainlineConfig:
    """加载配置；文件 mtime 变化时自动重载。

    文件缺失/解析失败**不抛错**：回落到内置默认值并在 `load_error` 里写明原因。
    理由是这个模块挂在资金流监控页签上 —— 一个 YAML 缩进错误不该让整页 500。
    """
    target = Path(path) if path else CONFIG_PATH
    missing = ""
    try:
        mtime = target.stat().st_mtime
    except OSError as exc:
        mtime = None
        missing = f"配置文件不可读：{brief(exc, BRIEF_TIGHT)}"

    if (not force and _CACHE["config"] is not None
            and _CACHE["mtime"] == mtime and _CACHE["path"] == str(target)):
        return _CACHE["config"]  # type: ignore[return-value]

    if mtime is None:
        config = MainlineConfig(source_path=str(target), load_error=missing)
        _CACHE.update(mtime=mtime, path=str(target), config=config)
        return config

    try:
        import yaml

        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("顶层结构必须是映射（key: value）")
        config = parse(raw, source_path=str(target))
    except Exception as exc:  # noqa: BLE001 配置损坏不该让页签 500
        logger.warning("主线挖掘配置解析失败（回落默认值）：%s",
                       brief(exc, BRIEF_TIGHT))
        config = MainlineConfig(
            source_path=str(target),
            load_error=f"配置解析失败：{brief(exc, BRIEF_TIGHT)}")

    _CACHE.update(mtime=mtime, path=str(target), config=config)
    return config


def load_yaml_config(name: str, *, base: str | Path | None = None
                     ) -> dict[str, Any] | None:
    """加载 `configs/` 下的附属 YAML（映射表 / 敏感度表）。

    返回 None 表示文件不存在或不可解析 —— 调用方据此在 `gaps` 里如实标注，
    而不是用一个空 dict 假装"表是空的"（两者的运维含义完全不同）。
    """
    root = Path(base) if base else PROJECT_ROOT / "configs"
    path = root / name
    if not path.exists():
        logger.info("主线挖掘附属配置不存在：%s", path)
        return None
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("主线挖掘附属配置解析失败 %s：%s", name,
                       brief(exc, BRIEF_TIGHT))
        return None
    return raw if isinstance(raw, dict) else None


def clear_config_cache() -> None:
    """清空热重载缓存（测试用：改完文件立刻生效，不依赖 mtime 精度）。"""
    _CACHE.update(mtime=None, path="", config=None)


__all__ = [
    "CONFIG_PATH",
    "PROJECT_ROOT",
    "AccumulationConfig",
    "AlertRuleConfig",
    "BacktestConfig",
    "BoardProfile",
    "BoardProfilesConfig",
    "ChipConfig",
    "DataConfig",
    "FunnelConfig",
    "FuturesConfig",
    "LeaderConfig",
    "LeverageConfig",
    "MacroConfig",
    "MainlineConfig",
    "NorthboundConfig",
    "NotifyConfig",
    "RelevanceConfig",
    "SeatConfig",
    "SixDimConfig",
    "SynthesisConfig",
    "VolumePriceConfig",
    "clear_config_cache",
    "load_config",
    "load_yaml_config",
    "parse",
]
