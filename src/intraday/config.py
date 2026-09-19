"""做T辅助模块配置加载（configs/intraday.yaml，mtime热重载 + 权重硬校验）。

为什么独立于 core.config：
- 本模块参数（因子权重/阈值/箱体指数/板块清单/自选池）是「策略参数」而非「环境配置」，
  放在 yaml 里便于版本化与调参，且不需要重启进程（按 mtime 自动重载）；
- 权重合计必须恰好=100，否则总分刻度失去可比性（±20/±30 阈值将失效）→ 加载即校验。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from src.core.errors import (
    BRIEF_LOG,
    brief,
)
from src.core.exceptions import ConfigError
from src.intraday.weight_profiles import (
    DAILY_FACTOR_ORDER,
    INTRADAY_FACTOR_ORDER,
)

logger = logging.getLogger(__name__)

#: 做T模块配置路径。
#:
#: ⚠️ **必须是绝对路径**：早期写成 `Path("configs/intraday.yaml")`（CWD 相对），
#: 服务/脚本一旦不在仓库根启动，`target.exists()` 为假 → 只打一条 WARNING
#: 就回退内置默认值，**用户改的权重与阈值静默失效**（这类问题极难排查）。
#: 同时支持 `INTRADAY_CONFIG` 环境变量覆盖（与 auction_select / sector_crowding
#: 两个模块的约定保持一致）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: str = os.environ.get(
    "INTRADAY_CONFIG", str(_PROJECT_ROOT / "configs" / "intraday.yaml"))

# 因子键 → 中文标签（前端表格与明细统一口径）
FACTOR_LABELS: dict[str, str] = {
    "box": "箱体/压力位",
    "vwap": "VWAP偏离",
    "boll": "布林带",
    "macd": "MACD",
    "kdj_rsi": "KDJ/RSI",
    "sentiment": "市场情绪",
    "news": "消息面",
    "index_volume": "指数量能",
    "board_rank": "板块涨幅排行",
    "overseas": "海外映射",
    # ---- 交易技能库贡献的四因子（2026-09-16 接入）----
    "chan": "缠论结构",
    "chip": "筹码量能结构",
    "cycle": "市场情绪周期",
    "character": "股性适配",
}

# 日线做T（日K级别）的因子标签
DAILY_FACTOR_LABELS: dict[str, str] = {
    "trend": "趋势结构",
    "chan_daily": "缠论买卖点",
    "volume": "量价结构",
    "position": "位置与身位",
    "signal_rule": "规则信号质量",
    "cycle": "市场情绪周期",
    "character": "股性适配",
}


class Weights(BaseModel):
    """分时做T十四因子权重（合计必须=100）。

    演变史（每一版都保持「合计=100、总分刻度 [-100,100]、±20/±30 阈值语义不变」）：
      v1 七因子（箱体30/VWAP20/布林15/…）
      v2 十因子：原七因子 ×0.8，新增指数量能8/板块排行6/海外映射6
      v3 十四因子（当前）：原十因子 ×0.71（17/11/8/6/6/6/3/6/4/4 = 71），
         新增缠论结构12/筹码量能8/情绪周期6/股性适配3 = 29

    为什么要重新配平而不是「新因子默认 0、用户自己开」：
    技能库四因子（缠论/筹码/情绪周期/股性）承载的是用户明确要求接入的判断维度，
    默认 0 等于接了个空。想回到旧口径的用户在前端一键切 `legacy` 模板即可
    （那里四因子为 0，其余十项恢复成 v2 的 24/16/12/8/8/8/4/8/6/6）。
    """

    box: float = 17
    vwap: float = 11
    boll: float = 8
    macd: float = 6
    kdj_rsi: float = 6
    sentiment: float = 6
    news: float = 3
    index_volume: float = 6
    board_rank: float = 4
    overseas: float = 4
    chan: float = 12
    chip: float = 8
    cycle: float = 6
    character: float = 3

    @model_validator(mode="after")
    def _sum_is_100(self, info: ValidationInfo) -> Weights:
        # `allow_partial=True` 是**个股微调专用**通道：`overrides.XXXXXX.weights`
        # 是稀疏差分（只写要覆盖的键），合并后合计未必=100，
        # 而全局配置的合计必须严格=100（总分刻度依赖它）。
        # 用校验上下文区分这两条路径，而不是把校验整个去掉 ——
        # 去掉就等于「全局权重写错了也没人拦」。
        if (info.context or {}).get("allow_partial"):
            return self
        total = sum(self.as_dict().values())
        if abs(total - 100.0) > 1e-6:
            raise ConfigError(
                f"intraday.yaml 权重合计必须为100，当前={total:g}"
                "（总分刻度依赖权重合计，否则±20/±30阈值失效）。"
                "注意：本版本已从10个维度扩展到14个维度，"
                "若你只改了原有的10个权重，请同时写上新增的 "
                "chan(默认12) / chip(默认8) / cycle(默认6) / character(默认3)；"
                "想完全回到旧口径可直接写 legacy 模板的数值 "
                "(box24 vwap16 boll12 macd8 kdj_rsi8 sentiment8 news4 "
                "index_volume8 board_rank6 overseas6 + 新增四项全0)"
            )
        return self


    @model_validator(mode="after")
    def _matches_catalog(self) -> Weights:
        """字段集合必须与因子目录一致 —— 目录加因子而这里忘记加，直接报错。"""
        missing = set(INTRADAY_FACTOR_ORDER) - set(self.model_dump())
        extra = set(self.model_dump()) - set(INTRADAY_FACTOR_ORDER)
        if missing or extra:
            raise ConfigError(
                f"Weights 字段与因子目录不一致：缺 {sorted(missing)}，多 {sorted(extra)}"
                "（请同步 src/intraday/weight_profiles.py 的 INTRADAY_FACTOR_ORDER）")
        return self

    def as_dict(self) -> dict[str, float]:
        return {key: float(getattr(self, key)) for key in INTRADAY_FACTOR_ORDER}


class DailyWeights(BaseModel):
    """日线做T（日K级别）七因子权重（合计必须=100）。

    与分时 `Weights` 分开的理由：两者的因子集合与语义完全不同 ——
    分时回答「今天在哪一档动手」，日线回答「这只票现在该不该做这一轮滚动」。
    强行共用一套权重只会让两边都失去可解释性。
    """

    trend: float = 20
    chan_daily: float = 18
    volume: float = 16
    position: float = 14
    signal_rule: float = 12
    cycle: float = 10
    character: float = 10

    @model_validator(mode="after")
    def _sum_is_100(self, info: ValidationInfo) -> DailyWeights:
        if (info.context or {}).get("allow_partial"):
            return self
        total = sum(self.as_dict().values())
        if abs(total - 100.0) > 1e-6:
            raise ConfigError(
                f"日线做T权重合计必须为100，当前={total:g}；"
                f"字段：{', '.join(DAILY_FACTOR_ORDER)}")
        return self

    def as_dict(self) -> dict[str, float]:
        return {key: float(getattr(self, key)) for key in DAILY_FACTOR_ORDER}



class Thresholds(BaseModel):
    action: float = 30.0
    hint: float = 20.0

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.hint <= 0 or self.action <= self.hint:
            raise ConfigError(
                f"阈值须满足 0 < 提示线({self.hint}) < 动手线({self.action})")
        return self


class BoxParams(BaseModel):
    lookback_days: int = Field(default=20, ge=5, le=250)
    exponent: float = Field(default=2.5, gt=0.0, le=6.0)


class VwapParams(BaseModel):
    z_scale: float = Field(default=3.0, gt=0.0, le=10.0)


class BollParams(BaseModel):
    window: int = Field(default=20, ge=5, le=120)
    num_std: float = Field(default=2.0, gt=0.0, le=4.0)
    squeeze_percentile: float = Field(default=0.2, ge=0.0, le=1.0)
    squeeze_damp: float = Field(default=0.6, ge=0.0, le=1.0)


class TimeframeWeights(BaseModel):
    intraday: float = 0.6
    daily: float = 0.4

    def normalized(self) -> tuple[float, float]:
        total = self.intraday + self.daily
        if total <= 0:
            return 0.5, 0.5
        return self.intraday / total, self.daily / total


class MacdParams(BaseModel):
    fast: int = Field(default=12, ge=2, le=100)
    slow: int = Field(default=26, ge=3, le=200)
    signal: int = Field(default=9, ge=2, le=100)
    gap_scale_pct: float = Field(default=0.008, gt=0.0, le=0.1)
    cross_bonus: float = Field(default=0.15, ge=0.0, le=1.0)
    tf_weights: TimeframeWeights = Field(default_factory=TimeframeWeights)

    @model_validator(mode="after")
    def _fast_lt_slow(self) -> MacdParams:
        if self.fast >= self.slow:
            raise ConfigError(f"MACD 参数需 fast({self.fast}) < slow({self.slow})")
        return self


class KdjRsiParams(BaseModel):
    kdj_n: int = Field(default=9, ge=3, le=100)
    rsi_n: int = Field(default=14, ge=2, le=100)
    rsi_scale: float = Field(default=40.0, gt=0.0, le=100.0)
    tf_weights: TimeframeWeights = Field(
        default_factory=lambda: TimeframeWeights(intraday=0.5, daily=0.5))


class SentimentParams(BaseModel):
    breadth_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    rs_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    rs_scale_pct: float = Field(default=2.0, gt=0.0, le=20.0)


class NewsParams(BaseModel):
    llm_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    count_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    count_saturation: float = Field(default=3.0, gt=0.0, le=10.0)
    max_items: int = Field(default=10, ge=1, le=50)
    # 消息面 LLM 任务层级：light=本地 qwen2.5:1.5b-instruct（非推理模型，主）
    # / deepseek-flash（备）。
    # 为什么默认 light 而不是 medium（实测 2026-09-15，同一批新闻）：
    #   light  : 1.1~1.2s，稳定产出合法JSON，2/2 成功
    #   medium : 72~85s（qwen3:8b 是推理模型，思维链极长），
    #            且偶发把 max_tokens 全耗在思维链上、正文一个字都不输出
    # 新闻情绪是「短分类任务」，1.5b-instruct + JSON mode 足够；权重只有5，
    # 为它付出 80 秒延迟完全不值得。需要更深的语义判断时可改回 medium。
    news_task_tier: str = Field(default="light")
    # 内容为空/非JSON时的重试次数（推理模型偶发把 max_tokens 全用在思维链上，
    # 正文一个字都不输出；重试一次即可拿到合法JSON，实测单次失败率约 1/2）
    llm_retries: int = Field(default=1, ge=0, le=3)
    # 单条新闻喂给模型的正文截断长度（越短思维链越短，越不容易耗尽token）
    item_text_chars: int = Field(default=180, ge=40, le=1000)
    # 新闻内容未变时是否复用上次打分（**省 LLM 调用**）。
    # 实测依据（data/audit/llm_audit.jsonl）：做T消息面 278 次调用只有 28 个不同
    # prompt（重复率 90%，最热的一个被推理 97 次），其中 161 次走本地 qwen3:8b、
    # 中位 53.7 秒/次 —— 全是白烧算力。开启后：TTL 到期照常抓一次新闻（本来也要抓），
    # 内容指纹没变就直接复用，不调 LLM。
    reuse_when_unchanged: bool = True


class IndexVolumeParams(BaseModel):
    """指数量能因子参数。"""

    # 指数涨跌幅满分刻度（%）：±1% 给方向满分
    change_scale_pct: float = Field(default=1.0, gt=0, le=10)
    # 量能比满分刻度：量能 ±50% 给量能强度满分
    volume_scale: float = Field(default=0.5, gt=0, le=5)


class BoardRankParams(BaseModel):
    """板块涨幅排行因子参数。"""

    rank_weight: float = Field(default=0.6, ge=0, le=1)
    change_weight: float = Field(default=0.4, ge=0, le=1)
    # 板块涨跌幅满分刻度（%）
    change_scale_pct: float = Field(default=2.0, gt=0, le=20)


class OverseasParams(BaseModel):
    """海外映射因子参数。"""

    # 映射涨跌幅满分刻度（%）：加权均涨跌 ±3% 给满分
    scale_pct: float = Field(default=3.0, gt=0, le=20)
    # 美股隔夜分与韩股盘中同步分的合成权重
    overnight_weight: float = Field(default=0.75, ge=0, le=1)
    intraday_weight: float = Field(default=0.25, ge=0, le=1)
    # 单条映射权重（未在 weights 中单独配置时使用）
    default_weight: float = Field(default=1.0, gt=0, le=10)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "usNVDA": 1.5, "usTSM": 1.2, "usMU": 1.0, "usASX": 1.0,
            "usGOOGL": 0.5, "usINTC": 0.5, "usORCL": 0.5, "usGLW": 0.8,
            "kr000660": 1.0,
        },
        description="各海外标的在映射打分中的权重（代码 → 权重）")


class ChanParams(BaseModel):
    """缠论结构因子参数（笔中枢位置 + MACD背驰）。"""

    # 构建结构所需的最少K线数（含包含处理前）
    min_bars: int = Field(default=30, ge=10, le=500)
    # 笔的分型之间最少间隔的原始K线数：4 = 新笔口径（老笔是5）
    min_gap: int = Field(default=4, ge=3, le=10)
    # 现价在最后一个中枢中的位置超过该倍数（1+overshoot）视为"离开中枢"，
    # 此时位置分饱和，不再随价格继续外扩 —— 否则强势突破股会被判成极端超买
    overshoot: float = Field(default=0.5, gt=0.0, le=3.0)
    # 背驰区间 MACD 面积比上限：创新高/新低但面积不足前一段的该比例 → 背驰
    area_ratio_max: float = Field(default=0.85, gt=0.0, le=1.0)
    # 背驰在总分中的占比（1 - 该值 = 中枢位置的占比）
    divergence_weight: float = Field(default=0.5, ge=0.0, le=1.0)


class ChipParams(BaseModel):
    """筹码/量能结构因子参数。"""

    # 量比满分刻度：今日预测量能 / 近5日均量 达到 1+该值 给满分
    volume_ratio_scale: float = Field(default=1.0, gt=0.0, le=5.0)
    # "高位"判定：现价在当日分时区间中的位置高于该值 → 该处的放量视为派发
    high_position: float = Field(default=0.65, ge=0.5, le=0.95)
    low_position: float = Field(default=0.35, ge=0.05, le=0.5)

    @model_validator(mode="after")
    def _ordered(self) -> ChipParams:
        if self.low_position >= self.high_position:
            raise ConfigError(
                f"ChipParams 需 low_position({self.low_position}) < "
                f"high_position({self.high_position})")
        return self


class CycleParams(BaseModel):
    """市场情绪周期因子参数。"""

    # 是否启用（关闭后该维度记为不可用，不参与打分与有效权重）
    enabled: bool = True
    # 情绪周期快照的缓存秒数（池子盘中实时变化，默认 60 秒足够跟手）
    ttl_seconds: int = Field(default=60, ge=0, le=3600)
    # 退潮期/冰点/触发一票否决时是否禁止正式做T信号（安全关键，默认开）
    veto_signals: bool = True
    # 温度→得分的中性点：温度 >= 该值时给正分（环境适合动手）
    neutral_temperature: float = Field(default=50.0, ge=0.0, le=100.0)


class LevelFitConfig(BaseModel):
    """关键价位**神经网络拟合**参数（做T档位从规则口径升级为"按这只票自己拟合"）。

    设计口径（与 `level_fit.py` 的实现一一对应）：

      - **输入 = 7 个客观维度**（用户点名的那 7 个）：筹码量能结构、箱体/压力位、
        缠论结构、VWAP偏离、布林带、MACD、KDJ/RSI；
      - **输出 = 三条价位线**（低吸/高抛/止损），以「ATR 倍数」为尺度；
      - **训练目标**：过去 `sessions`（默认 10）个交易日中，先触及**低吸线**、
        且在 `horizon_bars`（默认24根=2小时）内先到**高抛线**、且**全程不破止损**的
        比例（`target_hit_rate`，默认 0.80）；同时要求**够得着**（触及样本数下限）。
      - **一票一拟合**：每只票用自己的历史拟合，参数按 (代码, 交易日) 缓存。

    ⚠️ **80% 是"目标"而不是"承诺"**：它写成 `target_hit_rate` 是**闸门阈值**，
    只有留一日交叉验证（walk-forward）也达标才允许启用拟合档位；否则回退规则口径
    并把实测值如实报出来。样本不足时同样不启用 —— 480 根 5 分钟 bar 上把成功率
    做到 100% 太容易了，那是过拟合而不是能力。
    """

    # 总开关
    enabled: bool = True
    # 训练窗口（交易日）。取不到这么多时用实际可得的，并在结果里如实标注
    sessions: int = Field(default=10, ge=3, le=60)
    # 训练最少需要的交易日与 bar 数（不足则不做拟合，回退规则口径）
    min_sessions: int = Field(default=5, ge=2, le=30)
    min_bars: int = Field(default=200, ge=60, le=5000)
    # 前瞻窗口（bar 数）：24 根 5 分钟 bar = 2 小时（当日做T的合理持有上限）
    horizon_bars: int = Field(default=24, ge=2, le=96)
    # 目标成功率：既是优化目标，也是**启用闸门**（walk-forward 不达标就不启用）
    target_hit_rate: float = Field(default=0.80, ge=0.3, le=1.0)
    # 判定"够得着"的最少触及样本数：样本太少时成功率没有统计意义
    min_touch_samples: int = Field(default=20, ge=5, le=2000)
    # 一轮做T的双边摩擦成本（%）：低吸~高抛的价差至少要覆盖它，否则拟合会
    # 收敛到"线挨着线、天天触发但全是手续费"
    round_trip_cost_pct: float = Field(default=0.20, ge=0.0, le=2.0)
    # 三条线的搜索网格（分位数，基于该票自己的 |波动| 经验分布）
    low_quantiles: list[float] = Field(default_factory=lambda: [0.05, 0.10, 0.15, 0.20, 0.30])
    high_quantiles: list[float] = Field(default_factory=lambda: [0.70, 0.80, 0.85, 0.90, 0.95])
    stop_quantiles: list[float] = Field(default_factory=lambda: [0.97, 0.985, 0.995])
    # 神经网络本身（刻意小：单票样本量只有几百根 bar）
    hidden: int = Field(default=16, ge=2, le=64)
    epochs: int = Field(default=300, ge=10, le=3000)
    learning_rate: float = Field(default=0.02, gt=0, le=1.0)
    l2: float = Field(default=0.01, ge=0.0, le=1.0)
    seed: int = Field(default=20260917, ge=0, le=2**31 - 1)
    # 拟合档位的权重（0=完全不用，1=完全替换规则线）：默认 1.0 = 拟合为主
    blend: float = Field(default=1.0, ge=0.0, le=1.0)
    # 单次拟合的墙钟上限（秒）：超时就放弃本次拟合（回退规则口径），不拖慢面板
    timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    # 结果缓存秒数（拟合结果按交易日有效，盘中不必重算）
    cache_seconds: float = Field(default=1800.0, ge=0, le=86400)
    # 快照接口是否在**缓存未命中**时同步拟合一次。
    # 默认 True（用户要求"默认参数值，每个个股都要拟合"）；若面板首屏想更快，
    # 可置 False —— 那时只有 `/intraday/level-fit` 接口会触发拟合。
    fit_on_snapshot: bool = True


class CharacterParams(BaseModel):
    """股性适配因子参数。"""
    # 股性画像取样的日线根数
    sample_days: int = Field(default=250, ge=60, le=1000)
    # 画像所需的最少根数（不足即记为不可用）
    min_sample_days: int = Field(default=60, ge=30, le=500)
    # 偏离分母的 ATR 倍数：现价相对 VWAP 偏离达到 该倍数×日ATR 时给满分。
    # 0.35 ≈ 半日波动量级 —— 再大的偏离通常属于趋势日，不该指望回归。
    atr_scale: float = Field(default=0.35, gt=0.0, le=5.0)
    # 是否按股性自动推荐权重模板（前端「按股性推荐」按钮的数据来源）
    suggest_weights: bool = True


class FactorParams(BaseModel):
    box: BoxParams = Field(default_factory=BoxParams)
    vwap: VwapParams = Field(default_factory=VwapParams)
    boll: BollParams = Field(default_factory=BollParams)
    macd: MacdParams = Field(default_factory=MacdParams)
    kdj_rsi: KdjRsiParams = Field(default_factory=KdjRsiParams)
    sentiment: SentimentParams = Field(default_factory=SentimentParams)
    news: NewsParams = Field(default_factory=NewsParams)
    index_volume: IndexVolumeParams = Field(default_factory=IndexVolumeParams)
    board_rank: BoardRankParams = Field(default_factory=BoardRankParams)
    overseas: OverseasParams = Field(default_factory=OverseasParams)
    chan: ChanParams = Field(default_factory=ChanParams)
    chip: ChipParams = Field(default_factory=ChipParams)
    cycle: CycleParams = Field(default_factory=CycleParams)
    character: CharacterParams = Field(default_factory=CharacterParams)
    level_fit: LevelFitConfig = Field(default_factory=LevelFitConfig)



class DailyParams(BaseModel):
    """日K级别做T（量价体系）参数——对应需求 §9「参数表（回测可调）」。"""

    # 高量比较天数
    hv_lookback: int = Field(default=3, ge=1, le=10)
    # 大阴/大阳线幅度阈值（%）
    big_bar_pct: float = Field(default=7.0, gt=0, le=20)
    # 宽幅K线振幅阈值（%，对应「放量大阴大阳(宽幅)」）
    wide_amplitude_pct: float = Field(default=10.0, gt=0, le=30)
    # 小阴小阳振幅上限（%）
    small_amplitude_pct: float = Field(default=3.0, gt=0, le=10)
    # 缩倍量比例
    shrink_ratio: float = Field(default=0.5, gt=0, le=1)
    # 倍量比例
    double_ratio: float = Field(default=2.0, ge=1, le=10)
    # 平量容差
    flat_ratio: float = Field(default=0.1, gt=0, le=0.5)
    # 长下影线/实体倍数
    tail_shadow_x: float = Field(default=2.0, ge=1, le=10)
    # 涨停低吸市值上限（亿元）
    limit_cap_yi: float = Field(default=300.0, gt=0, le=100000)
    # 涨停低吸换手率下限（%）
    turnover_min: float = Field(default=3.0, ge=0, le=100)
    # 涨停回调/新屠龙刀调整窗口（天）
    pullback_days_min: int = Field(default=3, ge=1, le=30)
    pullback_days_max: int = Field(default=5, ge=1, le=60)
    # 固定止损幅度（%）
    stop_loss_pct: float = Field(default=5.0, gt=0, le=30)
    # 均线共振级别
    ma_short: int = Field(default=5, ge=2, le=60)
    ma_long: int = Field(default=10, ge=3, le=120)
    ma_trend_short: int = Field(default=60, ge=20, le=250)
    ma_trend_long: int = Field(default=120, ge=30, le=500)
    # 日K分析回看天数
    lookback_days: int = Field(default=250, ge=60, le=2000)
    # 涨停判定容差（%）：收盘涨幅 ≥ (10% - 容差) 视为涨停（主板10cm）
    limit_up_tolerance: float = Field(default=0.3, ge=0, le=2)
    # 低吸：回调期间阴线跌破情绪释放点的判定容差
    emotion_tolerance_pct: float = Field(default=0.5, ge=0, le=5)

    @model_validator(mode="after")
    def _ordered(self) -> DailyParams:
        if self.ma_short >= self.ma_long:
            raise ConfigError(
                f"日均线参数需 ma_short({self.ma_short}) < ma_long({self.ma_long})")
        if self.pullback_days_min > self.pullback_days_max:
            raise ConfigError("pullback_days_min 不得大于 pullback_days_max")
        if self.wide_amplitude_pct <= self.small_amplitude_pct:
            raise ConfigError(
                f"宽幅阈值({self.wide_amplitude_pct}) 必须大于 小阴小阳上限"
                f"({self.small_amplitude_pct})")
        return self


class DataParams(BaseModel):
    intraday_period: str = "5m"
    daily_context_bars: int = Field(default=120, ge=30, le=2000)
    minute_cache_ttl: int = Field(default=45, ge=0, le=3600)
    # 自选池自动刷新：盘中每 N 秒把所有自选标的的分钟数据取到最新（0=关闭）。
    # 默认 60 秒 = 用户口径「每分钟刷新一次」；只在 09:15~11:30 与 13:00~15:00 生效。
    watchlist_refresh_seconds: int = Field(default=60, ge=0, le=3600)
    # 自选概览缓存秒数：刷新循环写入后前端每分钟取一次直接命中缓存，
    # 不会每个请求都把 N 只票重算一遍（非盘中自动放宽到 300 秒）。
    watchlist_cache_ttl: int = Field(default=60, ge=0, le=3600)
    # 报价快车道：现价/涨跌幅每 N 秒走一次**批量**快照（QMT 9 只实测 0.54ms），
    # 与上面的整表重算解耦 —— 价格跟手不需要把打分也拉起来。0 = 关闭。
    quote_refresh_seconds: int = Field(default=5, ge=0, le=3600)
    # 日K做T快照的进程内缓存秒数。前端按「数据源更新周期的 3 倍」轮询：
    # 日线bar在盘中由逐笔驱动、按分钟更新（QMT 订阅实测 1 秒内到位），
    # 3 × 60 = 180 秒，两边同值才不会一个刚取完另一个又重算。
    daily_snapshot_ttl: int = Field(default=180, ge=0, le=86400)
    # 板块快照缓存秒数：同花顺板块快照要在子进程里跑（隔离 py_mini_racer 原生崩溃），
    # 单次约 0.3~1s；WS 每 15s 推一次快照，不缓存等于每 15s 为每个板块起一个子进程。
    board_cache_ttl: int = Field(default=20, ge=0, le=600)
    daily_cache_ttl: int = Field(default=1800, ge=0, le=86400)
    valuation_cache_ttl: int = Field(default=3600, ge=0, le=86400)
    news_cache_ttl: int = Field(default=600, ge=0, le=86400)
    request_timeout: float = Field(default=12.0, gt=0.0, le=60.0)
    intraday_sources: list[str] = Field(
        default_factory=lambda: ["qmt", "tencent", "eastmoney", "sina"])
    # 数据源失败冷却（秒）：某源失败后在该窗口内直接跳过，不再逐个试探。
    # 没有冷却时「QMT未启动」会让每次快照都白等 4~5s 的 xtquant 连接超时，
    # 东财被阻断时还会走完整个分页重试（实测首个快照因此被拖到 54s）。
    # 与项目 ConnectorRouter 的失败冷却口径一致。
    source_cooldown_seconds: int = Field(default=300, ge=0, le=3600)


class SessionParams(BaseModel):
    call_auction: str = "09:25"
    morning_open: str = "09:30"
    morning_close: str = "11:30"
    afternoon_open: str = "13:00"
    afternoon_close: str = "15:00"


class LevelParams(BaseModel):
    stop_loss_pct: float = Field(default=1.0, gt=0.0, le=20.0)
    # 止损距离的**波动率自适应**倍数：最终距离取 max(低吸线×stop_loss_pct%, 该倍数×ATR)。
    # 实测（2026-09-16）：300308 日线 ATR=52.8（占价 5.8%），而固定 1% 的止损距离
    # 只有 9 元，任何噪声都能打穿 → 早盘低位反复报"已跌破止损"。
    # 0 = 只用固定百分比口径。
    atr_stop_mult: float = Field(default=0.5, ge=0.0, le=5.0)
    # 低吸线的**兜底距离**（×ATR）：当箱体下沿/布林下轨都跑到现价上方时（跳空高开、
    # 强势股上冲），低吸线按"现价下方 该倍数×ATR"给一个**真实可触及**的位置。
    # 为什么不能用"现价下方一个贴线带宽"当替身：贴线判定是 price ≤ low_buy×(1+band)，
    # 两者相乘≈现价，低吸信号会被彻底做死（实测当日 0 次触发）。
    dip_fallback_atr: float = Field(default=0.3, ge=0.0, le=3.0)
    take_profit_buffer_pct: float = Field(default=0.0, ge=-10.0, le=10.0)
    touch_band_pct: float = Field(default=0.3, gt=0.0, le=5.0)
    blend_boll_bands: bool = True
    # VWAP偏离极值：|z|≥该值时视为「价格触及关键档位」的等效条件
    # （对应需求「价格触及设定的箱体边界/VWAP极值」中的后者）
    vwap_extreme_z: float = Field(default=1.5, gt=0.0, le=6.0)
    # 低吸线~高抛线的最小档位差（占现价%）：
    # 做T一轮的双边成本约 佣金0.05% + 印花税0.05% + 滑点0.1% ≈ 0.2%，
    # 档位差若只有零点几%，信号再准也会被摩擦成本吃光；默认要求至少1.5%。
    min_band_pct: float = Field(default=1.5, ge=0.0, le=10.0)
    # 档位差上限：防止箱体过宽（如20日振幅25%）时低吸/高抛线离现价太远而永不触发
    max_band_pct: float = Field(default=8.0, gt=0.0, le=40.0)

    @model_validator(mode="after")
    def _band_ordered(self) -> LevelParams:
        if self.max_band_pct < self.min_band_pct:
            raise ConfigError(
                f"max_band_pct({self.max_band_pct}) 不得小于 min_band_pct({self.min_band_pct})")
        return self


class BoardConfig(BaseModel):
    name: str
    kind: Literal["concept", "industry"] = "concept"
    aliases: list[str] = Field(default_factory=list)
    # 该板块的默认海外映射：个股自身未配置 overseas 时按所属板块取用，
    # 这样「不在自选池里的股票」也能拿到海外映射因子（实测：不在池里就永远缺口）
    overseas: list[str] = Field(default_factory=list)

    @field_validator("overseas")
    @classmethod
    def _overseas_codes(cls, values: list[str]) -> list[str]:
        return _validate_overseas(values)


def _validate_overseas(values: list[str]) -> list[str]:
    """海外映射代码校验：必须是 us/kr 前缀的腾讯代码（禁止写A股代码）。"""
    cleaned: list[str] = []
    for value in values:
        symbol = str(value).strip()
        if not symbol:
            continue
        if not symbol.startswith(("us", "kr")) or len(symbol) < 4:
            raise ConfigError(
                f"海外映射代码须为 us*/kr* 腾讯代码（如 usNVDA、kr000660）: "
                f"{symbol!r}")
        cleaned.append(symbol)
    return cleaned


class WatchConfig(BaseModel):
    code: str
    name: str = ""
    boards: list[str] = Field(default_factory=list)
    industry: str = ""
    peers: list[str] = Field(default_factory=list)
    # 海外映射：与该股涨跌最相关的海外标的（腾讯代码，如 usNVDA / kr000660）
    # us*=美股（隔夜映射，驱动跳空）；kr*=韩股（与A股同时开市，盘中同步信号）
    overseas: list[str] = Field(default_factory=list)
    # 置顶：置顶的自选永远排在最前（用户要求，2026-09-17）。
    # 存在配置里而不是前端 localStorage：换浏览器/换机器应当保持一致。
    pinned: bool = False

    @field_validator("code")
    @classmethod
    def _tradeable_code(cls, value: str) -> str:
        """只接受本项目数据源能覆盖的沪深6位代码。

        北交所（8/4/920开头）当前所有连接器都不支持（exchange_symbol 会直接拒绝），
        若允许写入自选池，之后每次快照都会在取数层失败——与其让它悄悄烂在配置里，
        不如在写入时就明确拒绝。
        """
        code = str(value).strip()
        if not (code.isdigit() and len(code) == 6):
            raise ConfigError(f"自选标代码须为6位数字: {code!r}")
        if code.startswith(("8", "4", "920")):
            raise ConfigError(
                f"北交所标的暂不支持（数据源未覆盖）: {code}")
        return code

    @field_validator("peers")
    @classmethod
    def _tradeable_peers(cls, values: list[str]) -> list[str]:
        return [cls._tradeable_code(peer) for peer in values]

    @field_validator("overseas")
    @classmethod
    def _overseas_codes(cls, values: list[str]) -> list[str]:
        return _validate_overseas(values)


class NotifyConfig(BaseModel):
    enabled: bool = True
    push_solid_only: bool = True
    cooldown_minutes: int = Field(default=30, ge=0, le=1440)
    feishu_webhook_env: str = "FEISHU_WEBHOOK_URL"
    dingtalk_webhook_env: str = "DINGTALK_WEBHOOK_URL"
    wecom_webhook_env: str = "WECOM_WEBHOOK_URL"
    email_fallback: bool = True


class CodeOverride(BaseModel):
    """**个股微调**：只写要覆盖的项，其余沿用全局口径。

    为什么需要它：不同标的的脾气差得远。实测 300308（中际旭创）日线 ATR=52.8
    （占价 5.8%），而默认 `stop_loss_pct=1%` 的止损距离只有 9 元 —— 任何噪声都能
    打穿，早盘低位会反复报"已跌破止损"；而 600036（招商银行）ATR 只占 1.7%，
    1% 的止损就是合适的。用一套全局参数覆盖所有票，必然有一半是错的。

    用法（configs/intraday.yaml）：

        overrides:
          "300308":
            weights: {boll: 8.0, vwap: 20.0}      # 只覆盖这两个因子
            thresholds: {action: 35, hint: 25}    # 动手线/提示线
            levels:
              stop_loss_pct: 2.0
              atr_stop_mult: 0.8                  # 止损距离 = max(2%, 0.8×ATR)
              min_band_pct: 2.0                   # 低吸~高抛最小档位差

    字段名写错会**直接报配置错误**（不是静默忽略）：面板会显示"配置解析失败，
    已回退默认参数"，避免"改了却没生效"这种最难查的问题。
    """

    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    levels: dict[str, float] = Field(default_factory=dict)
    # 日线做T（日K级别）的七因子权重覆盖。与分时 weights 分开，
    # 因为两者因子集合不同；混用一套会让「日线该不该滚动」与「今天在哪一档动手」
    # 互相污染。
    daily_weights: dict[str, float] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.weights or self.thresholds or self.levels
                    or self.daily_weights)

    def describe(self) -> str:
        """人话描述（面板上显示"这只票改了什么"）。"""
        parts = []
        if self.weights:
            parts.append("分时权重 " + ", ".join(
                f"{key}={value:g}" for key, value in sorted(self.weights.items())))
        if self.daily_weights:
            parts.append("日线权重 " + ", ".join(
                f"{key}={value:g}"
                for key, value in sorted(self.daily_weights.items())))
        if self.thresholds:
            parts.append("阈值 " + ", ".join(
                f"{key}={value:g}" for key, value in sorted(self.thresholds.items())))
        if self.levels:
            parts.append("档位 " + ", ".join(
                f"{key}={value:g}" for key, value in sorted(self.levels.items())))
        return "；".join(parts)



class IntradayConfig(BaseModel):
    """做T辅助模块完整配置。"""

    version: int = 1
    weights: Weights = Field(default_factory=Weights)
    # 日线做T（日K级别）的七因子权重：与分时 weights 独立
    daily_weights: DailyWeights = Field(default_factory=DailyWeights)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    # 日线做T的阈值刻度比分时更粗：一天只有一根bar，
    # ±15/±25 之外的区间留给"确实有事"的情形，避免用户天天改主意。
    daily_thresholds: Thresholds = Field(
        default_factory=lambda: Thresholds(action=25.0, hint=15.0))
    factors: FactorParams = Field(default_factory=FactorParams)
    daily: DailyParams = Field(default_factory=DailyParams)
    data: DataParams = Field(default_factory=DataParams)
    session: SessionParams = Field(default_factory=SessionParams)
    levels: LevelParams = Field(default_factory=LevelParams)
    boards: list[BoardConfig] = Field(default_factory=list)
    watchlist: list[WatchConfig] = Field(default_factory=list)
    # 个股微调：键=6位代码，值=只覆盖需要改的项（见 CodeOverride）
    overrides: dict[str, CodeOverride] = Field(default_factory=dict)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    disclaimer: str = (
        "本模块输出为多因子量化打分的机械结果，不构成投资建议；"
        "做T为高风险短线操作，请自行承担全部交易风险。"
    )
    # 配置文件解析失败而回退内置默认值时的原因（非空表示**当前用的是默认参数**）。
    # 必须在接口里暴露：否则用户改了 weights/watchlist 却因一处笔误被静默忽略，
    # 面板照样出数，问题极难发现（实测踩过：name 写成纯数字导致整份配置失效）。
    load_error: str | None = None

    @model_validator(mode="after")
    def _validate_overrides(self) -> IntradayConfig:
        """个股微调的字段名必须存在 —— 写错就报错，不能静默忽略。"""
        known_weights = set(self.weights.model_dump())
        known_daily = set(self.daily_weights.model_dump())
        known_thresholds = set(self.thresholds.model_dump())
        known_levels = set(self.levels.model_dump())
        for code, override in self.overrides.items():
            if not (str(code).strip().isdigit() and len(str(code).strip()) == 6):
                raise ConfigError(f"overrides 的键必须是6位证券代码：{code!r}")
            for field, known in (("weights", known_weights),
                                 ("daily_weights", known_daily),
                                 ("thresholds", known_thresholds),
                                 ("levels", known_levels)):
                unknown = set(getattr(override, field)) - known
                if unknown:
                    raise ConfigError(
                        f"overrides.{code}.{field} 含未知字段 {sorted(unknown)}；"
                        f"可用字段：{sorted(known)}")
        return self

    def override_for(self, code: str) -> CodeOverride | None:
        """取该标的的个股微调（无则 None）。"""
        return self.overrides.get(str(code).strip())

    def for_code(self, code: str) -> IntradayConfig:
        """返回**这只票实际生效**的配置（应用个股微调；无微调时原样返回）。

        深拷贝后覆盖，避免把个股参数写回全局配置。

        ⚠️ 覆盖值**必须先过构造器校验**再进来：`model_copy` 在 pydantic v2 里
        不做校验，从数据库读出的未知字段名会被静默塞进模型，
        变成"改了却没生效"这种最难查的问题。调用方请用
        `IntradayConfig(overrides={code: ...}).for_code(code)` 的写法，
        或走 `validate_override()`。
        """
        override = self.override_for(code)
        if override is None or override.is_empty():
            return self
        return self.with_override(code, override)

    def with_override(self, code: str, override: CodeOverride) -> IntradayConfig:
        """用**已校验**的 `CodeOverride` 生成本票生效配置（不写回全局）。"""
        if override is None or override.is_empty():
            return self
        target = str(code).strip()
        updated: dict[str, Any] = {}
        partial = {"allow_partial": True}
        if override.weights:
            updated["weights"] = Weights.model_validate(
                {**self.weights.model_dump(), **dict(override.weights)},
                context=partial)
        if override.daily_weights:
            updated["daily_weights"] = DailyWeights.model_validate(
                {**self.daily_weights.model_dump(), **dict(override.daily_weights)},
                context=partial)
        if override.thresholds:
            updated["thresholds"] = Thresholds.model_validate(
                {**self.thresholds.model_dump(), **dict(override.thresholds)},
                context=partial)
        if override.levels:
            updated["levels"] = LevelParams.model_validate(
                {**self.levels.model_dump(), **dict(override.levels)},
                context=partial)
        _ = target  # 保留参数以对齐 for_code 的调用形态
        return self.model_copy(deep=True, update=updated)

    @staticmethod
    def validate_override(raw: dict[str, Any]) -> CodeOverride:
        """把外部来的原始字典（DB 行 / HTTP body）校验成 `CodeOverride`。

        未知字段直接抛 `ConfigError`（不静默忽略）——
        这条规则与 `_validate_overrides` 同源，只是提前到「写库/接口」这一层，
        这样坏数据根本进不了库、也影响不到打分。

        实现上刻意**借道构造一个探针 `IntradayConfig`**：字段名白名单只在
        `_validate_overrides` 里维护一份，复制一份到本函数必然漂移。
        探针用 `000000` 这个不会与真实标的冲突的代码。
        """
        override = CodeOverride(**raw)
        IntradayConfig(overrides={"000000": override})
        return override

    def with_score_patch(
        self,
        mode: str = "intraday",
        *,
        weights: dict[str, float] | None = None,
        thresholds: dict[str, float] | None = None,
        levels: dict[str, float] | None = None,
        daily_weights: dict[str, float] | None = None,
    ) -> IntradayConfig:
        """返回「套上一组临时覆盖」的配置副本（供前端**预览**用，不写回任何状态）。

        与 `with_override` 的区别：那一条是"这只票长期生效的口径"（会被持久化），
        这一条是"用户现在拖着滑杆想看会发生什么"（用完即弃）。
        两者共用同一套校验路径，避免预览过的参数保存后行为不一致。
        """
        partial = {"allow_partial": True}
        updated: dict[str, Any] = {}
        if mode == "daily":
            merged = {**self.daily_weights.model_dump(),
                      **{k: float(v) for k, v in (daily_weights or weights or {}).items()}}
            updated["daily_weights"] = DailyWeights.model_validate(
                merged, context=partial)
            if thresholds:
                updated["daily_thresholds"] = Thresholds.model_validate(
                    {**self.daily_thresholds.model_dump(), **thresholds},
                    context=partial)
            # 日线模式没有「低吸档位」，levels 只在分时口径下有意义
            return self.model_copy(deep=True, update=updated)
        merged_weights = {**self.weights.model_dump(),
                          **{k: float(v) for k, v in (weights or {}).items()}}
        updated["weights"] = Weights.model_validate(merged_weights, context=partial)
        if thresholds:
            updated["thresholds"] = Thresholds.model_validate(
                {**self.thresholds.model_dump(), **thresholds}, context=partial)
        if levels:
            updated["levels"] = LevelParams.model_validate(
                {**self.levels.model_dump(), **levels}, context=partial)
        return self.model_copy(deep=True, update=updated)


    def watch(self, code: str) -> WatchConfig | None:
        """按代码取自选配置（不存在返回 None）。"""
        for item in self.watchlist:
            if item.code == code:
                return item
        return None

    def board_names(self, code: str) -> list[str]:
        """该标的**已绑定**的关联板块名（仅自选池里显式声明过 boards 的标的）。

        未在自选池声明板块的标的返回空列表 —— 板块归属是「用户判断的关联板块」，
        数据源没有成分股接口可供反查。实测踩过：随口查一只招商银行，面板拿配置里
        第一个板块（PCB概念）当它的板块，于是「银行股按 PCB 板块涨幅排名打分」，
        板块情绪(8) + 板块排行(6) 共 14 分权重全被污染。
        未绑定时必须让这两个维度记为「不可用」并在 gaps 说明，而不是随便套一个板块。
        """
        item = self.watch(code)
        if item and item.boards:
            return list(item.boards)
        return []

    def boards_bound(self, code: str) -> bool:
        """该标的的关联板块是否已绑定（决定板块类维度能否参与打分）。"""
        return bool(self.board_names(code))

    def reference_board_names(self) -> list[str]:
        """全部配置板块名（仅供未绑定标的在面板上做「参考板块」展示，不参与打分）。"""
        return [b.name for b in self.boards]

    def board_config(self, name: str) -> BoardConfig | None:
        for board in self.boards:
            if board.name == name:
                return board
        return None

    def overseas_for(self, code: str) -> list[str]:
        """该标的的海外映射：个股配置 → 其所属板块的默认映射 → 空（记缺口）。

        只对**已在自选池里声明过板块**的标的回落到板块映射。
        不在自选池的股票无法得知其板块归属（数据源没有成分股接口），
        此时宁可返回空让人看到缺口，也不能随便套一个板块的映射 ——
        实测踩过：招商银行被套上 PCB 板块映射，于是「招行映射英伟达」这种
        毫无意义的组合会静默进入打分。
        """
        item = self.watch(code)
        if item is None:
            return []
        if item.overseas:
            return list(item.overseas)
        for name in item.boards:
            board = self.board_config(name)
            if board and board.overseas:
                return list(board.overseas)
        return []

    def replace_watchlist(self, items: list[WatchConfig]) -> IntradayConfig:
        """返回替换了自选池的配置副本（供前端加/删自选后即时生效）。"""
        return self.model_copy(update={"watchlist": items})

    def snapshot(self) -> dict[str, Any]:
        """本次打分口径快照（前端展示 + 结果可复现）。"""
        return {
            "weights": self.weights.as_dict(),
            "weights_sum": sum(self.weights.as_dict().values()),
            "daily_weights": self.daily_weights.as_dict(),
            "daily_weights_sum": sum(self.daily_weights.as_dict().values()),
            "thresholds": {
                "action": self.thresholds.action,
                "hint": self.thresholds.hint,
            },
            "daily_thresholds": {
                "action": self.daily_thresholds.action,
                "hint": self.daily_thresholds.hint,
            },
            "factor_params": self.factors.model_dump(),
            "daily_params": self.daily.model_dump(),
            "levels": self.levels.model_dump(),
            "intraday_period": self.data.intraday_period,
        }


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, IntradayConfig]] = {}


def load_intraday_config(
    path: str | Path | None = None, *, force: bool = False,
) -> IntradayConfig:
    """加载配置（按文件 mtime 热重载；文件缺失/损坏时回退内置默认值并告警）。"""
    target = Path(path or DEFAULT_CONFIG_PATH)
    with _cache_lock:
        if not target.exists():
            if force or str(target) not in _cache:
                logger.warning("做T配置缺失(%s)，使用内置默认参数", target)
                _cache[str(target)] = (0.0, IntradayConfig())
            return _cache[str(target)][1]
        mtime = target.stat().st_mtime
        hit = _cache.get(str(target))
        if hit is not None and not force and hit[0] == mtime:
            return hit[1]
        try:
            raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            config = IntradayConfig.model_validate(raw)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 配置写坏不应打断服务：回退默认并明确告警
            logger.error(
                "做T配置解析失败(%s)，**已回退内置默认参数**（你配置的权重/自选池"
                "在当前进程中不生效），请修正后重试: %s", target, exc)
            config = IntradayConfig(load_error=f"{target} 解析失败：{brief(exc, BRIEF_LOG)}")
        _cache[str(target)] = (mtime, config)
        logger.info(
            "做T配置已加载: %s（权重合计=%g，动手线=±%g，提示线=±%g，自选%d只）",
            target, sum(config.weights.as_dict().values()),
            config.thresholds.action, config.thresholds.hint,
            len(config.watchlist),
        )
        return config


def reset_config_cache() -> None:
    """测试用：清空配置缓存。"""
    with _cache_lock:
        _cache.clear()


# ==================== 自选池落盘（保留注释的外科式YAML改写） ====================

_WATCHLIST_HEADER = (
    "# ============ 自选做T标的 ============\n"
    "# peers：自选同业股票池（用于逐只 PE/PB 对比，经腾讯批量快照一次取回）\n"
    "# industry：巨潮行业分类名（用于自动取\"行业中位数PE\"作权威对标）\n"
)

# 受管段落标记：前端「加/删自选」只重写两个标记之间的内容。
# 用显式标记而不是「靠缩进猜段落边界」，是因为后者会吃掉紧邻下一段落的注释头
# （实测把 "# ==== 推送 ====" 一起删掉），且重复替换会不断累积注释头、不幂等。
_BEGIN_LINE = "# >>> intraday-watchlist:begin（本段由前端「加/删自选」自动维护）"
_END_LINE = "# <<< intraday-watchlist:end"
_BEGIN_MARK = ">>> intraday-watchlist:begin"
_END_MARK = "<<< intraday-watchlist:end"

_TOP_LEVEL_KEY_RE = None  # 延迟编译（保持模块导入轻量）


def _top_level_key_re():
    global _TOP_LEVEL_KEY_RE
    if _TOP_LEVEL_KEY_RE is None:
        import re

        # 顶格的非注释行（新段落开始）：如 "boards:" / "notify:" / "weights:"
        _TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")
    return _TOP_LEVEL_KEY_RE


def _yaml_scalar(value: str) -> str:
    """把字符串安全地写成 YAML 标量（总是加引号并转义）。

    实测踩过的坑：自动填的证券简称若恰好是纯数字（如 ETF 代码 588170），
    写成 `name: 588170` 会被 YAML 解析成 **int**，下次加载即校验失败 →
    整个做T模块静默回退到内置默认参数（用户的自选与权重全丢）。
    统一加双引号即可根治。
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def dump_watchlist_block(items: list[WatchConfig]) -> list[str]:
    """自选池 → 受管段落文本行（含begin/end标记，缩进2空格与仓库配置风格一致）。"""
    lines = [_BEGIN_LINE, *_WATCHLIST_HEADER.rstrip("\n").split("\n")]
    if not items:
        lines.append("watchlist: []")
    else:
        lines.append("watchlist:")
        for item in items:
            lines.append(f"  - code: {_yaml_scalar(item.code)}")
            lines.append(f"    name: {_yaml_scalar(item.name or item.code)}")
            boards = ", ".join(_yaml_scalar(b) for b in item.boards)
            lines.append(f"    boards: [{boards}]")
            if item.industry:
                lines.append(f"    industry: {_yaml_scalar(item.industry)}")
            peers = ", ".join(_yaml_scalar(p) for p in item.peers)
            lines.append(f"    peers: [{peers}]")
            if item.overseas:
                overseas = ", ".join(_yaml_scalar(o) for o in item.overseas)
                lines.append(f"    overseas: [{overseas}]")
            # 只在置顶时写这一行：不给所有条目都加 `pinned: false`，
            # 免得每次前端增删都重写一遍整段（diff 噪声大、也容易看出"没改却变了"）
            if item.pinned:
                lines.append("    pinned: true")
    lines.append(_END_LINE)
    return lines


