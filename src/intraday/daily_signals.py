"""日K级别做T信号库（B1–B15 买入 / S1–S7 卖出风控）。

逐条对应需求给出的信号库，每条规则都输出 **逐条件明细**（SignalCondition），
前端可以展示「哪几条满足、差在哪」，而不是只有一个黑盒结论。

## 卖出侧的实际覆盖（2026-09-17 补齐）

实测发现卖出侧原本只有 S2（唯一真卖点）+ S3/S6（风控）：10 只票 × 30 个交易日里
**真卖点触发 0 次**（S2 要"爆量 1.5 倍"或"跌破 20 日平台"，震荡市不具备这两个条件）。
因此补了两条能在常规行情下触发的真卖点：

| 信号 | 类型 | 触发场景 |
|---|---|---|
| S2 日线级别卖点 | sell | **下跌侧**：爆量离场 / 平台跌破（锁筹跳水） |
| S5 拉升达标止盈 | sell | **上涨侧**：当日大涨 ≥5% 或 高位滞涨收阴 |
| S7 量价背离 | sell | 创新高但量能未跟、且冲高回落 |
| S3 缩量阴线风控 | risk | 当天不抄底（**不是卖点**） |
| S6 高量纪律 | risk | 高量破 / 遇顶不过等纪律命中 |
| S4 缩量阳线持股 | watch | 当天不卖（**也不是卖点**） |

**S1 刻意不实现**：「S1 分时三类卖点」依赖分时/盘口数据，日线无法复现；
硬做只会是另一个规则冒充分时卖点，所以留在缺口说明里，而不是假装有。

实现取舍（诚实标注，不假装能做到）：
- 依赖 Level-2 / 竞价数据的规则（B3 竞价确认、B15 打板买点、S1 分时三类卖点、
  §6.5 盘口五单）只能做**日线可复现的部分**，并在 gaps 里写明缺什么数据；
- 依赖「筹码峰」的规则用「近60日成交量加权均价」近似（真实筹码分布需逐笔成交数据）；
- 依赖「板块同步吸筹」的规则（B14）需要板块成分数据，当前数据源不可得 → 记 gap。

所有规则在**技术信号相互矛盾时优先观望**（需求 §10 第5条）。
"""

from __future__ import annotations

from collections.abc import Callable

from src.intraday import volume as vol
from src.intraday.models import DailySignalItem, SignalCondition
from src.intraday.volume import DailyContext

SignalFn = Callable[[DailyContext], DailySignalItem | None]


def _cond(label: str, met: bool, actual: str = "",
          expected: str = "", note: str = "",
          required: bool = True) -> SignalCondition:
    """构造单条条件。

    required=True 表示这是该信号的**硬性条件**（缺一不可）；
    位置、待确认项、以及依赖缺失数据的条件应显式置 required=False，
    只参与打分展示，不能让一个「核心条件不满足」的信号被判为触发。
    """
    return SignalCondition(label=label, met=met, actual=actual,
                           expected=expected, note=note, required=required)


def _finish(code: str, name: str, kind: str, conditions: list[SignalCondition],
            reason: str, entry: float | None = None,
            stop_loss: float | None = None,
            gaps: list[str] | None = None,
            require_all: bool = True) -> DailySignalItem:
    """按条件满足度组装信号。

    触发判定：**全部硬性条件（required=True）满足**才 triggered。
    `require_all=False` 时放宽为「硬性条件全部满足 或 加权得分≥0.8」——
    仅用于「次日确认」这类天然无法当日判定的信号，且仍不允许核心条件缺失。
    """
    total = len(conditions) or 1
    met = sum(1 for item in conditions if item.met)
    score = met / total
    required = [item for item in conditions if item.required]
    required_ok = all(item.met for item in required) if required else False
    triggered = required_ok and (score >= 0.8 if not require_all else True)
    return DailySignalItem(
        code=code, name=name, kind=kind,  # type: ignore[arg-type]
        triggered=triggered, score=round(score, 4),
        conditions=conditions, reason=reason, entry=entry,
        stop_loss=stop_loss, gaps=gaps or [])


def _recent_limit_up(ctx: DailyContext, tolerance: float) -> int | None:
    """近 N 日内是否存在涨停（收盘涨幅 ≥ 9.7% 视为10cm涨停）。"""
    frame = ctx.frame
    threshold = 10.0 - tolerance
    for offset in range(0, min(30, ctx.index + 1)):
        if vol.pct_change(frame, ctx.index - offset) >= threshold:
            return offset
    return None


def _consolidation_after_rally(ctx: DailyContext, window: int = 15) -> bool:
    """是否处于「一波拉升后的横盘末端/上升途中」（B1 的位置前提）。

    近似口径：近 window 日内出现过 ≥7% 大阳，且最近 5 日振幅收敛（横盘）。
    """
    frame = ctx.frame
    start = max(0, ctx.index - window)
    had_big = any(
        vol.pct_change(frame, i) >= ctx.params.big_bar_pct
        for i in range(start, ctx.index + 1))
    recent = frame.iloc[max(0, ctx.index - 4):ctx.index + 1]
    if recent.empty:
        return False
    high = float(recent["high"].max())
    low = float(recent["low"].min())
    base = ctx.close()
    return had_big and base > 0 and (high - low) / base * 100 <= 8.0


# ==================================================================
# 买入信号库 B1–B15
# ==================================================================

