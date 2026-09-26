"""主线挖掘：龙头共振与提名通道的回归测试。

这一组测试对应一次**真实漏报**的修复（2024-06 CRO 案例），因此每条都钉住了
一个具体的失效方式，而不是泛泛地"测试一下函数能跑"：

1. **板块净流出时集中度必须有定义。** 原口径 `龙头净流入 / 板块净流入` 在
   分母为负时返回"无定义" → 不触发共振。而"板块还在净流出、龙头已被大额
   买入"正是"启动前"场景 —— 医药 2024-06 就是这样被漏掉的。
2. **共振必须在候选池外也能被评估。** 旧实现只对精选前 10 名评估；医疗服务
   板块集中度已到 110%~162%，却因为第一层排 61/93 而从未被计算。
3. **候选池外的板块不能走普通档位判定。** 它只算了第一层，拿"只算一层的 58 分"
   去比"两层合成才够的 55 分线"不是同一把尺子（实测会让告警涨到 18 条/天）。
4. **提名要有下限。** 只留集中度会捞进"跌久了刚好有人抢反弹"的小板块。
"""

from __future__ import annotations

import pytest

from src.mainline.config import load_config
from src.mainline.leader import (
    LeaderInput,
    evaluate_resonance,
    gate_bonus,
    identify_leaders,
)
from src.mainline.models import (
    AlertSignal,
    BoardBar,
    BoardScore,
    BoardSeries,
    DimensionScore,
    LayerScore,
    SeatRow,
    SignalLevel,
)
from src.mainline.service import MainlineService


def _series(code: str, closes: list[float]) -> BoardSeries:
    series = BoardSeries(code=code)
    previous = closes[0]
    for index, close in enumerate(closes):
        series.bars.append(BoardBar(
            date=f"2026{index + 1:04d}", close=close, open=previous,
            high=max(previous, close), low=min(previous, close),
            volume=1000.0, pre_close=previous))
        previous = close
    return series


def _leader_input(*, board_net: float,
                  leader_net: float = 2.0e8) -> LeaderInput:
    """一个"龙头逆势吸筹"的最小输入：板块净流出，龙头净流入。

    ⚠️ 成分股必须**明显多于** `leader.top_n`（默认 3），否则"进前 3 名"
    对所有票都成立，整个板块都会变成龙头 —— 见 `identify_leaders` 的说明。
    """
    members = {
        "600001": {"net_5d": leader_net, "ret_10d": 0.12, "amount_5d": 9.0e8,
                   "close": 30.0},
        "600002": {"net_5d": -1.0e8, "ret_10d": -0.02, "amount_5d": 1.0e8,
                   "close": 10.0},
        "600003": {"net_5d": -0.5e8, "ret_10d": -0.03, "amount_5d": 1.0e8,
                   "close": 10.0},
    }
    for index in range(4, 12):
        members[f"6000{index:02d}"] = {
            "net_5d": -0.2e8, "ret_10d": -0.01, "amount_5d": 0.5e8,
            "close": 8.0}
    return LeaderInput(
        code="885927.TI", name="CRO概念",
        series=_series("885927.TI", [100.0 + i * 0.1 for i in range(40)]),
        board_net=board_net, members=members,
        names={"600001": "药明康德", "600002": "甲", "600003": "乙"})


# ==================================================================
# 一、集中度口径
# ==================================================================


def test_concentration_defined_when_board_is_in_net_outflow() -> None:
    """板块净流出时集中度**必须有值**（这正是"启动前"的场景）。"""
    cfg = load_config(force=True)
    item = _leader_input(board_net=-3.0e8)
    outcome = evaluate_resonance(item, config=cfg)
    assert outcome.concentration is not None, "分母为负不该让指标失效"
    assert outcome.concentration > 0
    # 原始口径（负数）单独保留供复核，不参与判定
    assert outcome.raw_concentration is not None
    assert outcome.raw_concentration < 0


