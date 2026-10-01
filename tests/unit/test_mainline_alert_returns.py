"""信号收益追踪面板（`src.mainline.alert_returns`）单元测试。

## 这里盯住的是三个**静默失效**的口径问题

它们都不会抛异常、界面上也照样出数，所以只有测试能守住：

1. **折叠基准**。仓储 `load_alerts` 返回的是 `ORDER BY trade_date DESC`
   （"新的在前"给流水页签用），而折叠必须**按时间升序**推进。照传入顺序处理时
   每个板块先遇到最新那条，后续都是负数间隔 → 去重**恒不生效**
   （实测 1965 条一条都没折）。`test_cluster_is_order_independent` 守住它。
2. **窗口没走满不能给最终值**。数据到 2026-09-23 为止，2026-08-25 之后的信号连
   60 个交易日都没走完。若照常输出"60 日最大涨幅"，那个数每天都在变，
   和历史行放在同一列里不可比。`test_partial_window_leaves_final_blank` 守住它。
3. **"亏不亏钱"不能用最大涨幅判**。窗口内最高价高于信号日收盘几乎是必然事件
   （实测按"最大涨幅 > 0"算胜率 97%，133/134 个板块全部通过），所以板块筛选走
   **实际收益**。`test_pass_filter_ignores_max_gain` 守住它。
4. **筛选口径是「20 日胜率 > 门槛」，不是「20 日平均实际收益 ≥ 0」。** 两者不是
   同一件事：样本 `+10/+1/+1/-6/-7` 胜率 60%、均值为负（**显示**），
   `+30/-1/-2` 均值为正、胜率 33%（**隐藏**）。
   `test_gate_follows_win_rate_not_average_return` 把这两个方向都钉死 ——
   只测其中一边的话，把口径写回"平均收益"也能过。
5. **比较是严格大于。** 恰好等于门槛的板块**隐藏**。
   `test_exactly_at_threshold_is_hidden` 守住这条边界 —— 写成 `>=` 不会报错、
   只会让一批题材悄悄回来。默认门槛见 `DEFAULT_MIN_WIN_RATE`
   （2026-09-25 起 40%，**2026-09-30 起 39%**）。
6. **判不出胜率的不隐藏。** 一条 20 日窗口都没走满的板块没有胜率，
   谈不上"未过门槛"。若把"判不出"当"不通过"，最近一个月的板块会整片消失。
   `test_pending_gate_is_not_hidden` 守住它。
7. **展示门槛与池级门槛已经解耦，且必须保持解耦。** 放宽展示门槛时若"顺手"
   把 `theme_gate.DEFAULT_THRESHOLD` 也降下来，下次生成剔除清单会把胜率落在
   (0.39, 0.40] 的题材也剔掉 —— 而它们不在冻结名单里，**池子会静默缩水**，
   `--keep` 也留不住。`test_display_gate_is_decoupled_from_pool_gate` 守住它。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.mainline.alert_returns import (
    DEFAULT_HORIZONS,
    DEFAULT_MIN_WIN_RATE,
    AlertReturnRow,
    WindowStat,
    build,
    build_rows,
    cluster_alerts,
    summarize_boards,
)
from src.mainline.models import BoardBar, BoardSeries


def calendar_of(count: int, start: str = "20240101") -> list[str]:
    """造一串连续交易日（合成日历，只用来定位下标，不含节假日语义）。"""
    begin = datetime.strptime(start, "%Y%m%d")
    return [(begin + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(count)]


def bar(date: str, high: float, low: float, close: float) -> BoardBar:
    return BoardBar(date=date, open=close, high=high, low=low, close=close,
                    pre_close=close)


class FakeStore:
    """最小数据仓替身：只实现本模块用到的方法（不碰真实 sqlite）。"""

    def __init__(self, days: list[str], bars: dict[str, list[BoardBar]],
                 members: dict[str, list[str]] | None = None) -> None:
        self._days = days
        self._bars = bars
        self._members = members or {}
        self.warehouse = None          # 领涨股段会如实报"行情仓不可用"

    def calendar(self, start: str = "", end: str = "") -> list[str]:
        days = self._days
        if start:
            days = [day for day in days if day >= start]
        if end:
            days = [day for day in days if day <= end]
        return days

    def board_bars(self, codes, *, start: str, end: str) -> dict[str, BoardSeries]:
        out: dict[str, BoardSeries] = {}
        for code in codes:
            items = self._bars.get(code)
            if not items:
                continue
            series = BoardSeries(code=code, name=code, source="test")
            series.bars.extend(item for item in items if start <= item.date <= end)
            out[code] = series
        return out

    def members(self, board_code: str):
        return [{"code": code} for code in self._members.get(board_code, [])]

    def pure_member_relevance(self):
        return {}


def alert(date: str, board: str = "886042.TI", level: str = "strong",
          score: float = 82.0, entry: float = 100.0) -> dict:
    return {"trade_date": date, "board_code": board, "board_name": "存储芯片",
            "level": level, "score": score, "entry_close": entry,
            "payload": {"level_label": "🔴 强信号", "ret_20d": 3.1,
                        "reasons": ["r1"]}}


# ==================================================================
# 折叠（X2 / X3）
# ==================================================================


def test_folds_repeats_within_window() -> None:
    days = calendar_of(30)
    # 第 0 天触发，第 5 天再触发（在 10 个交易日内）→ 折成一条 X2
    periods = cluster_alerts([alert(days[0]), alert(days[5])], days, dedup_days=10)
    assert len(periods) == 1
    assert periods[0]["signal_date"] == days[0]     # 保留**首次**触发日
    assert periods[0]["alert_count"] == 2
    assert periods[0]["alert_dates"] == [days[0], days[5]]


def test_new_period_beyond_window() -> None:
    days = calendar_of(40)
    # 第 11 个交易日再触发 → 超出 10 日窗口，算新周期
    periods = cluster_alerts([alert(days[0]), alert(days[11])], days, dedup_days=10)
    assert [item["signal_date"] for item in periods] == [days[0], days[11]]
    assert [item["alert_count"] for item in periods] == [1, 1]


def test_cluster_is_order_independent() -> None:
    """**回归测试**：传入顺序不能影响折叠结果。

    仓储返回降序（`ORDER BY trade_date DESC`）时，旧实现一条都折不上 ——
    界面上只是"X2/X3 从不出现"，看不出是 bug。
    """
    days = calendar_of(40)
    items = [alert(days[0]), alert(days[4]), alert(days[8]), alert(days[20])]
    ascending = cluster_alerts(items, days, dedup_days=10)
    descending = cluster_alerts(list(reversed(items)), days, dedup_days=10)
    assert [item["signal_date"] for item in ascending] == \
        [item["signal_date"] for item in descending]
    assert [item["alert_count"] for item in ascending] == [3, 1]
    assert [item["alert_count"] for item in descending] == [3, 1]


def test_chained_repeats_do_not_collapse_forever() -> None:
    """折叠基准是**周期内的第一次**触发，不是上一次。

    按"距上一次 ≤10 日"链式折叠，每 8 天报一次的概念会被无限折成一条
    （跨度半年也只剩一行），面板上就看不到"最近又起了一波"。
    """
    days = calendar_of(120)
    items = [alert(days[index * 8]) for index in range(10)]   # 每 8 个交易日一次
    periods = cluster_alerts(items, days, dedup_days=10)
    assert len(periods) == 5                    # 每 2 次触发折成一个周期
    assert periods[0]["alert_count"] == 2
    assert all(item["alert_count"] == 2 for item in periods)


def test_different_boards_never_fold() -> None:
    days = calendar_of(20)
    periods = cluster_alerts([alert(days[0], "886042.TI"),
                              alert(days[1], "885908.TI")], days, dedup_days=10)
    assert len(periods) == 2


# ==================================================================
# 各周期最大涨幅 / 实际收益 / 浮亏
# ==================================================================


def test_window_full_gives_final_values() -> None:
    days = calendar_of(80)
    bars = [bar(days[0], high=100.0, low=99.0, close=100.0)]
    # 之后 10 个交易日：最高冲到 130，最低回踩 95，第 10 日收盘 120
    for offset in range(1, 11):
        high = 130.0 if offset == 4 else 110.0
        low = 95.0 if offset == 7 else 105.0
        close = 120.0 if offset == 10 else 108.0
        bars.append(bar(days[offset], high=high, low=low, close=close))
    store = FakeStore(days, {"886042.TI": bars})

    rows, gaps = build_rows([alert(days[0])], store, calendar=days,
                            horizons=(7, 20), dedup_days=10)
    assert gaps == []
    row = rows[0]
    stat7 = row.windows[7]
    assert stat7.status == "done"
    assert stat7.max_gain_pct == 30.0            # 130/100-1
    assert stat7.max_gain_date == days[4]
    assert stat7.ret_pct == 8.0                  # 窗口末（第 7 根）收盘 108 → +8%
    assert stat7.worst_pct == -5.0               # 窗口内最低 95 → 95/100-1
    assert stat7.remaining_days == 0
    # 20 日窗口只有 10 根K线 → 走不满
    stat20 = row.windows[20]
    assert stat20.status == "partial"
    assert stat20.max_gain_pct is None           # 最终值**不填**
    assert stat20.current_gain_pct == 30.0       # 但进度如实给
    assert stat20.remaining_days == 10


def test_partial_window_leaves_final_blank() -> None:
    """**回归测试**：数据末端附近的行不能拿"未走完"冒充"走满了"。"""
    days = calendar_of(30)
    bars = [bar(days[0], high=100.0, low=100.0, close=100.0)]
    for offset in range(1, 4):
        bars.append(bar(days[offset], high=105.0, low=101.0, close=104.0))
    store = FakeStore(days, {"886042.TI": bars})

    rows, _ = build_rows([alert(days[0])], store, calendar=days, horizons=(7, 20, 60))
    for horizon in (7, 20, 60):
        stat = rows[0].windows[horizon]
        assert stat.status == "partial"
        assert stat.max_gain_pct is None, f"{horizon} 日窗口没走满却给了最终值"
        assert stat.ret_pct is None
        assert stat.current_gain_pct == 5.0
        assert stat.remaining_days == horizon - 3


def test_pending_when_no_bar_after_signal() -> None:
    days = calendar_of(30)
    store = FakeStore(days, {"886042.TI": [bar(days[0], 100.0, 100.0, 100.0)]})
    rows, _ = build_rows([alert(days[0])], store, calendar=days, horizons=(7,))
    stat = rows[0].windows[7]
    assert stat.status == "pending"
    assert stat.max_gain_pct is None
    assert stat.current_gain_pct is None
    assert stat.bars_used == 0


def test_segment_split_and_newest_first() -> None:
    days = calendar_of(90)
    bars = [bar(day, high=100.0 + index, low=100.0, close=100.0 + index)
            for index, day in enumerate(days)]
    store = FakeStore(days, {"886042.TI": bars, "885908.TI": bars})
    alerts = [alert(days[0]), alert(days[30]), alert(days[70], "885908.TI")]
    rows, _ = build_rows(alerts, store, calendar=days, horizons=(7,),
                         split=days[40])
    dates = [row.signal_date for row in rows]
    assert dates == sorted(dates, reverse=True), "表格必须新的在最上面"
    assert rows[0].segment == "recent"           # days[70] > 分界日
    assert {row.segment for row in rows[1:]} == {"history"}


def test_missing_board_bars_is_reported() -> None:
    days = calendar_of(20)
    store = FakeStore(days, {})
    rows, gaps = build_rows([alert(days[0])], store, calendar=days, horizons=(7,))
    assert len(rows) == 1
    assert rows[0].windows[7].status == "pending"
    assert gaps and "没有指数日线" in gaps[0]


# ==================================================================
# 板块筛选：20 日胜率门槛
# ==================================================================


def row_of(ret20: float | None, *, board: str = "886042.TI",
           date: str = "20240101", done: bool = True,
           level: str = "strong") -> AlertReturnRow:
    """造一行，只关心 20 日**实际收益** —— 板块筛选唯一看的就是它。

    直接拼 `AlertReturnRow` 而不是走 `build_rows`：门槛只看
    `windows[20].ret_pct`，用行情造数只会把"口径测试"埋进价格算式里。
    `done=False` 表示窗口没走满（`ret_pct` 为 None，但有"至今"进度）。
    """
    stat = WindowStat(days=20, status="done" if done else "partial",
                      ret_pct=ret20 if done else None,
                      current_ret_pct=ret20, max_gain_pct=10.0)
    return AlertReturnRow(signal_date=date, board_code=board,
                          board_name="存储芯片", level=level,
                          level_label="🔴 强信号", score=80.0,
                          windows={20: stat})


def test_pass_filter_ignores_max_gain() -> None:
    """**回归测试**：最大涨幅为正但实际亏损的板块必须判为不通过。

    这正是旧口径的坑：按"20 日内最大涨幅 > 0"筛，实测 133/134 个板块全部通过
    （窗口内总有上影线），筛选等于没筛，还会让人以为"这个系统几乎不亏钱"。
    """
    days = calendar_of(80)
    # 冲高 40% 后一路跌回，第 20 日收盘比信号日低 10%
    bars = [bar(days[0], high=100.0, low=100.0, close=100.0)]
    for offset in range(1, 21):
        high = 140.0 if offset == 2 else 101.0
        low = 90.0 if offset == 20 else 99.0
        close = 90.0 if offset == 20 else 100.0
        bars.append(bar(days[offset], high=high, low=low, close=close))
    store = FakeStore(days, {"886042.TI": bars})
    rows, _ = build_rows([alert(days[0])], store, calendar=days, horizons=(7, 20))

    stat = rows[0].windows[20]
    assert stat.status == "done"
    assert stat.max_gain_pct == 40.0             # 最大涨幅很漂亮
    assert stat.ret_pct == -10.0                 # 但持有到底是亏的

    boards = summarize_boards(rows)
    assert boards[0]["avg_gain_20d"] == 40.0
    assert boards[0]["avg_ret_20d"] == -10.0
    assert boards[0]["quality"] == "poor"
    assert boards[0]["win_rate_20d"] == 0.0      # 唯一一个窗口是亏的
    assert boards[0]["gate"] == "hidden"
    assert boards[0]["passed"] is False, '20 日胜率 0% 的板块不该通过筛选'
    assert boards[0]["worst_20d"] == -10.0


def test_gate_follows_win_rate_not_average_return() -> None:
    """**回归测试**：显示/隐藏只看 20 日胜率 —— 与"平均实际收益"解耦。

    两个方向都要测。只测一边的话，把判据写回"平均实际收益 ≥ 0"也能通过，
    而那条口径**恰好在这两个样本上给出相反的结果**：

    * `+10/+1/+1/-6/-7` → 胜率 60%、均值 **-0.2%** → 显示（旧口径会隐藏它）
    * `+30/-1/-2`       → 胜率 33%、均值 **+9.0%** → 隐藏（旧口径会显示它）
    """
    swing = summarize_boards(
        [row_of(value) for value in (10.0, 1.0, 1.0, -6.0, -7.0)])[0]
    assert swing["win_rate_20d"] == 0.6
    assert swing["avg_ret_20d"] == -0.2          # 均值为负也照样显示
    assert swing["gate"] == "pass"
    assert swing["passed"] is True, "胜率过半，不该因为均值为负被隐藏"

    outlier = summarize_boards([row_of(value) for value in (30.0, -1.0, -2.0)])[0]
    assert outlier["avg_ret_20d"] == 9.0         # 均值很漂亮
    assert outlier["win_rate_20d"] == 0.3333
    assert outlier["gate"] == "hidden"
    assert outlier["passed"] is False, "胜率 33% 未过半，均值再高也不显示"


def test_exactly_at_threshold_is_hidden() -> None:
    """**回归测试**：胜率**不超过**门槛 → 隐藏（比较是严格大于）。

    用户口径是"只显示 20 日胜率**大于**门槛的概念板块"。写成 `>=` 不会报错、
    界面也照样出数，只会让一批卡在门槛上的题材悄悄回到表里，
    所以这条边界必须由测试钉住。

    ⚠️ **判据用显式门槛，不用默认门槛** —— 默认门槛是 0.39，而 5 个窗口能表达的
    比例只有 0/20/40/…%，**取不到"恰好等于 0.39"的那一档**。早先版本拿
    `win_rate == 0.4` 去对默认门槛，默认值从 0.40 改成 0.39 之后它就不覆盖边界了
    （而且会静默变成"高于门槛却断言 hidden"的假红）。所以这里：
    用显式门槛 `0.5` 钉"恰好等于→隐藏"，用默认门槛钉"低于默认→隐藏"。
    """
    # ① 恰好等于显式门槛（2/5 = 40%，门槛 0.5 之下的另一档不算）
    at_half = summarize_boards(
        [row_of(value) for value in (10.0, 1.0, -6.0, -7.0)],
        min_win_rate=0.5)[0]
    assert at_half["win_rate_20d"] == 0.5
    assert at_half["gate"] == "hidden", "恰好等于显式门槛也算不达标"
    assert at_half["passed"] is False
    # 再高一个窗口就越过它 —— 两侧都要看得见
    over_half = summarize_boards(
        [row_of(value) for value in (10.0, 1.0, 1.0, -6.0, -7.0)],
        min_win_rate=0.5)[0]
    assert over_half["win_rate_20d"] == 0.6
    assert over_half["gate"] == "pass"

    # ② 默认门槛：明显不达标的一档必须隐藏（对**默认**取值生效的证据）
    below_default = summarize_boards(
        [row_of(value) for value in (10.0, -1.0, -2.0)])[0]   # 33%
    assert below_default["win_rate_20d"] == 0.3333
    assert below_default["gate"] == "hidden"
    # ③ 默认值本身要有出处：改了默认门槛，这条会红（提醒同步上面两条判据）
    assert DEFAULT_MIN_WIN_RATE == 0.39


def test_win_rate_threshold_is_configurable() -> None:
    """门槛是参数，不是一个写死的 0.5 —— 改门槛不该动别的口径。"""
    rows = [row_of(value) for value in (10.0, 1.0, -6.0, -7.0)]   # 胜率 50%
    assert summarize_boards(rows, min_win_rate=0.4)[0]["gate"] == "pass"
    assert summarize_boards(rows, min_win_rate=0.5)[0]["gate"] == "hidden"
    # 默认门槛：2026-09-25 用户按"减少假阳性"从 50% 放宽到 40%；
    # 2026-09-30 用户裁定再放宽到 **0.39**（让 CRO概念 0.3913 回到面板）。
    assert DEFAULT_MIN_WIN_RATE == 0.39


def test_display_gate_is_decoupled_from_pool_gate() -> None:
    """**回归测试**：展示门槛（39%）与池级门槛（40%）是两件事，不许合并。

    ## 为什么必须钉住

    2026-09-30 用户裁定把「回测收益展示」的门槛放宽到 **0.39**。
    而 `theme_gate.DEFAULT_THRESHOLD`（决定"哪些题材该被剔出池子"）当时**同值**，
    看起来"顺手一起改"很自然 —— 但那会造成**静默缩水**：

      · `select()` 会把胜率落在 `(0.39, 0.40]` 的题材也判成"该剔"；
      · 它们**不在冻结名单**里 ⇒ 下次 `import_crowding_pool` 时池子直接少几个题材；
      · `build_theme_exclusions.py --keep` **留不住** —— `--keep` 只在题材
        **已经**被判为剔除时才把它挪回观察名单，而"不在池内"是另一回事。
      · 本机实测（2026-09-30，池内 124 个）：门槛 >40% 时过门槛 **110** 个，
        放宽到 >39% 时 **112** 个 —— 多出来的正是这一档，其中就有 `CRO概念`。

    所以这条判据锁的是**两者可以不相等**：改任意一个都不该被另一个悄悄跟改。
    它不是"数值必须不同"（将来若有意统一，改这里并写清理由），
    而是"必须是两个独立的常量，且各自有出处"。
    """
    from src.mainline import theme_gate

    assert DEFAULT_MIN_WIN_RATE == 0.39, "展示门槛（面板显示）"
    assert theme_gate.DEFAULT_THRESHOLD == 0.40, "池级门槛（决定题材还做不做）"
    # 两处口径的**比较方向**必须一致：都是"严格大于才通过/保留"
    below = [row_of(value) for value in (10.0, 1.0, -6.0, -7.0)]     # 胜率 50%
    assert summarize_boards(below, min_win_rate=0.5)[0]["gate"] == "hidden"
    excluded, _ = theme_gate.select(
        [{"board_code": "886999.TI", "board_name": "边界题材", "win_rate_20d": 0.4,
          "done_20d": 5, "signals": 5, "alert_total": 5, "avg_ret_20d": 0.0}],
        threshold=0.4, min_samples=3)
    assert len(excluded) == 1, "恰好等于池级门槛也算不达标（与展示门槛同向）"


def test_pending_gate_is_not_hidden() -> None:
    """**回归测试**：没有一条 20 日窗口走满 → 胜率判不出来 → **不隐藏**。

    若把"判不出"当"不通过"，最近一个月的板块会被整片剔除，面板上只剩历史 ——
    这正是这条规则最容易踩的坑，而且界面上只会表现为"最近一个月的信号不见了"。
    """
    rows = [row_of(11.0, date="20240101", done=False),
            row_of(4.0, date="20240102", done=False)]
    board = summarize_boards(rows)[0]
    assert board["win_rate_20d"] is None
    assert board["done_20d"] == 0
    assert board["gate"] == "pending"            # 三态要能区分"判不出"与"不达标"
    assert board["passed"] is True, "判不出胜率的板块不该被当成不达标"
    assert board["avg_ret_20d_now"] == 7.5       # "至今"口径仍如实给出


def test_hidden_board_drops_all_its_rows_when_history_only() -> None:
    """`history_only` 走的是同一套 `passed`：隐藏的板块，它的行也不能漏出去。

    同时盯住"隐藏了多少"这几个计数 —— 前端要把它们写在界面上，
    数错会让用户以为告警丢了。
    """
    days = calendar_of(100)
    falling = [bar(day, high=100.0 - index, low=100.0 - index,
                   close=100.0 - index)
               for index, day in enumerate(days)]
    rising = [bar(day, high=100.0 + index, low=100.0 + index,
                  close=100.0 + index)
              for index, day in enumerate(days)]
    store = FakeStore(days, {"886042.TI": falling, "885908.TI": rising})
    alerts = [alert(days[0], "886042.TI"), alert(days[0], "885908.TI")]

    payload = build(alerts, store, horizons=(7, 20), split=days[50])
    # 载荷必须把门槛原样带出去：前端就是照着它渲染「> 40%」这句文案的
    assert payload["min_win_rate"] == DEFAULT_MIN_WIN_RATE
    assert payload["stats"]["min_win_rate"] == DEFAULT_MIN_WIN_RATE
    gates = {item["board_code"]: item["gate"] for item in payload["boards"]}
    # 一路下跌 → 20 日胜率 0% → 隐藏；一路上涨 → 100% → 显示
    assert gates == {"886042.TI": "hidden", "885908.TI": "pass"}
    assert payload["stats"]["boards"] == 2
    assert payload["stats"]["boards_passed"] == 1
    assert payload["stats"]["boards_hidden"] == 1
    assert payload["stats"]["boards_pending"] == 0
    assert payload["stats"]["win_rate_hidden"] == 1
    assert payload["stats"]["history_hidden"] == 1

    # 未筛的载荷里两行都在（前端按 boards[].passed 本地筛）；
    # `history_only=true` 时被隐藏板块的行必须一起消失。
    assert {row["board_code"] for row in payload["rows"]} == \
        {"886042.TI", "885908.TI"}
    payload2 = build(alerts, store, horizons=(7, 20), split=days[50],
                     history_only=True)
    assert {row["board_code"] for row in payload2["rows"]} == {"885908.TI"}


def test_build_drops_pool_excluded_boards() -> None:
    """**池级剔除**的题材在回测展示里也不出现（汇总行 + 逐条行一起摘）。

    否则"清单外的题材不显示"只在打分与告警上生效、报表里却还列着，
    用户只会认为这条规则没生效 —— 而这正是最难自查的一类不一致。
    """
    days = calendar_of(100)
    rising = [bar(day, high=100.0 + index, low=100.0 + index,
                  close=100.0 + index) for index, day in enumerate(days)]
    store = FakeStore(days, {"886042.TI": rising, "885908.TI": rising})
    alerts = [alert(days[0], "886042.TI"), alert(days[0], "885908.TI")]

    both = build(alerts, store, horizons=(7, 20), split=days[50])
    assert {item["board_code"] for item in both["boards"]} == \
        {"886042.TI", "885908.TI"}
    assert both["stats"]["pool_excluded_boards"] == 0

    cut = build(alerts, store, horizons=(7, 20), split=days[50],
                excluded_boards={"886042.TI"})
    assert {item["board_code"] for item in cut["boards"]} == {"885908.TI"}
    assert {row["board_code"] for row in cut["rows"]} == {"885908.TI"}
    assert cut["stats"]["pool_excluded_boards"] == 1
    assert cut["stats"]["pool_excluded_rows"] == 1
    # 剔了多少必须写在正文里 —— 不写就会变成"数据怎么少了"的报障
    assert "池级剔除清单" in cut["note"]


def test_good_board_passes() -> None:
    days = calendar_of(80)
    bars = [bar(days[0], high=100.0, low=100.0, close=100.0)]
    for offset in range(1, 21):
        bars.append(bar(days[offset], high=120.0, low=99.0,
                        close=118.0 if offset == 20 else 110.0))
    store = FakeStore(days, {"886042.TI": bars})
    rows, _ = build_rows([alert(days[0])], store, calendar=days, horizons=(7, 20))
    boards = summarize_boards(rows)
    assert boards[0]["passed"] is True
    assert boards[0]["gate"] == "pass"
    assert boards[0]["quality"] == "good"
    assert boards[0]["win_rate_20d"] == 1.0


def test_summary_keeps_board_with_only_partial_windows() -> None:
    """全是未走满窗口的板块不能因为"没有 20 日最终值"被整体剔除。"""
    days = calendar_of(40)
    bars = [bar(days[0], high=100.0, low=100.0, close=100.0)]
    for offset in range(1, 4):
        bars.append(bar(days[offset], high=112.0, low=101.0, close=111.0))
    store = FakeStore(days, {"886042.TI": bars})
    rows, _ = build_rows([alert(days[0])], store, calendar=days, horizons=(7, 20))
    boards = summarize_boards(rows)
    assert boards[0]["avg_ret_20d"] is None
    assert boards[0]["avg_ret_20d_now"] == 11.0
    assert boards[0]["basis"] == "ret_20d_now"
    assert boards[0]["gate"] == "pending"
    assert boards[0]["passed"] is True


# ==================================================================
# build()：整包契约
# ==================================================================


def test_build_payload_shape_and_split() -> None:
    days = calendar_of(100)
    bars = [bar(day, high=101.0 + index, low=100.0, close=100.0 + index)
            for index, day in enumerate(days)]
    store = FakeStore(days, {"886042.TI": bars}, members={"886042.TI": ["000001"]})
    alerts = [alert(days[0], score=85.0), alert(days[3], score=88.0),
              alert(days[60], score=70.0, level="medium")]

    payload = build(alerts, store, horizons=(7, 20, 60), split=days[50])
    assert payload["as_of"] == days[-1]
    assert payload["horizons"] == [7, 20, 60]
    assert payload["dedup_days"] == 10
    assert payload["stats"]["signals"] == 2          # 前两条折成 1 个周期
    assert payload["stats"]["alert_total"] == 3
    assert payload["stats"]["folded"] == 1
    assert payload["stats"]["history"] == 1
    assert payload["stats"]["recent"] == 1
    # 折叠取周期内最高分（85 / 88 → 88）
    top = next(row for row in payload["rows"] if row["alert_count"] == 2)
    assert top["score"] == 88.0
    assert top["alert_dates"] == [days[0], days[3]]
    # 每个周期三个窗口都在
    for row in payload["rows"]:
        assert set(row["windows"]) == {"7", "20", "60"}
    # 没请求领涨股时要明说原因，不能静默为空
    assert any("leaders=true" in gap for gap in payload["gaps"])
    assert payload["note"]


def test_build_reports_warehouse_gap_when_leaders_requested() -> None:
    days = calendar_of(40)
    bars = [bar(day, high=101.0, low=100.0, close=100.5) for day in days]
    store = FakeStore(days, {"886042.TI": bars}, members={"886042.TI": ["000001"]})
    payload = build([alert(days[0])], store, horizons=(7,), leaders=True)
    assert any("行情仓不可用" in gap for gap in payload["gaps"])


def test_build_handles_empty_alerts() -> None:
    days = calendar_of(10)
    store = FakeStore(days, {})
    payload = build([], store, horizons=(7,))
    assert payload["rows"] == []
    assert payload["stats"]["signals"] == 0
    assert payload["gaps"]


def test_default_horizons_are_seven_twenty_sixty() -> None:
    """任务书口径：7 / 20 / 60 个交易日。改这里等于改表头含义，必须显式。"""
    assert DEFAULT_HORIZONS == (7, 20, 60)