def signal_b1(ctx: DailyContext) -> DailySignalItem | None:
    """B1 左长黑右长红：一波拉升后横盘末端出现「左长黑 + 右长红」。"""
    if ctx.index < 3:
        return None
    params = ctx.params
    left1 = ctx.change(1)
    left2 = ctx.change(2)
    single_black = left1 <= -params.big_bar_pct
    two_black = (left1 <= -3.0 and left2 <= -3.0
                 and (left1 + left2) <= -params.big_bar_pct)
    right_red = ctx.change(0) >= params.big_bar_pct
    position_ok = _consolidation_after_rally(ctx)
    # 小K线间隔天数越少确定性越高
    return _finish(
        "B1", "左长黑右长红", "buy",
        [
            _cond("左侧长黑（单根跌幅>7% 或 两根合计>7%）",
                  single_black or two_black,
                  f"昨日{left1:+.2f}% / 前日{left2:+.2f}%",
                  f"单根≤-{params.big_bar_pct:g}% 或两根各≤-3%且合计"
                  f"≤-{params.big_bar_pct:g}%"),
            _cond("右侧长红（当日涨幅≥7%）", right_red, f"{ctx.change():+.2f}%",
                  f"≥{params.big_bar_pct:g}%"),
            _cond("位置：一波拉升后的横盘末端/上升途中", position_ok,
                  "近15日出现过大阳且近5日振幅≤8%" if position_ok
                  else "未见拉升后横盘结构", required=False),
            _cond("次日收阳确认（待次日验证）", False, "需次日收盘确认",
                  "次日收阳", required=False),
        ],
        reason="洗盘结束+拉升启动形态；大阳线次日收阳为确认买点",
        entry=ctx.close(), stop_loss=ctx.close(),
        require_all=False)


def signal_b2(ctx: DailyContext) -> DailySignalItem | None:
    """B2 进击K线：洗盘后重新拉起突破/逼近前高。"""
    if ctx.index < 12:
        return None
    frame = ctx.frame
    prior_high = float(frame["high"].iloc[max(0, ctx.index - 10):ctx.index].max())
    breakout = ctx.close() >= prior_high * 0.995
    pullback_before = ctx.change(1) < 0 and ctx.volume(1) < ctx.volume(2)
    return _finish(
        "B2", "进击K线", "buy",
        [
            _cond("前置洗盘（前一日下跌且缩量）", pullback_before,
                  f"昨日{ctx.change(1):+.2f}%，量比"
                  f"{ctx.volume(1) / max(ctx.volume(2), 1):.2f}"),
            _cond("重新拉起突破/逼近前10日高点", breakout,
                  f"现价{ctx.close():.2f} vs 前10日高{prior_high:.2f}",
                  "≥前高的99.5%"),
            _cond("拉升放量（不放量=诱多陷阱）",
                  ctx.volume(0) > ctx.volume(1),
                  f"量比 {ctx.volume(0) / max(ctx.volume(1), 1):.2f}", ">1"),
        ],
        reason="洗盘结束的诱空确认；次日低开或快速回踩为买点",
        entry=None, stop_loss=ctx.low(),
        gaps=["分时诱空结构（快速拉高又快速回落）需分时数据，日线无法判定"],
        require_all=False)


def signal_b3(ctx: DailyContext) -> DailySignalItem | None:
    """B3 阴线反包法：涨停无明显放量后，阴线量≥近5日最大量且<其1.7倍。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset == 0 or ctx.index < max(offset + 1, 5):
        return None
    limit_index = ctx.index - offset
    limit_volume = vol._vol(ctx.frame, limit_index)
    limit_shrink = limit_volume <= vol._vol(ctx.frame, max(0, limit_index - 1))
    # 近5日（不含当日）最大单日成交量
    recent_max_vol = max(ctx.volume(i) for i in range(1, 6))
    big_shadow = ctx.volume(0) >= recent_max_vol
    not_extreme = ctx.volume(0) < recent_max_vol * 1.7
    return _finish(
        "B3", "阴线反包法", "buy",
        [
            _cond("近期有涨停", True, f"{ctx.date(offset)} 涨停"),
            _cond("涨停日无明显放量（主力高度控盘）", limit_shrink,
                  f"涨停日量 {limit_volume:.0f} vs 前日 "
                  f"{vol._vol(ctx.frame, max(0, limit_index - 1)):.0f}"),
            _cond("阴线量 ≥ 近5日最大单日成交量", big_shadow,
                  f"当日量 {ctx.volume(0):.0f} vs 近5日最大 "
                  f"{recent_max_vol:.0f}",
                  "≥近5日最大量"),
            _cond("阴线量 < 近5日最大量 × 1.7", not_extreme,
                  f"当日量 {ctx.volume(0):.0f} vs 上限 "
                  f"{recent_max_vol * 1.7:.0f}",
                  "<近5日最大量×1.7"),
        ],
        reason="阴线反包：放量阴线洗盘，次日竞价/分时确认后低吸",
        stop_loss=ctx.low(),
        gaps=["竞价模式（一气呵成/缓慢上攻）与分时三类买点需竞价+分时数据"],
        require_all=False)


def signal_b4(ctx: DailyContext) -> DailySignalItem | None:
    """B4 涨停回调低吸（注册制战法）。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset < params.pullback_days_min:
        return None
    limit_index = ctx.index - offset
    frame = ctx.frame
    emo_point = (float(frame["high"].iloc[limit_index])
                 + float(frame["low"].iloc[limit_index])) / 2
    support = float(frame["low"].iloc[limit_index])
    pullback = offset
    below_emo = ctx.close() < emo_point * (
        1 - params.emotion_tolerance_pct / 100)
    in_window = params.pullback_days_min <= pullback <= params.pullback_days_max + 3
    return _finish(
        "B4", "涨停回调低吸", "buy",
        [
            _cond("近期首板涨停", True, f"{ctx.date(offset)}"),
            _cond(f"回调≥{params.pullback_days_min}天", in_window,
                  f"回调 {pullback} 天"),
            _cond("阴线跌破情绪释放点(涨停日高低中点)", below_emo,
                  f"现价 {ctx.close():.2f} vs 情绪释放点 {emo_point:.2f}"),
            _cond("未破支撑位(涨停日最低)", ctx.close() > support,
                  f"支撑 {support:.2f}"),
        ],
        reason="情绪释放点下方低吸，破支撑无条件离场",
        entry=ctx.close(), stop_loss=support * 0.97,
        gaps=["换手率≥3%与市值≤300亿需快照/财务数据，若缺失则不做该项过滤"],
        require_all=False)