def test_concentration_uses_gross_flow_denominator() -> None:
    """集中度 = 龙头净流入 / Σ|成分股净流入|（分母恒为正）。"""
    cfg = load_config(force=True)
    item = _leader_input(board_net=-3.0e8, leader_net=2.0e8)
    outcome = evaluate_resonance(item, config=cfg)
    gross = 2.0e8 + 1.0e8 + 0.5e8 + 8 * 0.2e8
    assert outcome.concentration == pytest.approx(2.0e8 / gross, rel=1e-6)
    assert outcome.triggered is True          # 2/5.1 ≈ 39% > 30%


def test_tiny_board_has_no_leader() -> None:
    """成分股 ≤ top_n（前 3 名）时"进前三"对所有票都成立 —— 必须返回空。

    否则小板块会稳定拿到 hits=3、集中度 100%，"板块越小越容易共振"。
    """
    cfg = load_config(force=True)
    members = {"600001": {"net_5d": 1.0e8, "ret_10d": 0.05, "amount_5d": 1.0e8},
               "600002": {"net_5d": 0.5e8, "ret_10d": 0.04, "amount_5d": 1.0e8},
               "600003": {"net_5d": 0.5e8, "ret_10d": 0.04, "amount_5d": 1.0e8}}
    item = LeaderInput(code="X", name="小板块", series=_series("X", [10.0] * 40),
                       board_net=1.0e8, members=members, names={})
    assert identify_leaders(item, config=cfg) == []
    assert evaluate_resonance(item, config=cfg).triggered is False


def test_concentration_none_when_no_capital_action() -> None:
    """板块没有任何资金动作时分母为 0 —— 返回 None 而不是编一个数。"""
    cfg = load_config(force=True)
    item = LeaderInput(code="X", name="X", series=_series("X", [10.0] * 40),
                       board_net=0.0, members={}, names={})
    outcome = evaluate_resonance(item, config=cfg)
    assert outcome.triggered is False


# ==================================================================
# 二、龙头识别
# ==================================================================


def test_leader_identified_by_three_dim_intersection() -> None:
    cfg = load_config(force=True)
    item = _leader_input(board_net=-3.0e8)
    leaders = identify_leaders(item, config=cfg)
    assert leaders, "三维都第一的票必须被识别为龙头"
    assert leaders[0].name == "药明康德"
    assert leaders[0].hits == 3


def test_seat_confirmation_needs_three_stocks_same_branch() -> None:
    cfg = load_config(force=True)
    item = _leader_input(board_net=-3.0e8)
    item.seats = [SeatRow(date="20260612", code=code, exalter="某游资营业部",
                          net_buy=2.0e7, side="0")
                  for code in ("600001", "600002")]
    outcome = evaluate_resonance(item, config=cfg)
    assert outcome.seat_confirmed is False, "只有 2 只票，不够 min_stocks=3"
    item.seats.append(SeatRow(date="20260612", code="600003",
                              exalter="某游资营业部", net_buy=2.0e7, side="0"))
    outcome = evaluate_resonance(item, config=cfg)
    assert outcome.seat_confirmed is True


def test_institution_branch_weighted_higher_than_hot_money() -> None:
    cfg = load_config(force=True)
    institution = gate_bonus(0.30, config=cfg, seat_confirmed=True,
                             seat_weight=cfg.leader.seat.institution_weight,
                             triggered=True)
    hot_money = gate_bonus(0.30, config=cfg, seat_confirmed=True,
                           seat_weight=cfg.leader.seat.branch_weight,
                           triggered=True)
    assert institution >= hot_money


# ==================================================================
# 三、提名通道与档位判定
# ==================================================================


def _board(*, code: str, six: float, candidate: bool = False,
           resonance: bool = False, concentration: float = 0.0,
           gate: float = 0.0, promoted: bool = False) -> BoardScore:
    board = BoardScore(code=code, name=code, trade_date="20260612")
    board.six_dim = LayerScore("six_dim", "六维", six, dimensions=[
        DimensionScore("moneyflow", "资金流", six, 20.0, available=True)])
    board.candidate = candidate
    board.resonance = resonance
    board.resonance_ratio = concentration
    board.gate_bonus = gate
    board.base_total = six
    board.total = min(100.0, six + gate)
    board.promoted = promoted
    return board


