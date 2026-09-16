"""因子目录与权重模板（分时做T / 日线做T 共用一份口径）。

为什么单独一个模块，而不是把模板塞进 `config.py`：
- `config.Weights` 需要在**导入期**就知道全部因子键（做合计=100 的硬校验），
  若把目录写在 config 里，`character.py`/API/前端要拿目录就得反过来 import config，
  形成环。这里保持「目录是纯数据、不 import 任何本项目模块」的单向依赖。
- 权重模板与因子目录是**用户可见的产品口径**（前端权重编辑器的下拉项、说明文案），
  它们不该跟 pydantic 校验、yaml 读写、mtime 热重载混在一个文件里。

三份数据：
  1. `FACTOR_META`     —— 每个因子的中文名/分组/适用模式/一句话公式/数据来源（前端展示）
  2. `TEMPLATES`       —— 命名权重配方（均衡/震荡/趋势/题材/低波动/原始口径）
  3. 归一化与合并工具   —— 用户在前端把某一项拖到 0 或 50 时，后台把整套缩放到合计 100

权重语义（全项目统一）：
  贡献分 = 因子得分(-1..1) × 权重；总分 = Σ贡献分 ∈ [-100, 100]。
  所以「权重合计必须=100」不是洁癖 —— 一旦合计变成 120，±20/±30 阈值就全部失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["intraday", "daily"]

# 归一化后允许的最小可见权重：低于该值的项在四舍五入时容易被压成 0，
# 前端滑杆也会抖（0.4 → 0 → 0.6 反复）。见 `normalize_weights` 的取整策略。
MIN_VISIBLE_WEIGHT = 0.5


@dataclass(frozen=True)
class FactorMeta:
    """单个因子的展示与契约信息（前端权重编辑器直接消费）。"""

    key: str
    label: str
    group: str
    mode: Mode
    default_weight: float
    formula: str
    source: str
    # 该因子是否来自「交易经验/技能库」而非纯技术指标 —— 前端会打一个「技能」角标，
    # 让用户一眼看出哪些维度是自己那套战法贡献的。
    from_skill: bool = False


FACTOR_GROUPS: dict[str, str] = {
    "price": "价格结构",
    "volume": "量能筹码",
    "momentum": "动量指标",
    "market": "市场环境",
    "stock": "个股特性",
    "trend": "趋势与位置",
}

# ==================================================================
# 分时做T 因子目录（14 个）
# ==================================================================

INTRADAY_FACTOR_ORDER: tuple[str, ...] = (
    "box", "vwap", "boll", "macd", "kdj_rsi", "sentiment", "news",
    "index_volume", "board_rank", "overseas",
    "chan", "chip", "cycle", "character",
)

INTRADAY_FACTOR_META: tuple[FactorMeta, ...] = (
    FactorMeta(
        key="box", label="箱体/压力位", group="price", mode="intraday",
        default_weight=17,
        formula="p = 现价在近N日箱体的位置 → -sign(p-0.5)·|2(p-0.5)|^2.5",
        source="日线高低点（动态支撑压力）"),
    FactorMeta(
        key="vwap", label="VWAP偏离", group="volume", mode="intraday",
        default_weight=11,
        formula="clip(-z / 3)，z = 现价相对当日分时均价的偏离率标准化值",
        source="当日分时（累计均价 + 成交量）"),
    FactorMeta(
        key="boll", label="布林带", group="price", mode="intraday",
        default_weight=8,
        formula="clip((0.5 - %B) × 2)，带宽收口时打 squeeze_damp 折",
        source="当日分钟K线"),
    FactorMeta(
        key="macd", label="MACD", group="momentum", mode="intraday",
        default_weight=6,
        formula="(DIF-DEA)/price 线性映射 + 新交叉加成，5分钟0.6 / 日线0.4",
        source="5分钟K线 + 日线"),
    FactorMeta(
        key="kdj_rsi", label="KDJ/RSI", group="momentum", mode="intraday",
        default_weight=6,
        formula="mean(clip((50-J)/50), clip((50-RSI)/40))，30分钟与日线各半",
        source="30分钟K线 + 日线"),
    FactorMeta(
        key="sentiment", label="市场情绪", group="market", mode="intraday",
        default_weight=6,
        formula="0.5×(2×板块涨跌家数占比-1) + 0.5×clip(相对板块强度/2%)",
        source="同花顺板块快照（涨跌家数）"),
    FactorMeta(
        key="news", label="消息面", group="stock", mode="intraday",
        default_weight=3,
        formula="0.6×LLM情绪分 + 0.4×clip(3×(偏多-偏空)/(偏多+偏空))",
        source="近24小时个股新闻 + 本地LLM"),
    FactorMeta(
        key="index_volume", label="指数量能", group="market", mode="intraday",
        default_weight=6,
        formula="方向 × (0.5 + 0.5×量能强度)；量能只放大方向，不单独给方向",
        source="所属指数快照 + 指数日线"),
    FactorMeta(
        key="board_rank", label="板块涨幅排行", group="market", mode="intraday",
        default_weight=4,
        formula="0.6×排名分位分 + 0.4×板块涨跌幅分",
        source="同花顺全A板块涨幅排名"),
    FactorMeta(
        key="overseas", label="海外映射", group="market", mode="intraday",
        default_weight=4,
        formula="美股隔夜0.75 + 韩股盘中同步0.25，按标的权重加权",
        source="腾讯美股/韩股快照"),
    # ---------------- 以下 4 个由交易技能库贡献 ----------------
    FactorMeta(
        key="chan", label="缠论结构", group="price", mode="intraday",
        default_weight=12, from_skill=True,
        formula="现价相对**最后一个笔中枢**的位置 + 顶/底背驰强度修正",
        source="分时+日线（包含处理→分型→笔→中枢→MACD背驰）"),
    FactorMeta(
        key="chip", label="筹码量能结构", group="volume", mode="intraday",
        default_weight=8, from_skill=True,
        formula="今日量能进度 × 放量位置（高位放量偏空/低位放量偏多）× 换手活跃度",
        source="当日分时量 + 日线换手率 + 量比"),
    FactorMeta(
        key="cycle", label="市场情绪周期", group="market", mode="intraday",
        default_weight=6, from_skill=True,
        formula="涨停家数/炸板率/最高连板 → 周期阶段 → 做T环境温度（含退潮期闸门）",
        source="东财涨停池/炸板池/跌停池（akshare，秒级）"),
    FactorMeta(
        key="character", label="股性适配", group="stock", mode="intraday",
        default_weight=3, from_skill=True,
        formula="-clip(偏离 / (该股自身日ATR × k))，按该股「震荡 vs 趋势」强度缩放",
        source="该股近120日振幅/ATR/涨停基因/趋势效率"),
)

# ==================================================================
# 日线做T 因子目录（7 个）—— 日K级别「该不该做这一轮」
# ==================================================================

DAILY_FACTOR_ORDER: tuple[str, ...] = (
    "trend", "chan_daily", "volume", "position", "signal_rule", "cycle", "character",
)

DAILY_FACTOR_META: tuple[FactorMeta, ...] = (
    FactorMeta(
        key="trend", label="趋势结构", group="trend", mode="daily",
        default_weight=20, from_skill=True,
        formula="均线排列(5/10/60/120) + 年线位置 + 平台突破/破位 → 方向分",
        source="日线收盘价"),
    FactorMeta(
        key="chan_daily", label="缠论买卖点", group="price", mode="daily",
        default_weight=18, from_skill=True,
        formula="日线笔中枢位置 + 背驰 + 三类买卖点接近度",
        source="日线（chan.py）"),
    FactorMeta(
        key="volume", label="量价结构", group="volume", mode="daily",
        default_weight=16, from_skill=True,
        formula="量柱体系：高量柱实顶/实底攻防 + 倍量/缩量/梯量 + 量价16形态方向",
        source="日线量价（volume.py）"),
    FactorMeta(
        key="position", label="位置与身位", group="trend", mode="daily",
        default_weight=14, from_skill=True,
        formula="近120日分位（越高越偏空）+ 距高点/低点幅度 + 连板高度惩罚",
        source="日线 + 涨停判定"),
    FactorMeta(
        key="signal_rule", label="规则信号质量", group="momentum", mode="daily",
        default_weight=12, from_skill=True,
        formula="B1-B15 与 S1-S6 的触发条数、满足度加权（买入正、卖出负）",
        source="daily_signals.py 规则体系"),
    FactorMeta(
        key="cycle", label="市场情绪周期", group="market", mode="daily",
        default_weight=10, from_skill=True,
        formula="周期阶段（试错/主升/高位震荡/退潮）→ 该阶段的做T适用性",
        source="东财涨停池/炸板池/跌停池"),
    FactorMeta(
        key="character", label="股性适配", group="stock", mode="daily",
        default_weight=10, from_skill=True,
        formula="做T友好度（振幅×均值回归性×活跃度×涨停基因）→ 该股值不值得滚动",
        source="该股近250日股性画像"),
)

FACTOR_META: dict[str, FactorMeta] = {item.key: item for item in INTRADAY_FACTOR_META}
# 日线单独一张表：`cycle` 与 `character` 在两种模式里是**同名但不同口径**的因子
# （分时 cycle 看"环境温度"，日线 cycle 看"阶段适用性"；权重也不同）。
# 早期把两张表合并成一个 dict，日线条目会把分时的同名字段覆盖掉 ——
# 结果分时目录里 cycle 的默认权重显示成日线的 10、公式也变成了日线那句。
_DAILY_META: dict[str, FactorMeta] = {item.key: item for item in DAILY_FACTOR_META}


def meta_for(key: str, mode: Mode = "intraday") -> FactorMeta:
    """按模式取因子元数据（未知键抛 KeyError，不静默返回空）。"""
    table = _DAILY_META if mode == "daily" else FACTOR_META
    return table[key]


FACTOR_ORDER_BY_MODE: dict[Mode, tuple[str, ...]] = {
    "intraday": INTRADAY_FACTOR_ORDER,
    "daily": DAILY_FACTOR_ORDER,
}


def factor_keys(mode: Mode) -> tuple[str, ...]:
    """该模式下的因子键顺序（未知模式抛 KeyError，避免静默返回空）。"""
    return FACTOR_ORDER_BY_MODE[mode]


def factor_meta(mode: Mode) -> list[FactorMeta]:
    """该模式下的因子元数据（保持目录顺序）。"""
    return [meta_for(key, mode) for key in factor_keys(mode)]


# ==================================================================
# 权重模板
# ==================================================================

@dataclass(frozen=True)
class WeightTemplate:
    """一套命名权重配方。"""

    key: str
    label: str
    mode: Mode
    description: str
    weights: dict[str, float] = field(default_factory=dict)

    def normalized(self) -> dict[str, float]:
        return normalize_weights(self.weights, self.mode)


# —— 分时做T ——
#
# `balanced` 是把技能库 4 个新因子接进来后的**新默认**：
# 原十因子按 0.71 等比缩放（17/11/8/6/6/6/3/6/4/4 = 71），
# 新四因子共 29 分（缠论12 / 筹码8 / 情绪周期6 / 股性3）。
# 「箱体仍是单项最高权重」的原始设计意图保持不变，总分刻度仍为 [-100,100]，
# ±20/±30 阈值语义不变。
_INTRADAY_BALANCED = {
    "box": 17, "vwap": 11, "boll": 8, "macd": 6, "kdj_rsi": 6,
    "sentiment": 6, "news": 3, "index_volume": 6, "board_rank": 4, "overseas": 4,
    "chan": 12, "chip": 8, "cycle": 6, "character": 3,
}

INTRADAY_TEMPLATES: tuple[WeightTemplate, ...] = (
    WeightTemplate(
        "balanced", "均衡（默认）", "intraday",
        "技术指标 + 市场环境 + 技能库四因子均衡配置，适合大多数自选标的",
        _INTRADAY_BALANCED),
    WeightTemplate(
        "swing", "震荡回归型", "intraday",
        "箱体震荡票：加重箱体/VWAP/布林等均值回归维度，压低趋势类指标",
        {"box": 24, "vwap": 16, "boll": 14, "macd": 4, "kdj_rsi": 8,
         "sentiment": 4, "news": 2, "index_volume": 4, "board_rank": 3, "overseas": 2,
         "chan": 8, "chip": 6, "cycle": 3, "character": 2}),
    WeightTemplate(
        "trend", "趋势跟随型", "intraday",
        "单边趋势票（做T极易卖飞）：加重缠论结构与 MACD，压低箱体权重",
        {"box": 8, "vwap": 8, "boll": 6, "macd": 12, "kdj_rsi": 6,
         "sentiment": 5, "news": 3, "index_volume": 8, "board_rank": 5, "overseas": 5,
         "chan": 18, "chip": 8, "cycle": 6, "character": 2}),
    WeightTemplate(
        "dragon", "高换手题材/连板", "intraday",
        "连板梯队与题材龙头：加重筹码、情绪周期、板块排行与缠论结构",
        {"box": 10, "vwap": 10, "boll": 2, "macd": 6, "kdj_rsi": 6,
         "sentiment": 10, "news": 4, "index_volume": 4, "board_rank": 8, "overseas": 2,
         "chan": 10, "chip": 14, "cycle": 12, "character": 2}),
    WeightTemplate(
        "low_vol", "低波动钝化型", "intraday",
        "银行/公用事业等窄幅票：只认箱体与布林，做T空间靠档位而非信号",
        {"box": 26, "vwap": 18, "boll": 16, "macd": 4, "kdj_rsi": 10,
         "sentiment": 4, "news": 2, "index_volume": 3, "board_rank": 2, "overseas": 1,
         "chan": 8, "chip": 4, "cycle": 1, "character": 1}),
    WeightTemplate(
        "legacy", "原始十因子口径", "intraday",
        "技能库四因子权重置 0，完全回到本次改动前的打分口径（用于对照历史结论）",
        {"box": 24, "vwap": 16, "boll": 12, "macd": 8, "kdj_rsi": 8,
         "sentiment": 8, "news": 4, "index_volume": 8, "board_rank": 6, "overseas": 6,
         "chan": 0, "chip": 0, "cycle": 0, "character": 0}),
)

# —— 日线做T ——
_DAILY_BALANCED = {
    "trend": 20, "chan_daily": 18, "volume": 16, "position": 14,
    "signal_rule": 12, "cycle": 10, "character": 10,
}

DAILY_TEMPLATES: tuple[WeightTemplate, ...] = (
    WeightTemplate(
        "balanced", "均衡（默认）", "daily",
        "趋势/结构/量价/位置/规则信号/情绪周期/股性 均衡配置",
        _DAILY_BALANCED),
    WeightTemplate(
        "swing", "震荡回归型", "daily",
        "箱体震荡票：加重缠论买卖点与规则信号，压低趋势跟随",
        {"trend": 10, "chan_daily": 22, "volume": 16, "position": 12,
         "signal_rule": 16, "cycle": 8, "character": 16}),
    WeightTemplate(
        "trend", "趋势跟随型", "daily",
        "趋势票：以均线趋势与缠论结构为主，规则信号只做辅助",
        {"trend": 26, "chan_daily": 18, "volume": 16, "position": 14,
         "signal_rule": 8, "cycle": 8, "character": 10}),
    WeightTemplate(
        "dragon", "高换手题材/连板", "daily",
        "连板/龙头：量价与身位优先，位置越高越要压住做T冲动",
        {"trend": 14, "chan_daily": 14, "volume": 20, "position": 18,
         "signal_rule": 14, "cycle": 14, "character": 6}),
    WeightTemplate(
        "low_vol", "低波动钝化型", "daily",
        "窄幅票：股性与量价权重抬高，趋势类信号在钝化票上噪声很大",
        {"trend": 12, "chan_daily": 16, "volume": 18, "position": 12,
         "signal_rule": 14, "cycle": 8, "character": 20}),
)

TEMPLATES_BY_MODE: dict[Mode, tuple[WeightTemplate, ...]] = {
    "intraday": INTRADAY_TEMPLATES,
    "daily": DAILY_TEMPLATES,
}

DEFAULT_WEIGHTS: dict[Mode, dict[str, float]] = {
    "intraday": dict(_INTRADAY_BALANCED),
    "daily": dict(_DAILY_BALANCED),
}


def templates(mode: Mode) -> tuple[WeightTemplate, ...]:
    return TEMPLATES_BY_MODE[mode]


def template(key: str, mode: Mode) -> WeightTemplate | None:
    """按 key 取模板（不存在返回 None，由调用方决定报错口径）。"""
    for item in TEMPLATES_BY_MODE[mode]:
        if item.key == key:
            return item
    return None


def template_keys(mode: Mode) -> tuple[str, ...]:
    return tuple(item.key for item in TEMPLATES_BY_MODE[mode])


# ==================================================================
# 归一化 / 合并
# ==================================================================

def normalize_weights(
    weights: dict[str, float], mode: Mode,
    *, target: float = 100.0, keep_zero: bool = True,
) -> dict[str, float]:
    """把任意一套权重缩放到合计 = target，并补齐缺失键。

    为什么不是简单等比缩放后了事：
    - 前端滑杆只能给整数步进，等比缩放后一堆 16.94 会互相抵消成 99.98/100.02，
      合计校验立刻失败 —— 所以必须做**最大余额取整**（largest remainder），
      保证结果仍是精确的 target。
    - `keep_zero=True` 时，用户刻意置 0 的因子保持 0（例如 legacy 模板关掉技能因子），
      不会被缩放的舍入重新"复活"成 0.4 分。
    """
    keys = factor_keys(mode)
    clean: dict[str, float] = {}
    for key in keys:
        raw = weights.get(key, 0.0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value != value or value < 0:  # NaN / 负数一律按 0 处理
            value = 0.0
        clean[key] = value
    total = sum(clean.values())
    if total <= 0:
        # 全 0（用户把所有权重拖到 0）→ 回落到默认配方，避免总分恒为 0 的假信号
        return dict(DEFAULT_WEIGHTS[mode])
    scaled = {key: value / total * target for key, value in clean.items()}
    if keep_zero:
        for key, value in clean.items():
            if value == 0:
                scaled[key] = 0.0
    return _round_to_total(scaled, target)


def _round_to_total(scaled: dict[str, float], target: float) -> dict[str, float]:
    """最大余额法取整到 target（保留一位小数）。"""
    # 先按 0.1 粒度取整，再用余额补差，避免出现 99.9 或 100.1
    shifted = {key: value * 10 for key, value in scaled.items()}
    floors = {key: float(int(value)) for key, value in shifted.items()}
    remainder = target * 10 - sum(floors.values())
    order = sorted(shifted, key=lambda k: (-(shifted[k] - floors[k]), k))
    step = 1.0 if remainder >= 0 else -1.0
    for index in range(int(abs(round(remainder)))):
        floors[order[index % len(order)]] += step
    return {key: round(value / 10, 1) for key, value in floors.items()}


def merge_weights(base: dict[str, float], patch: dict[str, float],
                  mode: Mode) -> dict[str, float]:
    """把 patch 覆盖到 base 上（只改出现的键），返回**未归一化**的合并结果。

    未归一化是刻意的：调用方（前端编辑器）需要看到"用户当前这一版"，由它决定
    何时点「归一化到 100」；后台保存时再调 `normalize_weights`。
    """
    merged = {key: float(base.get(key, 0.0)) for key in factor_keys(mode)}
    for key, value in patch.items():
        if key in merged:
            merged[key] = float(value)
    return merged


def weights_sum(weights: dict[str, float], mode: Mode) -> float:
    return round(sum(float(weights.get(key, 0.0)) for key in factor_keys(mode)), 4)


def diff_weights(before: dict[str, float], after: dict[str, float],
                 mode: Mode) -> list[dict[str, float | str]]:
    """两套权重的差异（面板显示「这只票改了哪些因子的权重」）。"""
    result: list[dict[str, float | str]] = []
    for key in factor_keys(mode):
        old = float(before.get(key, 0.0))
        new = float(after.get(key, 0.0))
        if abs(old - new) > 1e-9:
            meta = meta_for(key, mode)
            result.append({
                "key": key,
                "label": meta.label,
                "before": round(old, 2),
                "after": round(new, 2),
                "delta": round(new - old, 2),
            })
    return result