def signal_b5(ctx: DailyContext) -> DailySignalItem | None:
    """B5 新屠龙刀：低位首板 → 放量大阴 → 缩量调整不破首板低点 → 大阳拔起。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset < 4:
        return None
    frame = ctx.frame
    board_index = ctx.index - offset
    board_low = float(frame["low"].iloc[board_index])
    # 首板后是否出现放量大阴
    big_yin = any(
        vol.pct_change(frame, i) <= -params.big_bar_pct
        and not vol.is_shrink_volume(frame, i)
        for i in range(board_index + 1, ctx.index + 1))
    hold_low = all(
        float(frame["low"].iloc[i]) >= board_low
        for i in range(board_index + 1, ctx.index + 1))
    today_big = ctx.change() >= params.big_bar_pct
    return _finish(
        "B5", "新屠龙刀战法", "buy",
        [
            _cond("低位首板涨停", True, f"{ctx.date(offset)}"),
            _cond("首板后出现放量大阴线", big_yin, "已出现" if big_yin else "未见"),
            _cond("缩量调整不破首板低点", hold_low,
                  f"首板低点 {board_low:.2f}，调整期最低 "
                  f"{float(frame['low'].iloc[board_index + 1:].min()):.2f}"),
            _cond("大阳线放量拔起", today_big, f"当日 {ctx.change():+.2f}%",
                  f"≥{params.big_bar_pct:g}%"),
        ],
        reason="首板→放量大阴洗盘→缩量不破→大阳启动；破5日线离场、破支撑止损",
        entry=ctx.close(), stop_loss=board_low * 0.97,
        require_all=False)


def signal_b6(ctx: DailyContext) -> DailySignalItem | None:
    """B6 恐慌洗盘：盘中下影跌破重要支撑后收盘收回支撑上方。"""
    if ctx.index < 20:
        return None
    frame = ctx.frame
    support = float(frame["low"].iloc[ctx.index - 20:ctx.index].min())
    pierced = ctx.low() < support
    reclaimed = ctx.close() > support
    long_shadow = ctx.flags()["long_lower_shadow"]
    return _finish(
        "B6", "恐慌洗盘（低位长下影博弈）", "buy",
        [
            _cond("盘中下影跌破近20日支撑", pierced,
                  f"最低 {ctx.low():.2f} vs 支撑 {support:.2f}"),
            _cond("收盘收回支撑上方", reclaimed, f"收盘 {ctx.close():.2f}"),
            _cond(f"长下影（下影≥实体×{ctx.params.tail_shadow_x:g}）",
                  long_shadow, f"下影 {min(ctx.open(), ctx.close()) - ctx.low():.2f}"),
        ],
        reason="恐慌止损盘不会当日尾盘反人性买回 → 尾盘是短线博弈点；"
               "次日上影须突破前日收盘验证",
        entry=ctx.close(), stop_loss=ctx.low() * 0.98,
        require_all=False)


def signal_b7(ctx: DailyContext) -> DailySignalItem | None:
    """B7 均线金叉共振（5/10日均线，空头排列后的首次金叉）。"""
    params = ctx.params
    if ctx.index < params.ma_long + 3:
        return None
    frame = ctx.frame
    short = frame["close"].rolling(params.ma_short).mean()
    long = frame["close"].rolling(params.ma_long).mean()
    now = float(short.iloc[ctx.index]) if short.iloc[ctx.index] == short.iloc[ctx.index] else None
    prev_short = float(short.iloc[ctx.index - 1])
    prev_long = float(long.iloc[ctx.index - 1])
    today_long = float(long.iloc[ctx.index])
    golden = prev_short <= prev_long and now is not None and now > today_long
    # 空头排列后的**首次**金叉：前5日内无金叉
    first_cross = golden and all(
        float(short.iloc[i]) <= float(long.iloc[i])
        for i in range(max(0, ctx.index - 5), ctx.index))
    # 金叉时已连涨多日则不追高
    run_up = sum(1 for offset in range(1, min(6, ctx.index))
                 if ctx.change(offset) > 0)
    over_extended = run_up >= 4
    return _finish(
        "B7", "均线金叉共振（波段）", "buy",
        [
            _cond(f"{params.ma_short}/{params.ma_long}日均线金叉", first_cross,
                  f"MA{params.ma_short}={now:.2f} "
                  f"MA{params.ma_long}={today_long:.2f}" if now else "样本不足"),
            _cond("金叉时未连续上涨多日（不追高）", not over_extended,
                  f"近5日上涨{run_up}天", "≤3天"),
            _cond("回踩5日线企稳并出现阳包阴确认（待确认）", False,
                  "需回踩确认", "阳包阴", required=False),
        ],
        reason="空头排列后首次金叉，等回踩企稳再进；死叉后不杀跌，等共振反弹1-3天",
        entry=None, stop_loss=float(long.iloc[ctx.index]) if today_long == today_long else None,
        require_all=False)

def signal_b8(ctx: DailyContext) -> DailySignalItem | None:
    """B8 飞龙在天：第一波有力上涨后箱体内出现长下影K线。"""
    if ctx.index < 25:
        return None

    frame = ctx.frame

    # 新增过滤1：近10个交易日涨幅 > 50%，且收盘价下跌次数 >= 3，则放弃
    recent_10 = frame.iloc[max(0, ctx.index - 10):ctx.index + 1]  # 含当前，共11行，形成10个日涨跌
    if len(recent_10) >= 2:
        rise_10 = (ctx.close() / float(recent_10["close"].iloc[0]) - 1) * 100
        down_count = int((recent_10["close"].diff() < 0).sum())
        if rise_10 > 50 and down_count >= 3:
            return None

    # 新增过滤2：昨日收盘 < 截至昨日的5日均价，且今日当前价 < 5日均价，且今日下跌，则放弃
    if ctx.index >= 1:
        yesterday_close = float(frame["close"].iloc[ctx.index - 1])
        ma5_yesterday = float(
            frame["close"].iloc[max(0, ctx.index - 5):ctx.index].mean()
        )
        today_close = ctx.close()
        if (
            yesterday_close < ma5_yesterday
            and today_close < ma5_yesterday
            and today_close < yesterday_close
        ):
            return None

    # 原有逻辑
    window = frame.iloc[max(0, ctx.index - 20):ctx.index]
    box_high = float(window["high"].max())
    box_low = float(window["low"].min())
    in_box = box_low * 1.01 <= ctx.close() <= box_high * 0.99

    rally = (ctx.close()
             / float(frame["close"].iloc[max(0, ctx.index - 25)]) - 1) * 100
    has_board = _recent_limit_up(
        ctx, ctx.params.limit_up_tolerance) is not None
    strong_first_wave = rally >= 20 or has_board

    return _finish(
        "B8", "飞龙在天（箱体突破）", "buy",
        [
            _cond("第一波上涨有力度（≥20% 或含涨停）", strong_first_wave,
                  f"近25日累计 {rally:+.1f}%，"
                  f"{'含涨停' if has_board else '无涨停'}"),
            _cond("当前处于横盘箱体", in_box,
                  f"箱体 {box_low:.2f}~{box_high:.2f}，现价 {ctx.close():.2f}"),
            _cond("箱体内出现长下影K线", ctx.flags()["long_lower_shadow"],
                  f"下影 {min(ctx.open(), ctx.close()) - ctx.low():.2f}"),
        ],
        reason="激进者长下影当日尾盘买入；稳健者次日突破箱体上沿买入",
        entry=box_high, stop_loss=ctx.low() * 0.98,
        require_all=False)


def signal_b9(ctx: DailyContext) -> DailySignalItem | None:
    """B9 龙回头：第一波后 A 字回落 + 量能萎缩 + 长下影 + 次日±2%小实体确认。"""
    if ctx.index < 25:
        return None
    frame = ctx.frame
    peak_index = max(range(max(0, ctx.index - 25), ctx.index + 1),
                     key=lambda i: float(frame["high"].iloc[i]))
    drop_from_peak = (ctx.close() / float(frame["high"].iloc[peak_index]) - 1) * 100
    volume_shrink = all(
        vol.is_ladder_down(frame, i, 2) or vol.is_shrink_volume(frame, i)
        for i in range(max(1, ctx.index - 3), ctx.index + 1))
    prior_shadow = vol.is_long_lower_shadow(frame, ctx.index - 1,
                                           ctx.params.tail_shadow_x) \
        if ctx.index >= 1 else False
    small_body = abs(ctx.change()) <= 2.0
    return _finish(
        "B9", "龙回头（龙头第二波）", "buy",
        [
            _cond("A字型回落（自高点回撤≥15%）", drop_from_peak <= -15,
                  f"自高点 {drop_from_peak:+.1f}%"),
            _cond("成交量逐步萎缩", volume_shrink, "近4日量能递减"),
            _cond("出现长下影K线", prior_shadow or ctx.flags()["long_lower_shadow"],
                  "昨日长下影" if prior_shadow else "今日长下影"),
            _cond("次日涨跌幅在±2%内（支撑有效）", small_body,
                  f"今日 {ctx.change():+.2f}%"),
        ],
        reason="龙头第二波：确认日尾盘买入，跌破下影线最低价止损",
        entry=ctx.close(), stop_loss=ctx.low() * 0.99,
        require_all=False)


def signal_b10(ctx: DailyContext) -> DailySignalItem | None:
    """B10 涨停连阳线：涨停后5日线上方小阳/十字星横盘 + 地量。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset < 3:
        return None
    frame = ctx.frame
    ma5 = frame["close"].rolling(params.ma_short).mean()
    above_ma5 = all(
        float(frame["close"].iloc[i]) >= float(ma5.iloc[i])
        for i in range(ctx.index - min(5, ctx.index), ctx.index + 1))
    window_peak = max(ctx.volume(k) for k in range(0, min(offset + 1, ctx.index + 1)))
    ground = ctx.volume(0) <= window_peak * 0.5
    small_body = abs(ctx.change()) <= 3.0
    return _finish(
        "B10", "涨停连阳线", "buy",
        [
            _cond("近期涨停", True, f"{ctx.date(offset)}"),
            _cond(f"{params.ma_short}日线上方横盘整理", above_ma5,
                  f"现价 {ctx.close():.2f} vs MA{params.ma_short} "
                  f"{float(ma5.iloc[ctx.index]):.2f}"),
            _cond("小阳/十字星横盘", small_body, f"今日 {ctx.change():+.2f}%"),
            _cond("量缩至区间最高量一半以下（地量K线）", ground,
                  f"当前量 {ctx.volume(0):.0f} vs 区间峰 {window_peak:.0f}"),
        ],
        reason="地量当日尾盘或次日逢低低吸；跌破5日线止损",
        entry=ctx.close(),
        stop_loss=(float(ma5.iloc[ctx.index])
                   if ma5.iloc[ctx.index] == ma5.iloc[ctx.index] else None),
        require_all=False)