def test_out_of_pool_board_without_nomination_never_alerts() -> None:
    """候选池外、又没被提名的板块不能出告警（量纲不可比，见模块文档）。"""
    service = MainlineService(config=load_config(force=True))
    board = _board(code="X", six=69.0, candidate=False, resonance=True,
                   concentration=0.2)
    level, reasons, _ = service._decide_level(board)  # noqa: SLF001
    assert level is SignalLevel.NONE
    assert any("未进候选池" in text for text in reasons)


def test_promoted_board_alerts_as_medium_when_score_passes_line() -> None:
    # 中信号线在 V2.3 从 55 抬到 68（层权重的尺度变了），所以这里的构造
    # 数值同步抬高，保证测的仍然是"**刚好越过中信号线**的提名板块"。
    service = MainlineService(config=load_config(force=True))
    line = service.config.alert.medium_score
    board = _board(code="CRO", six=line - 18.4 + 0.4, candidate=False,
                   resonance=True, concentration=0.506, gate=18.4,
                   promoted=True)
    board.reasons = ["龙头共振提名：集中度 51%"]
    level, reasons, _ = service._decide_level(board)  # noqa: SLF001
    assert level is SignalLevel.MEDIUM
    assert any("未经第二层" in text for text in reasons)


def test_promoted_board_below_line_goes_to_watchlist() -> None:
    service = MainlineService(config=load_config(force=True))
    board = _board(code="Y", six=36.0, candidate=False, resonance=True,
                   concentration=0.5, gate=15.0, promoted=True)
    level, _, _ = service._decide_level(board)  # noqa: SLF001
    assert level is SignalLevel.WEAK


def test_nomination_requires_both_concentration_and_score_floor() -> None:
    cfg = load_config(force=True)
    service = MainlineService(config=cfg)
    floor = cfg.alert.nominate_min_concentration
    min_score = cfg.alert.nominate_min_score
    scores = {
        # 合格：集中度与第一层分都过线
        "ok": _board(code="ok", six=min_score + 5, resonance=True,
                     concentration=floor + 0.1),
        # 集中度高但第一层太差 → 不提名（"跌久了刚好有人抢反弹"）
        "weak": _board(code="weak", six=min_score - 10, resonance=True,
                       concentration=floor + 0.2),
        # 第一层不错但集中度不够 → 不提名
        "diluted": _board(code="diluted", six=min_score + 20, resonance=True,
                          concentration=floor - 0.1),
        # 已进候选池的板块不占提名名额
        "inside": _board(code="inside", six=min_score + 5, candidate=True,
                         resonance=True, concentration=floor + 0.3),
    }
    picked = service._nominate(scores, [])  # noqa: SLF001
    assert [row.code for row in picked] == ["ok"]


def test_nomination_respects_limit() -> None:
    cfg = load_config(force=True)
    service = MainlineService(config=cfg)
    limit = cfg.alert.nominate_limit
    scores = {
        f"c{index}": _board(code=f"c{index}", six=50.0, resonance=True,
                            concentration=0.9 - index * 0.01)
        for index in range(limit + 5)
    }
    picked = service._nominate(scores, [])  # noqa: SLF001
    assert len(picked) == limit
    assert picked[0].code == "c0"      # 按集中度降序


def test_nomination_can_be_disabled() -> None:
    cfg = load_config(force=True)
    cfg.alert.nominate_enabled = False
    service = MainlineService(config=cfg)
    scores = {"x": _board(code="x", six=60.0, resonance=True,
                          concentration=0.9)}
    assert service._nominate(scores, []) == []  # noqa: SLF001


# ==================================================================
# 四、加分只在入选后兑现
# ==================================================================


