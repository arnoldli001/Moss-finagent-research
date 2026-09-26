"""ETF 份额回测的统计口径工具：独立样本数、置信区间、按观察期取目标线。

## 为什么必须报「独立样本数」（2026-09-22 实测）

`by_kind` / `by_regime` / `by_kind_regime` / `by_etf` 四张表**按信号条数**计样本，
而 ETF 份额信号在时间上高度重叠，三条机制同时起作用：

    1. 判据是"份额 **5 日累计**"变化，而份额是**存量** —— 一次资金流入发生后
       接下来 5~21 天的累计值天天超标，同一个事件会连续报很多天；
    2. 同系列的 4 只沪深300 ETF 经常在**同一天**一起触发（实测最多 5 只同日）；
    3. T+34 的收益窗口本身就长达 34 个交易日，相邻信号的前瞻窗口大量重叠。

实测 `opportunity_live`（门控放行的机会信号）：66 条信号只落在 36 个交易日、
8 个月、**3 个年份（2018 / 2022 / 2024）**；按 34 个交易日去重后只剩
**3 个独立时段**。把 n=66 的置信区间套上去会得出"胜率 83.3% 显著高于 50%"，
而真实的有效样本量是 3。

所以本模块给出 `independent_episodes()`：**按观察期长度做贪婪去重**，
数出"前瞻窗口互不重叠"的时段个数。它不改变胜率本身（那回答"平均而言怎么样"），
只回答"这个胜率是几个事件撑起来的" —— 后者才决定结论能不能信。

⚠️ 它**不替代** `etf_flow_backtest.collapse_signal_runs`（那个管明细展示层
"同一事件连报多日"的合并）。两者是不同层次：那个让明细表不刷屏，
这个让统计表如实标出有效样本量。`run` 的统计口径仍按**信号条数**算胜率，
`independent` 只是一个**附加的诊断列** —— 改胜率的分母会让历史结论全部变样，
而那属于另一次有意的口径变更。

## 为什么按「交易日」而不是自然日算间隔

观察期是交易日口径（T+34 = 34 个交易日）。两个信号相隔 10 个自然日可能只隔
6 个交易日，用自然日判重叠会漏判。所以间隔一律在主指数日历上算；
日历不可用时退回自然日（见 `_distance`，退回方向是保守的）。

## 为什么目标线要按观察期分别配

`targets.median_return_34: 0.045` 原本被**所有观察期共用**，于是 T+5 那一行
拿 5 日收益的中位数去比 34 日的 4.5% 门槛，永远显示"未达标"——
那个标记没有信息量，还会让人误以为信号在短周期上失效。
`resolve_targets()` 支持按观察期取值，取不到就返回 None（**不表态**），
而不是拿别的观察期的线顶上。

## 达标为什么必须看方向

`risk`（高位 + 份额大减 → 后市走弱）的**正确**表现是负收益。拿"中位数 ≥ +4.5%"
这种做多口径去判它，等于结构性地给它判"未达标"—— 一个判断正确的看空信号
在报表里永远显示失败。所以 `target_hit()` 按信号方向取不等式：
做多类看 `median >= target`，风险类看 `median <= -target`。
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

#: Wilson 区间的默认 z（对应 95%）
WILSON_Z = 1.96

#: 目标线为"负向"的信号类型（判达标时取相反的不等式）。
#: 只放真正方向相反的：行业反转警示衡量的是"行业 ETF 散户追涨 → 短期反转"，
#: 方向与机会信号同为"看跌后市"，但它**不是**做空信号（模块只出警示不做动作），
#: 所以此处按做多口径判会让它永远不达标 —— 见 `target_hit` 的说明。
NEGATIVE_KINDS = frozenset({"risk"})


def calendar_index(calendar: Sequence[str], day: str) -> int:
    """`day` 在交易日日历里的位置；不在日历上时返回它**应当插入**的位置。

    取"应当插入的位置"而不是 -1：份额/指数序列偶有缺日（停牌、数据缺口），
    返回 -1 会让所有这些日期看起来彼此相邻（距离 0），把独立时段数算少。
    `bisect_left` 给出的是单调位置，用于比较间隔是安全的。
    """
    return bisect.bisect_left(calendar, str(day))


def independent_episodes(dates: Sequence[str], *, calendar: Sequence[str],
                        gap: int) -> list[str]:
    """按"间隔 ≥ `gap` 个交易日"贪婪去重，返回被保留的日期（升序）。

    规则：日期升序遍历，只接受与**上一个被接受的日期**相距 `gap` 以上的那些，
    第一个总是被接受。基准取"上一个被接受"而不是"上一个原始日期"，
    否则一段密集连报里会每隔 `gap` 天就被计入一次，仍然把同一个事件重复计数。

    `gap` 取观察期本身（T+34 就传 34）。于是同一批信号在 T+5 与 T+34 下的
    独立样本数**不同**：短窗口下互不重叠的信号更多。这是正确行为而不是不一致 ——
    "83.3% 的胜率有几个独立事件支撑"这个问题本身就依赖观察期。
    """
    days = sorted({str(item) for item in dates if str(item)})
    if not days:
        return []
    size = max(int(gap), 1)
    accepted = [days[0]]
    for day in days[1:]:
        if _distance(calendar, day, accepted[-1]) >= size:
            accepted.append(day)
    return accepted


def wilson_interval(successes: int, total: int, *,
                    z: float = WILSON_Z) -> tuple[float, float] | None:
    """二项比例的 Wilson 95% 置信区间；`total <= 0` 返回 None。

    用 Wilson 而不是正态近似：小样本 + 比例接近 0/1 时（本模块的胜率经常是
    0.83、0.30 这种）正态近似会算出 [0,1] 之外的区间，而 Wilson 天然有界。
    这正是本模块最需要的场景 —— 样本量小到必须给出区间，
    否则一个孤零零的 "83.3%" 会被读成确定的事实。
    """
    count = int(total)
    if count <= 0:
        return None
    hits = min(max(int(successes), 0), count)
    rate = hits / count
    denom = 1.0 + z * z / count
    centre = (rate + z * z / (2 * count)) / denom
    half = z * ((rate * (1 - rate) / count + z * z / (4 * count * count)) ** 0.5) / denom
    return (round(max(centre - half, 0.0), 4),
            round(min(centre + half, 1.0), 4))


def resolve_targets(targets: Mapping[str, Any], horizon: int, *,
                    short: bool = False) -> tuple[float | None, float | None]:
    """取某个观察期的 `(收益幅度目标, 胜率目标)`，缺配时返回 None。

    三条取值路径，优先级从高到低：

        1. `median_return: {5: 0.01, 34: 0.045}`   —— 推荐写法
        2. `median_return_34: 0.045`               —— 兼容旧配置（后缀式）
        3. `median_return: 0.03`                   —— 标量，所有观察期共用

    第 3 条保留是有意的：某个指标确实可能所有观察期用同一个门槛。
    但**取不到时必须返回 None**，不能拿另一个观察期的线顶上 ——
    那正是"T+5 永远未达标"那个坑的成因。

    `short=True`（风险信号）时优先取 `median_return_short`，缺配则回退到
    `median_return` —— 即**对称门槛**："一次正确的看空要跌够同样的幅度"。
    之所以留一个独立键而不是直接取负：多空两侧的合理门槛未必对称
    （A 股 34 个交易日的下行幅度分布与上行不同），但默认值必须有个说法，
    不能靠"配置里碰巧写着 4.5%"来决定看空信号的门槛。

    返回的是**幅度**（正数），方向由 `target_hit` 按信号类型决定。
    """
    key = "median_return_short" if short else "median_return"
    median = _pick_target(targets, key, horizon)
    if median is None and short:
        median = _pick_target(targets, "median_return", horizon)
    return (median, _pick_target(targets, "win_rate", horizon))


def target_hit(*, kind: str, median: float | None, win_rate: float | None,
               target_median: float | None,
               target_win: float | None) -> bool | None:
    """按**信号方向**判达标；未配目标或中位数缺失时返回 None（不表态）。

    ## 方向为什么必须区分

    `risk`（高位 + 份额大减 → 后市走弱）的**正确**表现是负收益。拿
    "中位数 >= +4.5%" 这种做多口径去判它，等于结构性地给它判"未达标" ——
    一个判断正确的看空信号在报表里永远显示失败，而且看不出是方向搞错了。
    所以做多类看 `median >= 目标`，风险类看 `median <= -目标`。

    ## ⚠️ 方向修正**不等于**门槛放宽

    修正后风险信号仍可能不达标 —— 那时它表达的是"方向对，但幅度没达到门槛"
    这个**真实结论**（实测 `risk_live` 的 T+34 中位数是 -2.00%，
    达不到 4.5% 的对称门槛）。这是有意保留的：把看空的门槛悄悄调低，
    就会把一个"强度不足"的信号粉饰成"达标"。
    真要改门槛，用 `median_return_short` 显式配。

    ## 胜率门槛在两个方向上是镜像的

    `win_rate` 的定义是"收益为正的比例"。做多类要求它 `>= 目标`；
    风险类要求它 `<= 1 - 目标`（即大多数时候确实在跌）。两者语义对称，
    但**不能**对风险类也用 `>=`：那会让"跌得不干脆"的信号看起来更好。
    """
    if median is None or target_median is None:
        return None
    magnitude = abs(float(target_median))
    if kind in NEGATIVE_KINDS:
        hit = median <= -magnitude
        if target_win is None:
            return hit
        return hit and (win_rate is None or win_rate <= 1.0 - float(target_win))
    hit = median >= magnitude
    if target_win is None:
        return hit
    return hit and (win_rate is None or win_rate >= float(target_win))


def _pick_target(targets: Mapping[str, Any], key: str,
                 horizon: int) -> float | None:
    """按 `key` 取某观察期的目标值（见 `resolve_targets` 的三条路径）。"""
    node: Any = targets.get(key)
    if isinstance(node, Mapping):
        # 字典形式：**这一期没配就是没配**，不再回退到后缀式，
        # 否则 `{5: ...}` 里缺 T+34 时会悄悄用上一个别的键的值。
        node = node.get(str(int(horizon)), node.get(int(horizon)))
    elif node is None:
        node = targets.get(f"{key}_{int(horizon)}")
    return _as_float(node)


def _as_float(value: Any) -> float | None:
    """转 float；`True`/`False` 不算数字（YAML 里 `win_rate: true` 是写错了）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance(calendar: Sequence[str], later: str, earlier: str) -> int:
    """`later` 与 `earlier` 的间隔（交易日优先；日历不可用时退回自然日）。

    退回自然日是**保守**方向：自然日间隔 ≥ 交易日间隔，用同一个阈值会判得更严、
    算出的独立样本更少 —— 宁可低估独立性，也不要高估。
    解析失败返回 0（视为同一天，即不独立），同样偏保守。
    """
    if calendar:
        return calendar_index(calendar, later) - calendar_index(calendar, earlier)
    try:
        return (datetime.strptime(str(later), "%Y%m%d")
                - datetime.strptime(str(earlier), "%Y%m%d")).days
    except (TypeError, ValueError):
        return 0


__all__ = [
    "NEGATIVE_KINDS",
    "WILSON_Z",
    "calendar_index",
    "independent_episodes",
    "resolve_targets",
    "target_hit",
    "wilson_interval",
]