def signal_b11(ctx: DailyContext) -> DailySignalItem | None:
    """B11 三阴不破阳：涨停后3根缩量小阴不破涨停最低价。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset < 3 or ctx.index < 4:
        return None
    frame = ctx.frame
    board_index = ctx.index - offset
    board_low = float(frame["low"].iloc[board_index])
    three_yin = all(
        not vol.is_yang(frame, i) for i in range(board_index + 1, board_index + 4))
    hold = all(float(frame["low"].iloc[i]) >= board_low
               for i in range(board_index + 1, board_index + 4))
    shrink = all(vol.is_shrink_volume(frame, i)
                 for i in range(board_index + 1, board_index + 4))
    breakout = ctx.high() > float(frame["high"].iloc[board_index])
    expanded = not vol.is_shrink_volume(frame, ctx.index)
    return _finish(
        "B11", "三阴不破阳", "buy",
        [
            _cond("涨停后连续3根小阴线", three_yin,
                  "三阴涨跌幅 "
                  + "/".join(f"{vol.pct_change(frame, i):+.2f}%"
                             for i in range(board_index + 1,
                                            board_index + 4))),
            _cond("三阴不破涨停板最低价", hold, f"涨停低点 {board_low:.2f}"),
            _cond("三阴量能萎缩", shrink,
                  "三阴量 "
                  + "/".join(f"{vol._vol(frame, i):.0f}"
                             for i in range(board_index + 1,
                                            board_index + 4))),
            _cond("放量突破1号K线(涨停)最高点", breakout and expanded,
                  f"最高 {ctx.high():.2f} vs 涨停高 "
                  f"{float(frame['high'].iloc[board_index]):.2f}"),
        ],
        reason="缩量三阴洗盘后放量突破涨停高点为买点；跌破3号K线最低点止损",
        entry=ctx.close(), stop_loss=float(frame["low"].iloc[ctx.index - 1]),
        require_all=False)


def signal_b12(ctx: DailyContext) -> DailySignalItem | None:
    """B12 涨停揉搓线：涨停次日长上影（试盘）+ 长下影（承接）。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None:
        return None
    frame = ctx.frame
    board_index = ctx.index - offset
    if board_index < 1 or board_index + 1 > ctx.index:
        return None
    rub_index = board_index + 1
    upper = float(frame["high"].iloc[rub_index]) - max(
        float(frame["open"].iloc[rub_index]), float(frame["close"].iloc[rub_index]))
    body = vol.body_size(frame, rub_index)
    lower = min(float(frame["open"].iloc[rub_index]),
                float(frame["close"].iloc[rub_index])) - float(frame["low"].iloc[rub_index])
    upper_long = upper >= body * 1.0
    lower_long = lower >= body * params.tail_shadow_x if body > 0 else lower > 0
    hold_lower = all(float(frame["low"].iloc[i]) >= float(frame["low"].iloc[rub_index])
                     for i in range(rub_index, ctx.index + 1))
    return _finish(
        "B12", "涨停揉搓线", "buy",
        [
            _cond("涨停次日出现长上影（拉高试盘）", upper_long,
                  f"上影 {upper:.2f}"),
            _cond("同K线出现长下影（打压有承接）", lower_long,
                  f"下影 {lower:.2f}"),
            _cond("后续未跌破长下影最低价", hold_lower,
                  f"长下影低点 {float(frame['low'].iloc[rub_index]):.2f}"),
        ],
        reason="长下影之后逢低低吸；跌破长下影K线最低价止损",
        entry=ctx.close(), stop_loss=float(frame["low"].iloc[rub_index]),
        require_all=False)