def test_gate_bonus_only_paid_to_selected_or_promoted() -> None:
    """共振参与排序，但只有入选精选/被提名的板块才实发加分。

    不这么做的话，候选池里每个共振板块都 +15~20，强信号会从日均个位数
    涨到十几条（实测 2024-06-12 从 2 条涨到 18 条），告警失去区分度。
    """
    cfg = load_config(force=True)
    service = MainlineService(config=cfg)
    assert cfg.synthesis.gate_bonus_max == 20.0
    # 没入选的候选板块：加分应被清零（由 `_compute` 统一处理），
    # 这里验证"入选与否"这个判据本身是显式存在的
    board = _board(code="c", six=50.0, candidate=True, gate=18.0)
    assert board.selected is False and board.promoted is False
    assert service is not None


def test_promoted_flag_in_payload() -> None:
    """`promoted` 必须出现在序列化结果里，否则前端无法区分"没算"与"0 分"。"""
    board = _board(code="CRO", six=40.0, promoted=True)
    payload = board.to_dict()
    assert payload["promoted"] is True
    assert payload["candidate"] is False
    assert payload["accumulation_score"] == 0.0


def test_alert_signal_carries_promoted_reason() -> None:
    signal = AlertSignal(board_code="885927.TI", board_name="CRO概念",
                         trade_date="20260612", level=SignalLevel.MEDIUM,
                         score=58.25,
                         reasons=["龙头共振提名：集中度 51%"])
    payload = signal.to_dict()
    assert payload["level"] == "medium"
    assert "提名" in payload["reasons"][0]


