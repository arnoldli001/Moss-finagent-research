"""主线挖掘：打分用数学原语（纯函数、无 IO、无配置依赖）。

## 为什么单独一个文件

六维/五维/龙头三层打分里有大量重复的数学动作：分位映射、z-score、
加权平均、滚动相关系数。这些动作各有**容易搞错且不会报错**的细节
（z-score 的 std=0、加权平均里"无数据"被当 0 分、滚动相关样本不足），
集中在这里就能一次钉住 —— 散在三处必然慢慢漂移成三种口径。

决定性证据：把"无数据"当 0 分参与加权，会让数据源一抖动就**全市场**
都不告警（所有板块的分数被系统性拉低），而界面上看不出任何异常。

## 两条硬规则

1. **无数据 ≠ 0 分。** `weighted_score` 会把 `available=False` 的项从
   权重里剔除并重新归一化；`None` 一律不参与均值/分位计算。
2. **样本不足就返回 None，不返回一个看起来正常的数。** 例如 20 日滚动
   相关至少要 10 个共同交易日，否则宁可不给信号。

## 口径约定

- 分数一律 **0-100**；`ratio` 类入参一律 **0-1**（不是百分数）。
- `rank_percentile` 越大 = 越好（用于"景气度越高分越高"这类正向映射）；
  需要反向时调用方传 `reverse=True`，而不是在调用处写 `100 - x`
  （后者在 x 为 None 时会炸，且在分位口径改变时容易漏改）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def is_finite(value: object) -> bool:
    """是否有限实数（None / NaN / inf / 非数字 → False）。

    比 `math.isfinite` 更宽松的入参：打分链路上游可能给 numpy 标量、
    字符串数字或 None，逐个判类型会让调用处到处是 try。
    """
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def to_float(value: object, default: float | None = None) -> float | None:
    """安全转 float；不可转或非有限值时返回 `default`。"""
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """把分数夹在 [low, high]（默认 0-100）。"""
    if value != value:  # NaN
        return low
    return max(low, min(high, value))


def mean(values: Iterable[object]) -> float | None:
    """算术均值（跳过 None/NaN；空集返回 None）。"""
    clean = [item for item in (to_float(v) for v in values) if item is not None]
    return sum(clean) / len(clean) if clean else None


def median(values: Iterable[object]) -> float | None:
    """中位数（跳过 None/NaN；空集返回 None）。

    筹码集中度一类的横截面统计**必须用中位数**：股东户数变化率的分布
    有极端值（重组股动辄 -60%），均值会被单只票带偏。
    """
    clean = sorted(item for item in (to_float(v) for v in values)
                   if item is not None)
    if not clean:
        return None
    size = len(clean)
    middle = size // 2
    if size % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2.0


def stdev(values: Sequence[object]) -> float | None:
    """样本标准差（n-1 分母；少于 2 个有效值返回 None）。"""
    clean = [item for item in (to_float(v) for v in values) if item is not None]
    if len(clean) < 2:
        return None
    average = sum(clean) / len(clean)
    variance = sum((item - average) ** 2 for item in clean) / (len(clean) - 1)
    return math.sqrt(variance)


def zscore(values: Sequence[object]) -> list[float | None]:
    """序列的 z-score（与入参等长；None 位置保持 None）。

    **std 为 0 时全部返回 None 而不是 0**：整段序列完全不动时，
    "偏离均值 0 倍标准差"没有意义 —— 返回 0 会让"横盘板块"拿到
    与"正常板块"一样的分数，从而出现在异动榜上。
    """
    clean = [to_float(item) for item in values]
    valid = [item for item in clean if item is not None]
    if len(valid) < 3:
        return [None] * len(clean)
    average = sum(valid) / len(valid)
    deviation = stdev(valid)
    if deviation is None or deviation <= 1e-12:
        return [None] * len(clean)
    return [None if item is None else (item - average) / deviation
            for item in clean]


def last_zscore(values: Sequence[object]) -> float | None:
    """序列**最后一个**有效值的 z-score（在整段窗口上算）。"""
    scores = zscore(values)
    for item in reversed(scores):
        if item is not None:
            return item
    return None


def rank_percentile(values: Sequence[object], value: object, *,
                    reverse: bool = False) -> float | None:
    """`value` 在 `values` 中的分位（0-1；越大越好，`reverse=True` 时越小越好）。

    口径：分位 = (严格小于的个数 + (并列数-1)/2) / (有效样本数 - 1)。

    用「严格小于 + 平均秩」而不是 `scipy.stats.rankdata` 的默认平均秩，
    是为了让**无并列时**最大值恒等于 1.0、最小值恒等于 0.0。
    ⚠️ 有并列时最高分位会略低于 1.0（`[1,2,3,4,5,5]` 里的 5 得到 0.9）——
    这是平均秩口径的必然结果，不是 bug；横截面里出现并列本该被"摊平"，
    否则并列第一的两只票会同时拿到满分，而我们无法区分它们。

    样本少于 5 个时返回 None：在 5 个以下样本里排第一名太容易，
    给出高分等于制造假信号。
    """
    clean = [item for item in (to_float(v) for v in values)
             if item is not None]
    target = to_float(value)
    if target is None or len(clean) < 5:
        return None
    below = sum(1 for item in clean if item < target)
    equal = sum(1 for item in clean if item == target)
    percentile = (below + (equal - 1) / 2.0) / max(len(clean) - 1, 1)
    percentile = clamp(percentile, 0.0, 1.0)
    return 1.0 - percentile if reverse else percentile


def percentile_score(values: Sequence[object], value: object, *,
                     reverse: bool = False) -> float | None:
    """分位映射成 0-100 分（`rank_percentile` × 100）。"""
    percentile = rank_percentile(values, value, reverse=reverse)
    return None if percentile is None else percentile * 100.0


def weighted_score(items: Sequence[tuple[float, float, bool]]) -> tuple[float, float]:
    """加权分。入参 `[(score, weight, available)]`，返回 `(分数, 覆盖率)`。

    两条规则：

    1. `available=False` 的项**权重要从分母里剔除**，而不是当 0 分算。
       把"没取到数"当 0 分会系统性压低全部板块的分数，数据源一抖动
       全市场都不告警 —— 而界面上看不出异常（分数就是低而已）。
    2. 覆盖率一并返回（= 参与加权的权重 / 总权重），让调用方能展示
       "这个分是靠 40% 的维度算出来的"，而不是把一个残缺的分当成完整的。

    全部项都不可用时返回 `(0.0, 0.0)`（调用方据此判为无数据）。
    """
    usable_weight = sum(weight for _, weight, ok in items if ok and weight > 0)
    total_weight = sum(weight for _, weight, _ in items if weight > 0)
    if usable_weight <= 0 or total_weight <= 0:
        return 0.0, 0.0
    total = sum(score * weight for score, weight, ok in items
                if ok and weight > 0)
    return total / usable_weight, usable_weight / total_weight


def breakout_ceiling(history: Sequence[object], *, quantile: float,
                     min_samples: int) -> float | None:
    """板块**自己**过去一段时间的分数上沿（分位数）。

    为什么要有它：告警阈值（`medium_score`/`strong_score`）是**全市场统一**
    的绝对分，于是"能不能报"由"分数够不够极端"决定，而不是由
    "这个板块是不是刚突破它自己的震荡区间"决定。实测农业种植 885812 在
    20260623 的六维排名是**全市场第 8**（确实启动了），但 `total` 只有
    62.3、低于中信号线 → 不报；真正报出来靠的是三天后的门控加分。
    也就是说全市场普涨时真领涨的板块会被绝对阈值漏掉。

    返回 `None` 表示历史不足（`< min_samples`）→ 这条路径不生效。
    """
    values = [float(item) for item in history if item is not None]
    values = [value for value in values if math.isfinite(value)]
    if len(values) < max(int(min_samples), 5):
        return None
    ordered = sorted(values)
    # 用 `quantile` 位置法取分位（与 `statistics.quantiles` 不同，这里不需要插值）
    position = min(len(ordered) - 1,
                   max(0, int(round(quantile * (len(ordered) - 1)))))
    return ordered[position]


def is_breakout(total: object, ceiling: float | None, *, min_score: float,
                recent: Sequence[object] | None = None,
                fresh_days: int = 0) -> bool:
    """当前分是否**刚刚越过**自己的上沿，且不低于全局下限。

    `min_score` 不是可有可无的：没有它，一个长期在 30 分徘徊的板块
    冲上 35 分也会算"突破"（矮子里拔将军），告警会爆掉。

    `recent` + `fresh_days` 是**第二个必须的闸门**：只看"当前 > 上沿"会把
    **趋势**当成突破 —— 一个持续走强的板块几乎每天都在它自己的 90 分位之上，
    于是天天报（实测占候选池 11~20%，告警量会翻几倍）。
    用户要的是"**过了**某个长期震荡上限"，也就是**穿越**：
    要求最近 `fresh_days` 天**都在上沿之下**，当前才越过去。
    """
    value = to_float(total)
    if value is None or ceiling is None:
        return False
    if value < float(min_score):
        return False
    if not value > float(ceiling):
        return False
    if fresh_days > 0 and recent:
        window = [to_float(item) for item in list(recent)[-fresh_days:]]
        window = [item for item in window if item is not None]
        # 只要窗口里有一天已经在上沿之上，说明它不是"刚越过去"
        if any(item > float(ceiling) for item in window):
            return False
    return True


def safe_ratio(numerator: object, denominator: object, *,
               default: float | None = None) -> float | None:
    """安全除法（分母为 0/None/非有限值时返回 `default`）。

    比率类指标里 0 分母极其常见（新股没有前收、停牌股没有成交额），
    每次手写 `if denom` 必然有漏掉的一处 —— 而漏掉那处就是 ZeroDivisionError
    把整轮打分打断。
    """
    top = to_float(numerator)
    bottom = to_float(denominator)
    if top is None or bottom is None or abs(bottom) < 1e-12:
        return default
    result = top / bottom
    return result if math.isfinite(result) else default


def pct_change(current: object, previous: object) -> float | None:
    """变化率（小数，不是百分数）；基准为 0/None 时返回 None。"""
    base = to_float(previous)
    now = to_float(current)
    if base is None or now is None or abs(base) < 1e-12:
        return None
    return (now - base) / abs(base)


def rolling_return(values: Sequence[object], window: int) -> float | None:
    """近 `window` 期的收益率（小数）：`values[-1] / values[-1-window] - 1`。

    不足 `window + 1` 个有效值时返回 None（**不外推到更短窗口**：
    那会让"近 5 日"在数据不足时静默变成"近 2 日"，而调用方无从察觉）。
    """
    clean = [item for item in (to_float(v) for v in values) if item is not None]
    if window <= 0 or len(clean) < window + 1:
        return None
    start, end = clean[-1 - window], clean[-1]
    if abs(start) < 1e-12:
        return None
    return end / start - 1.0


def rolling_sum(values: Sequence[object], window: int) -> float | None:
    """近 `window` 期合计（不足则按实际天数合计；完全无数据返回 None）。"""
    clean = [item for item in (to_float(v) for v in values) if item is not None]
    if not clean:
        return None
    return float(sum(clean[-window:]))


def correlation(left: Sequence[object], right: Sequence[object], *,
                min_samples: int = 10) -> float | None:
    """皮尔逊相关系数（按位置配对，任一侧缺失则丢弃该对）。

    `min_samples` 默认 10：20 日滚动相关里只要 3-4 个点就能算出 ±0.9，
    那种数字看着像信号、实际是噪声。样本不足**返回 None**
    （调用方据此不产生信号），而不是给一个低置信度的相关。
    """
    if len(left) != len(right):
        size = min(len(left), len(right))
        left, right = left[:size], right[:size]
    # `strict=True` 是刻意的：上面刚把两侧截成等长，若这里仍抛
    # "zip() argument N is shorter"，说明截断逻辑被改坏了 ——
    # 早抛比静默丢掉尾部数据好。
    pairs = [(to_float(a), to_float(b))
             for a, b in zip(left, right, strict=True)]
    clean = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(clean) < max(3, min_samples):
        return None
    xs = [item[0] for item in clean]
    ys = [item[1] for item in clean]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in clean)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 1e-12 or var_y <= 1e-12:
        return None
    result = cov / math.sqrt(var_x * var_y)
    return result if math.isfinite(result) else None


def correlation_series(left: Sequence[object], right: Sequence[object], *,
                       window: int, min_samples: int = 10
                       ) -> list[float | None]:
    """滚动相关序列（与入参等长；窗口内样本不足处为 None）。

    末尾元素即"当前 20 日相关"，用于检测相关性跃升。
    """
    size = min(len(left), len(right))
    out: list[float | None] = []
    for index in range(size):
        if index + 1 < window:
            out.append(None)
            continue
        out.append(correlation(left[index + 1 - window:index + 1],
                               right[index + 1 - window:index + 1],
                               min_samples=min_samples))
    return out


def spearman(left: Sequence[object], right: Sequence[object], *,
             min_samples: int = 10) -> float | None:
    """斯皮尔曼秩相关（= 排名上的皮尔逊相关）。

    计算 IC 用**秩相关**而不是皮尔逊：因子值与其未来收益的关系通常单调
    但非线性（分数 90 与 80 的收益差未必等于 60 与 50 的差），
    秩相关正是"排序对不对"的度量，而选股/选板块只用到排序。
    """
    if len(left) != len(right):
        size = min(len(left), len(right))
        left, right = left[:size], right[:size]
    pairs = [(to_float(a), to_float(b))
             for a, b in zip(left, right, strict=True)]
    clean = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(clean) < max(3, min_samples):
        return None
    ranks_left = _ranks([item[0] for item in clean])
    ranks_right = _ranks([item[1] for item in clean])
    return correlation(ranks_left, ranks_right, min_samples=max(3, min_samples))


def _ranks(values: Sequence[float]) -> list[float]:
    """平均秩（并列取平均），1-based。"""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while (end + 1 < len(order)
               and values[order[end + 1]] == values[order[position]]):
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def decay_weight(days_since: int, decay_days: int) -> float:
    """信号时间衰减权重（0-1；新信号 1.0，超过 `decay_days` 线性降到 0）。

    需求 8.3 要求区分板块类型：农业/题材 `decay_days=10`、大盘蓝筹 20-25。
    用**线性**而不是指数衰减：线性在接近到期时会明确归零，
    而指数衰减永远不到 0，会让三个月前的信号还挂着微弱权重。
    """
    if decay_days <= 0:
        return 1.0
    if days_since <= 0:
        return 1.0
    if days_since >= decay_days:
        return 0.0
    return 1.0 - days_since / decay_days


def icir(ic_series: Sequence[object]) -> float | None:
    """ICIR = IC 均值 / IC 标准差（样本不足或 std≈0 返回 None）。"""
    clean = [item for item in (to_float(v) for v in ic_series)
             if item is not None]
    if len(clean) < 5:
        return None
    average = sum(clean) / len(clean)
    deviation = stdev(clean)
    if deviation is None or deviation <= 1e-9:
        return None
    return average / deviation


def max_drawdown(equity: Sequence[object]) -> float | None:
    """最大回撤（正数小数；如 0.23 = -23%）。"""
    clean = [item for item in (to_float(v) for v in equity)
             if item is not None]
    if len(clean) < 2:
        return None
    peak = clean[0]
    worst = 0.0
    for value in clean:
        peak = max(peak, value)
        if peak > 1e-12:
            worst = max(worst, (peak - value) / peak)
    return worst


def annualize(total_return: float | None, days: int, *,
              trading_days: int = 244) -> float | None:
    """把区间总收益年化（几何）。

    `days` 为交易日数。不足 5 个交易日不年化（外推出来的年化收益
    动辄 ±200%，是纯粹的噪声，放进报告会误导人）。
    """
    if total_return is None or days < 5 or trading_days <= 0:
        return None
    base = 1.0 + total_return
    if base <= 0:
        # 亏光：年化收益没有定义（几何年化要求 base > 0）
        return -1.0
    return base ** (trading_days / days) - 1.0


def finite_values(values: Iterable[object]) -> list[float]:
    """过滤出有限实数（打分链路的清洗入口）。"""
    return [item for item in (to_float(v) for v in values)
            if item is not None]


__all__ = [
    "annualize",
    "breakout_ceiling",
    "clamp",
    "correlation",
    "correlation_series",
    "decay_weight",
    "finite_values",
    "icir",
    "is_breakout",
    "is_finite",
    "last_zscore",
    "max_drawdown",
    "mean",
    "median",
    "pct_change",
    "percentile_score",
    "rank_percentile",
    "rolling_return",
    "rolling_sum",
    "safe_ratio",
    "spearman",
    "stdev",
    "to_float",
    "weighted_score",
    "zscore",
]