def _fallback_section_end(lines: list[str], start: int) -> int:
    """旧格式（无标记）时推断 watchlist 段落结束行。

    遇到下一段落的顶格键即结束；若先遇到一段注释、且其后紧跟顶格键，
    则该注释属于**下一个**段落（如 "# ==== 推送 ===="），不能吞掉。
    """
    pattern = _top_level_key_re()
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if pattern.match(line):
            return index
        if line.startswith("#"):
            probe = index
            while probe < len(lines) and (
                    lines[probe].startswith("#") or not lines[probe].strip()):
                probe += 1
            if probe < len(lines) and pattern.match(lines[probe]):
                return index
    return len(lines)


def _fallback_section_start(lines: list[str], key_index: int) -> int:
    """把紧贴在 watchlist 之上的连续注释行纳入替换范围（它们属于本段说明）。"""
    head = key_index
    while head > 0 and lines[head - 1].startswith("#"):
        head -= 1
    return head


def replace_watchlist_section(text: str, items: list[WatchConfig]) -> str:
    """重写 YAML 中的自选段落，**保留文件其余内容与注释**，且**幂等**。

    为什么不用 yaml.safe_dump 重写整个文件：configs/intraday.yaml 里每个参数都带
    中文注释说明，整文件重写会把注释全部抹掉——那正是这份配置最有价值的部分。

    优先在受管标记之间替换；首次迁移（旧文件无标记）时用关键字+缩进推断段落边界，
    并在替换结果中写入标记，之后的每次编辑都是精确的。
    """
    lines = text.split("\n")
    block = dump_watchlist_block(items)

    begin = next((i for i, line in enumerate(lines) if _BEGIN_MARK in line), None)
    if begin is not None:
        end = next((i for i in range(begin + 1, len(lines))
                    if _END_MARK in lines[i]), None)
        if end is not None:
            merged = [*lines[:begin], *block, *lines[end + 1:]]
            result = "\n".join(merged)
            return result if result.endswith("\n") else result + "\n"

    key_index = next(
        (i for i, line in enumerate(lines) if line.startswith("watchlist:")), None)
    if key_index is None:
        merged = [*lines, *block]
    else:
        start = _fallback_section_start(lines, key_index)
        stop = _fallback_section_end(lines, key_index)
        merged = [*lines[:start], *block, *lines[stop:]]
    result = "\n".join(merged)
    return result if result.endswith("\n") else result + "\n"