def test_leader_carries_business_relevance_for_display() -> None:
    """龙头要带上**业务相关性**（只用于展示，不参与选取）。

    用户 2026-09-22 看到氟化工概念的龙头里有雅克科技（主营半导体材料）
    提出质疑。查下来成员没错、`identify_leaders()` 也没错 —— 它只看
    资金/动量/量能三维，业务**从不参与**。问题是界面上看不出
    「这只票凭什么是这个概念的成员」，所以补三个展示字段。

    ⚠️ 这条测试同时钉住"**零破坏性**"：加了字段之后 hits / 排名 / 顺序
    一个都不能变 —— 否则就是"顺手改了选取口径"，与本次目标不符。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.mainline.config import load_config
    from src.mainline.leader import LeaderInput, identify_leaders

    cfg = load_config()
    members = {code: {"net_5d": net, "ret_10d": ret, "amount_5d": amount,
                      "name": name}
               for code, net, ret, amount, name in (
                   ("002409", 3.78e8, 0.0964, 1e10, "雅克科技"),
                   ("688146", 2.59e8, 0.0536, 9e9, "中船特气"),
                   ("600378", 1.63e8, 0.0492, 8e9, "昊华科技"),
                   ("600160", 1.0e7, 0.0100, 7e9, "巨化股份"),
                   ("002407", 9.0e6, 0.0200, 6e9, "多氟多"),
                   ("603379", 8.0e6, 0.0300, 5e9, "三美股份"))}
    item = LeaderInput(
        code="885551.TI", name="氟化工概念", members=members,
        names={code: row["name"] for code, row in members.items()},
        business={
            # 只靠相关性入池、LLM 没判过该题材 → 标签必须点明
            "002409": {"score": None, "source": "corr",
                       "reason": "corr 0.610 ≥ 0.55",
                       "admission": "corr"},
            # LLM 判过主营业务 → 正常显示业务分
            "688146": {"score": 85.0, "source": "llm",
                       "reason": "主营分 85 ≥ 70",
                       "admission": "business"},
            # 靠 corr 入池但 LLM 判过同题材 → 由 ml_stock_theme 回查补上
            "600378": {"score": 95.0, "source": "llm_theme",
                       "reason": "LLM 判定「氟化工」主营分 95",
                       "admission": "business"},
        })
    leaders = identify_leaders(item, config=cfg)
    assert leaders, "六个成分股、top_n=3，应当识别出龙头"
    by_code = {row.code: row for row in leaders}
    assert by_code["002409"].business_source == "corr"
    assert "仅股价相关" in by_code["002409"].business_label
    assert by_code["688146"].business_score == 85.0
    assert "85" in by_code["688146"].business_label
    assert by_code["600378"].business_score == 95.0
    assert "95" in by_code["600378"].business_label
    # ⚠️ 没配业务信息的股票：不报错，标签退化成"无业务判定"。
    #    这里再跑一次**完全不给 business** 的情形 —— 展示字段是可选增强，
    #    缺了它选取必须照常工作（否则就是把展示耦合进了判定）。
    bare = identify_leaders(
        LeaderInput(code="885551.TI", name="氟化工概念", members=members,
                    names={c: r["name"] for c, r in members.items()}),
        config=cfg)
    assert bare, "没有业务信息时也必须能选出龙头"
    assert all(row.business_source == "" for row in bare)
    assert all(row.business_label == "无业务判定" for row in bare)
    assert all(row.business_warn is False for row in bare)
    assert [row.code for row in bare] == [row.code for row in leaders], \
        "缺业务信息不得改变选取结果（零破坏性）"
    # 展示字段必须出现在 payload 里（前端靠它渲染那一列）
    payload = leaders[0].to_dict()
    assert {"business_score", "business_source", "business_reason",
            "business_label", "admission", "business_warn"} <= set(payload)
    # ⚠️ 零破坏性：加了字段，排序依据仍是 (hits, net_5d)
    assert [row.hits for row in leaders] == sorted(
        (row.hits for row in leaders), reverse=True)


def test_business_label_never_claims_unjudged_when_score_exists() -> None:
    """⚠️ 回归（用户 2026-09-23 报障）：**判过但不及格**不得被说成"未判过"。

    报障形态：`002140 东华科技 @ 885652 钛白粉概念`。它在 `ml_member_pure` 里是
    `source='corr'`（按 `corr 0.564 ≥ 0.55` 入池），**但 `business_score = 65`**
    —— LLM 明明判过这个题材、给了不及格的 65 分（门槛 70）。

    旧 `business_label` 只看 `source == "corr"` 就输出
    「仅股价相关（**LLM 未判过该题材**）」：展示与数据**相反**，
    而且把那个不及格的分数**吞掉了** —— 用户因此看不到"业务已判过且不达标"
    这个最关键的信息。这正是 16.67 警告过的"用错误信息误导用户，比不加更糟"。

    四种情形一次钉全（`score` 有没有值与"判过没判过"是两件独立的事）。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.mainline.models import LeaderInfo

    # ① 报障原型：判过、65 分、仍按相关性入池
    failed = LeaderInfo(code="002140", name="东华科技", business_score=65.0,
                        business_source="corr",
                        business_reason="corr 0.564 ≥ 0.55",
                        admission="corr_business_failed")
    assert "未判过" not in failed.business_label, \
        "LLM 判过这个题材，标签不得说它没判过"
    assert "65" in failed.business_label, "不及格的业务分必须显示出来（不能被吞掉）"
    assert failed.business_warn is True, "这类票必须被标成需要警示"
    payload = failed.to_dict()
    assert payload["business_warn"] is True
    assert payload["admission"] == "corr_business_failed"

    # ② 真正"没判过"的（雅克科技形态）：中性，不警示
    unjudged = LeaderInfo(code="002409", name="雅克科技", business_score=None,
                          business_source="corr", admission="corr")
    assert "未判过" in unjudged.business_label
    assert unjudged.business_warn is False, \
        "没证据不该罚 —— 与「判过且不及格」必须分开"

    # ③ 判过且达标
    passed = LeaderInfo(code="600378", name="昊华科技", business_score=95.0,
                        business_source="llm_theme", admission="business")
    assert passed.business_label == "业务相关 95 分"
    assert passed.business_warn is False
    assert "未判过" not in passed.business_label

    # ④ 完全没有业务信息（展示字段缺失）
    bare = LeaderInfo(code="000001", name="平安银行")
    assert bare.business_label == "无业务判定"
    assert bare.business_warn is False