def signal_b13(ctx: DailyContext) -> DailySignalItem | None:
    """B13 涨停缩倍量：涨停后5日内量缩至最高量一半以下，再放量突破涨停收盘价。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset > 5 or offset < 2:
        return None
    frame = ctx.frame
    board_index = ctx.index - offset
    board_close = float(frame["close"].iloc[board_index])
    peak = max(ctx.volume(k) for k in range(0, min(offset + 1, ctx.index + 1)))
    shrink = ctx.volume(0) <= peak * params.shrink_ratio
    breakout = ctx.close() > board_close and not vol.is_shrink_volume(frame, ctx.index)
    return _finish(
        "B13", "涨停缩倍量", "buy",
        [
            _cond("近期放量换手板涨停", True, f"{ctx.date(offset)}"),
            _cond(f"5日内量缩至最高量{params.shrink_ratio:g}倍以下", shrink,
                  f"当前 {ctx.volume(0):.0f} vs 峰 {peak:.0f}"),
            _cond("放量突破换手板收盘价", breakout,
                  f"现价 {ctx.close():.2f} vs 涨停收盘 {board_close:.2f}"),
        ],
        reason="再次放量突破换手板收盘价为买点；跌破缩量回踩最低点止损",
        entry=ctx.close(), stop_loss=min(
            float(frame["low"].iloc[i]) for i in range(board_index, ctx.index + 1)),
        require_all=False)


def signal_b14(ctx: DailyContext) -> DailySignalItem | None:
    """B14 潜伏首板：大阳/涨停后资金连续吸筹（横盘+量柱连续）再缩量。"""
    if ctx.index < 20:
        return None
    frame = ctx.frame
    window = frame.iloc[max(0, ctx.index - 15):ctx.index]
    if window.empty:
        return None
    high = float(window["high"].max())
    low = float(window["low"].min())
    base = ctx.close()
    flat = base > 0 and (high - low) / base * 100 <= 12.0
    had_big = any(
        vol.pct_change(frame, i) >= ctx.params.big_bar_pct
        for i in range(max(0, ctx.index - 20), ctx.index + 1))
    shrink_now = vol.is_shrink_volume(frame, ctx.index)
    return _finish(
        "B14", "潜伏首板", "buy",
        [
            _cond("先有大阳线或涨停板", had_big, "近20日出现大阳"),
            _cond("洗盘期横盘（振幅≤12%）", flat,
                  f"近15日振幅 {(high - low) / base * 100:.1f}%"),
            _cond("洗盘末端缩量", shrink_now,
                  f"量比 {ctx.volume(0) / max(ctx.volume(1), 1):.2f}"),
        ],
        reason="吸筹城墙 + 缩量末端 = 大阳或涨停启动",
        entry=ctx.close(), stop_loss=low,
        gaps=["板块内多股同步吸筹需板块成分数据，当前数据源不可得，未做板块加权"],
        require_all=False)


def signal_b15(ctx: DailyContext) -> DailySignalItem | None:
    """B15 一进二（首板→二板）：有主力痕迹 + 题材无跌停 + 首板封板质量高。"""
    params = ctx.params
    offset = _recent_limit_up(ctx, params.limit_up_tolerance)
    if offset is None or offset > 5:
        return None
    frame = ctx.frame
    board_index = ctx.index - offset
    before = frame.iloc[max(0, board_index - 10):board_index]
    had_force = (not before.empty) and any(
        vol.pct_change(frame, i) >= params.big_bar_pct
        for i in range(max(0, board_index - 10), board_index))
    board_high = float(frame["high"].iloc[board_index])
    board_range = board_high - float(frame["low"].iloc[board_index])
    board_close = float(frame["close"].iloc[board_index])
    # 封板质量代理：收盘价位于当日振幅上沿（越贴近最高价，封板越实）
    quality = (board_range > 0
               and board_close >= board_high - board_range * 0.15)
    return _finish(
        "B15", "一进二（首板→二板）", "buy",
        [
            _cond("有主力资金痕迹（首板前10日出现大阳）", had_force,
                  "已出现" if had_force else "未见"),
            _cond("首板封板质量（收盘贴近当日最高的代理指标）", quality,
                  f"收盘 {board_close:.2f} vs 最高 {board_high:.2f}"
                  f"（精确封单需Level-2）", required=False),
            _cond("打板风控：成交量不超过近期最大量",
                  ctx.volume(0) <= max(
                      [ctx.volume(k) for k in range(1, min(20, ctx.index + 1))] or [0]),
                  f"当前量 {ctx.volume(0):.0f}"),
        ],
        reason="首板→二板的竞价/盘中/打板三类买点",
        gaps=["竞价模式、分时九类稳定结构、封单量需 Level-2 与竞价数据，"
              "日线无法完整复现；当前仅给出日线可判定的部分条件"],
        require_all=False)


# ==================================================================
# 卖出 / 风控信号库 S1–S6
# ==================================================================

def signal_s2(ctx: DailyContext) -> DailySignalItem | None:
    """S2 日线级别卖点：爆量离场 / 锁筹跳水（平台跌破）。"""
    if ctx.index < 25:
        return None
    frame = ctx.frame
    explode = ctx.flags()["explode_volume"]
    platform_low = float(frame["low"].iloc[ctx.index - 20:ctx.index].min())
    breakdown = ctx.close() < platform_low and ctx.volume() > ctx.volume(1)
    return _finish(
        "S2", "日线级别卖点", "sell",
        [
            _cond("爆量（量>近20日最大量×1.5）→ 调整开始", explode,
                  f"当前量 {ctx.volume():.0f}"),
            _cond("平台跌破（锁筹跳水）", breakdown,
                  f"现价 {ctx.close():.2f} vs 平台下沿 {platform_low:.2f}"),
        ],
        reason="爆量往往意味着调整开始；平台跌破往往大跌 → 离场",
        gaps=["平/低开跳水、高开跳水属竞价行为，需竞价数据"],
        require_all=False)


def signal_s3(ctx: DailyContext) -> DailySignalItem | None:
    """S3 缩量阴线风控法：当日缩量阴线 → 当天不抄底。"""
    if ctx.index < 2:
        return None
    conditions = [
        _cond("当日收阴", not ctx.is_yang(), f"{ctx.change():+.2f}%"),
        _cond("当日缩量", ctx.flags()["shrink_volume"],
              f"量比 {ctx.volume() / max(ctx.volume(1), 1):.2f}"),
    ]
    item = _finish(
        "S3", "缩量阴线风控（当天不抄底）", "risk", conditions,
        reason="缩量阴线=洗盘未达效果，次日盘中大概率跌破当日收盘 → 当天不买入",
        require_all=True)
    if item.triggered:
        item.entry = ctx.close()
        item.stop_loss = ctx.close()
    return item


def signal_s4(ctx: DailyContext) -> DailySignalItem | None:
    """S4 缩量阳线持股法：当日缩量阳线 → 当天不卖出。"""
    if ctx.index < 2:
        return None
    conditions = [
        _cond("当日收阳", ctx.is_yang(), f"{ctx.change():+.2f}%"),
        _cond("当日缩量", ctx.flags()["shrink_volume"],
              f"量比 {ctx.volume() / max(ctx.volume(1), 1):.2f}"),
    ]
    return _finish(
        "S4", "缩量阳线持股（当天不卖）", "watch", conditions,
        reason="缩量阳线=拉升未达主力目标位，次日盘中大概率突破当日收盘 → 当天不卖",
        require_all=True)


def signal_s6(ctx: DailyContext) -> DailySignalItem | None:
    """S6 高量纪律（逐条命中情况，命中即触发风控提示）。"""
    from src.intraday.volume import high_volume_discipline

    hits = high_volume_discipline(ctx.frame, ctx.anchors, ctx.params)
    conditions = [
        _cond("高量破/遇顶不过/高低点下移等纪律", bool(hits),
              "；".join(hits) if hits else "未命中任何高量纪律")
    ] if hits else [
        _cond("高量纪律", False, "未命中任何高量纪律条目")
    ]
    item = _finish(
        "S6", "高量纪律", "risk", conditions,
        reason="；".join(hits) if hits else "高量纪律未触发",
        require_all=False)
    item.triggered = bool(hits)
    item.score = 1.0 if hits else 0.0
    return item


def signal_s5(ctx: DailyContext) -> DailySignalItem | None:
    """S5 拉升达标止盈：当日大涨或触及区间上沿 → 做T仓位兑现离场。

    ## 为什么卖点库必须有这一条（实测数据支撑）

    2026-09-17 实测：10 只票各 30 个交易日里，卖出侧只触发了 S3(73 条) 与 S6(50 条)
    共 123 条**风控**，**真卖点（S2）0 条**。原因是 S2 要求「爆量 1.5 倍」或
    「跌破 20 日平台」——震荡市里这两个条件几乎不可能同时具备。
    而做T的本质是**赚波动的钱**：涨到目标就该兑现，不必等到破位。
    S5 补的正是这条：**上涨侧的离场依据**（S2 只管下跌侧）。

    判据（满足任一即触发，两条权重相同）：
      1. 当日涨幅 ≥ 5%（`big_yang` 量价形态）：一根大阳线之后短线容易回吐；
      2. 现价处于区间**高位**（位置分位 ≥ 0.85）且收阴：
         高位滞涨/上影说明上方抛压重，先落袋。
    """
    if ctx.index < 2:
        return None
    change = ctx.change()
    threshold = 5.0
    big_up = change >= threshold
    position = ctx.position
    at_high = bool(position and position.percentile >= 0.85)
    stall = at_high and not ctx.is_yang()
    conditions = [
        _cond(f"当日大涨（≥{threshold:.0f}%）→ 短线易回吐", big_up,
              f"{change:+.2f}%"),
        _cond("高位滞涨（区间分位≥85% 且收阴）→ 上方抛压重", stall,
              f"分位 {position.percentile * 100:.0f}%" if position else "无位置数据"),
    ]
    item = _finish(
        "S5", "拉升达标止盈（做T兑现）", "sell", conditions,
        reason="做T赚的是波动：涨到目标先兑现，不等破位；高位收阴说明抛压转强",
        require_all=False)
    # 只要任一条件成立就算触发（`require_all=False` 的默认门槛是 0.8，
    # 两条条件里命中一条只有 0.5，达不到）→ 这里显式收紧/放宽判定。
    if big_up or stall:
        item.triggered = True
        item.score = 1.0 if (big_up and stall) else 0.75
    if item.triggered:
        # 止盈参考位：当日收盘（次日开盘附近兑现），风控线给近 5 日低点
        item.entry = ctx.close()
        recent_low = float(ctx.frame["low"].iloc[max(0, ctx.index - 4):ctx.index + 1].min())
        item.stop_loss = round(recent_low, 2)
    return item


def signal_s7(ctx: DailyContext) -> DailySignalItem | None:
    """S7 量价背离（价新高、量不跟）→ 卖点。

    判据（**全部满足**，避免把正常缩量上涨误判成背离）：
      1. 现价创近 20 日新高；
      2. 当日量 **低于**近 20 日最大量的 70%（价涨量缩 = 上攻乏力）；
      3. 当日收阴或留长上影（冲高回落才算背离确认，继续涨停不算）。
    """
    if ctx.index < 21:
        return None
    window = ctx.frame.iloc[ctx.index - 20:ctx.index + 1]
    price_new_high = ctx.close() >= float(window["close"].max()) - 1e-9
    peak = float(window["volume"].max())
    shrink = peak > 0 and ctx.volume() < peak * 0.7
    reversal = (not ctx.is_yang()) or bool(ctx.flags()["long_lower_shadow"])
    conditions = [
        _cond("价格创近 20 日新高", price_new_high, f"收盘 {ctx.close():.2f}"),
        _cond("量能未跟（< 20 日峰量 70%）", shrink,
              f"量/峰量 {ctx.volume() / peak:.2f}" if peak > 0 else "无峰量"),
        # 每条条件都必须带 actual（项目契约：前端要显示"差在哪"，
        # 空字符串会让 test_signals_expose_condition_actuals 直接失败）
        _cond("冲高回落（收阴或长上影）", reversal,
              ("收阴" if not ctx.is_yang() else "长上影")
              + f"｜涨跌 {ctx.change():+.2f}%"),
    ]
    item = _finish(
        "S7", "量价背离（新高无量）", "sell", conditions,
        reason="价创新高而量跟不上，且当日冲高回落 → 上攻动能衰竭，先减仓",
        require_all=True)
    if item.triggered:
        item.entry = ctx.close()
        item.stop_loss = round(float(window["low"].min()), 2)
    return item


ALL_BUY_SIGNALS: list[SignalFn] = [
    signal_b1, signal_b2, signal_b3, signal_b4, signal_b5, signal_b6,
    signal_b7, signal_b8, signal_b9, signal_b10, signal_b11, signal_b12,
    signal_b13, signal_b14, signal_b15,
]

#: 卖出/风控信号库。
#:
#: `kind` 决定前端"方向"列的语义：
#:   - `sell`：真卖点（该走）—— S2 下跌侧、S5 上涨侧、S7 量价背离；
#:   - `risk`：风控提示（别买/别抄底/守纪律）—— S3、S6；
#:   - `watch`：不卖提示 —— S4。
#:
#: **S1 刻意不实现**：「S1 分时三类卖点」依赖分时/盘口数据，日线无法复现，
#: 硬做出来只会是另一个规则冒充分时卖点（见模块 docstring 的取舍说明）。
ALL_SELL_SIGNALS: list[SignalFn] = [
    signal_s2, signal_s3, signal_s5, signal_s6, signal_s7,
]


def run_daily_signals(ctx: DailyContext) -> dict[str, list[DailySignalItem]]:
    """跑全部日K信号，返回按类别分组的、**已触发优先**排序的列表。"""
    buy: list[DailySignalItem] = []
    sell: list[DailySignalItem] = []
    for function in ALL_BUY_SIGNALS:
        try:
            item = function(ctx)
        except Exception:  # noqa: BLE001 单条规则异常不应打断整个面板
            continue
        if item is not None:
            buy.append(item)
    for function in ALL_SELL_SIGNALS:
        try:
            item = function(ctx)
        except Exception:  # noqa: BLE001
            continue
        if item is not None:
            sell.append(item)
    # S4 是「不卖」提示，归入 watch 类一并展示
    s4 = signal_s4(ctx)
    if s4 is not None:
        sell.append(s4)
    buy.sort(key=lambda item: (not item.triggered, -item.score))
    sell.sort(key=lambda item: (not item.triggered, -item.score))
    return {"buy": buy, "sell": sell}


def build_verdict(buy: list[DailySignalItem], sell: list[DailySignalItem],
                  discipline: list[str], pattern,
                  position) -> str:
    """§10 组合与优先级：先定趋势 → 再定位置 → 高量定锚 → 信号 → 风控。"""
    triggered_buy = [item.code for item in buy if item.triggered]
    triggered_risk = [item.code for item in sell
                      if item.triggered and item.kind == "risk"]
    parts: list[str] = []
    if position is not None:
        parts.append(f"{position.label}（分位 {position.percentile * 100:.0f}%）")
    if pattern is not None:
        parts.append(f"最近量价形态：{pattern.name}（{pattern.signal}）")
    if discipline:
        parts.append(f"⚠️ 高量纪律命中 {len(discipline)} 条")
    if triggered_buy and triggered_risk:
        parts.append(f"买点信号 {','.join(triggered_buy)} 与风控信号 "
                     f"{','.join(triggered_risk)} 矛盾 → 依需求「技术矛盾优先观望」")
    elif triggered_buy:
        parts.append(f"买入信号触发：{','.join(triggered_buy)}")
    elif triggered_risk:
        parts.append(f"风控信号触发：{','.join(triggered_risk)}，不新增仓位")
    else:
        parts.append("无信号触发，继续观察")
    return "；".join(parts)