def save_watchlist(
    items: list[WatchConfig], path: str | Path | None = None,
) -> IntradayConfig:
    """把自选池写回 configs/intraday.yaml 并刷新进程内配置缓存。

    安全性保证（写配置文件必须可回滚）：
      1. 先写临时文件，**对临时文件做完整校验**（解析 + 权重合计=100 等），
         校验通过才原子替换正式文件；校验失败则丢弃临时文件、原配置纹丝不动；
      2. 替换后清空配置缓存并重载，返回新配置对象。
    """
    target = Path(path or DEFAULT_CONFIG_PATH)
    original = target.read_text(encoding="utf-8") if target.exists() else (
        "version: 1\n"
        # 新建文件时的骨架：权重必须合计=100，且必须写全**全部**因子键 ——
        # 少写一项就会被 `Weights` 的目录一致性校验拦下（宁可这里报错，
        # 也不要静默用默认值填，否则用户看到的 yaml 与生效口径不一致）。
        "weights: {box: 17, vwap: 11, boll: 8, macd: 6, kdj_rsi: 6,"
        " sentiment: 6, news: 3, index_volume: 6, board_rank: 4, overseas: 4,"
        " chan: 12, chip: 8, cycle: 6, character: 3}\n"
        "thresholds: {action: 30, hint: 20}\n"
    )
    updated = replace_watchlist_section(original, items)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    try:
        load_intraday_config(temporary, force=True)  # 校验候选内容本身
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(target)
    reset_config_cache()
    return load_intraday_config(target, force=True)


def upsert_watch(
    item: WatchConfig, path: str | Path | None = None,
) -> IntradayConfig:
    """新增或更新一只自选标的（按代码去重，保留其余标的的顺序）。"""
    config = load_intraday_config(path)
    items = [existing for existing in config.watchlist
             if existing.code != item.code]
    items.append(item)
    return save_watchlist(items, path)


def remove_watch(code: str, path: str | Path | None = None) -> IntradayConfig:
    """从自选池移除一只标的；不存在时不报错（幂等）。"""
    config = load_intraday_config(path)
    items = [existing for existing in config.watchlist
             if existing.code != code]
    if len(items) == len(config.watchlist):
        return config
    return save_watchlist(items, path)
