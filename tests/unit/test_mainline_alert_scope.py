"""告警范围限制（`mainline_alert_exclusions.yaml`）的单元测试。

## 为什么这批测试重要

这份名单直接决定"某类题材还发不发告警"。三种模式的行为差异很小
（`block` vs `strong_only` 只差"strong 放不放行"），但后果完全不同：
写错了要么把真行情一起关掉、要么等于没关。

而且它有两个**静默失效**的风险：

1. 名单文件读不到时若悄悄降级成"没有限制"，用户以为关掉的板块会重新
   开始告警，而界面上看不出任何异常；
2. 窗口的**左侧冗余**写错方向（例如加在右侧、或者按月份而不是按窗口起点）
   会静默制造漏报 —— 告警本来就常提前一两周报。

所以前两组测试专门钉这两点。
"""

from __future__ import annotations

from pathlib import Path

from src.mainline.config import (
    AlertScopeConfig,
    _load_alert_scope,
    load_config,
)

# ==================================================================
# 一、判定逻辑
# ==================================================================


def test_excluded_board_is_blocked_every_day() -> None:
    scope = AlertScopeConfig(excluded={"885312.TI": "测试用"})
    for day in ("20260115", "20260615", "20261231"):
        mode, why = scope.restriction("885312.TI", day)
        assert mode == "block" and "测试用" in why


# ==================================================================
# 一之二、`permits()` —— 范围闸门的**唯一**判据
# ==================================================================


def test_permits_covers_all_four_modes() -> None:
    """四种模式 × 三个档位，逐格钉死。

    ⚠️ 这个函数存在的理由是**避免静默不一致**：模式字符串的比对原来散在
    5 处（`service._decide_level` / `apply_alert_scope` / `alert_scope_effect`
    / `board_false_positive_report`），加一个模式要改 5 个地方，
    漏一处就是"重打分与后处理给出不同的表、且不报错"。
    """
    scope = AlertScopeConfig(
        excluded={"X": "关闭"},
        seasonal={"S": (("02-06", "04-15"),)},
        strong_only={"K": (("02-06", "04-15"),)},
        medium_up={"M": "只留中强"},
        lead_days=14)
    inside, outside = "20260301", "20260701"
    # 完全关闭：三个档位全挡，任何一天
    assert not any(scope.permits("X", day, lv)
                   for day in (inside, outside)
                   for lv in ("strong", "medium", "weak"))
    # 季节性：窗口内全放行，窗口外全挡
    assert all(scope.permits("S", inside, lv) for lv in
               ("strong", "medium", "weak"))
    assert not any(scope.permits("S", outside, lv) for lv in
                   ("strong", "medium", "weak"))
    # strong_only：窗口内全放行，窗口外只放 strong
    assert all(scope.permits("K", inside, lv) for lv in
               ("strong", "medium", "weak"))
    assert scope.permits("K", outside, "strong")
    assert not scope.permits("K", outside, "medium")
    assert not scope.permits("K", outside, "weak")
    # medium_up：**全年**只挡 weak（与 strong_only 的关键差别）
    for day in (inside, outside):
        assert scope.permits("M", day, "strong")
        assert scope.permits("M", day, "medium")
        assert not scope.permits("M", day, "weak")
    # 不在名单上：全放行
    assert all(scope.permits("OTHER", day, lv) for day in (inside, outside)
               for lv in ("strong", "medium", "weak"))


def test_medium_up_differs_from_strong_only_on_medium() -> None:
    """`medium_up` 与 `strong_only` 只差 medium 一档 —— 但差 9~16 条告警。

    用户对猪肉/中船系的指示是「只留中强信号」。按档位名（强/中/弱）取
    字面读法 = 中 + 强，所以要的是 `medium_up` 而不是 `strong_only`。
    这条测试就是防"顺手复用 strong_only"。
    """
    strong_only = AlertScopeConfig(strong_only={"C": (("01-01", "01-02"),)},
                                   lead_days=0)
    medium_up = AlertScopeConfig(medium_up={"C": "只留中强"})
    day = "20260701"
    assert strong_only.restriction("C", day)[0] == "strong_only"
    assert medium_up.restriction("C", day)[0] == "medium_up"
    assert not strong_only.permits("C", day, "medium")
    assert medium_up.permits("C", day, "medium")


def test_permits_blocks_unknown_level() -> None:
    """没识别的档位一律**不通过** —— 宁可少报一条，也不让脏档位绕过闸门。"""
    scope = AlertScopeConfig(medium_up={"C": "只留中强"})
    assert not scope.permits("C", "20260701", "none")
    assert not scope.permits("C", "20260701", "")
    assert not scope.permits("C", "20260701", "STRONG")   # 大小写敏感


def test_medium_up_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(
        "medium_up:\n"
        "  - code: \"885573.TI\"\n"
        "    name: \"猪肉\"\n"
        "    reason: \"只留中强\"\n",
        encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert scope.medium_up == {"885573.TI": "只留中强"}
    assert "1 个全年只留中强信号" in scope.load_note
    assert scope.permits("885573.TI", "20260101", "medium")
    assert not scope.permits("885573.TI", "20260101", "weak")


def test_window_blocks_outside_the_range() -> None:
    scope = AlertScopeConfig(seasonal={"B": (("02-06", "04-15"),)},
                             lead_days=14)
    assert scope.restriction("B", "20260301")[0] == ""            # 窗口内
    assert scope.restriction("B", "20260415")[0] == ""            # 窗口止（含）
    assert scope.restriction("B", "20260620")[0] == "block"       # 远离窗口


def test_left_lead_is_measured_from_the_window_start() -> None:
    """**用户给的验收例子**：窗口 2.6–4.15，冗余 14 天 → **1.23** 起放行。

    用户原话：「窗口日期往前两周，比如历史上高度炒作时间是 2.6-4.15，
    那窗口就是 2.6 − 14 个自然日，也就是 1.23 就要放开告警限制。」

    ⚠️ 第一版把冗余加在"允许月份的月初"上（月份粒度），2 月 1 日就放行了
    —— 与"窗口起点前推两周"不是一回事，这条测试就是钉死这个语义。
    """
    scope = AlertScopeConfig(seasonal={"B": (("02-06", "04-15"),)},
                             lead_days=14)
    assert scope.restriction("B", "20260123")[0] == ""            # 起点 − 14 天
    assert scope.restriction("B", "20260122")[0] == "block"       # 差一天就不行
    assert scope.restriction("B", "20260115")[0] == "block"
    # 右侧**不**放宽：窗口止于 04-15，04-16 就不放行
    assert scope.restriction("B", "20260416")[0] == "block"


def test_lead_can_be_disabled() -> None:
    """`lead_days=0` 回到"严格按窗口"的行为（对照用）。"""
    scope = AlertScopeConfig(seasonal={"B": (("02-06", "04-15"),)},
                             lead_days=0)
    assert scope.restriction("B", "20260123")[0] == "block"
    assert scope.restriction("B", "20260206")[0] == ""


def test_window_can_cross_the_year_boundary() -> None:
    """跨年窗口（12-20 ~ 01-15）必须能用 —— 年底年初的题材不少。"""
    scope = AlertScopeConfig(seasonal={"B": (("12-20", "01-15"),)},
                             lead_days=14)
    assert scope.restriction("B", "20251220")[0] == ""
    assert scope.restriction("B", "20260115")[0] == ""
    assert scope.restriction("B", "20260116")[0] == "block"
    # 12-20 往前 14 天 = **12-06**，边界当天算放行（闭区间）
    assert scope.restriction("B", "20251206")[0] == ""
    assert scope.restriction("B", "20251205")[0] == "block"


def test_strong_only_marks_instead_of_blocking() -> None:
    """`strong_only` 必须返回 `strong_only` 而不是 `block` ——
    调用方据此决定"只放 strong"，返回 block 会把强信号也一起关掉。"""
    scope = AlertScopeConfig(strong_only={"885914.TI": (("03-09", "03-26"),)},
                             lead_days=14)
    assert scope.restriction("885914.TI", "20260312")[0] == ""
    assert scope.restriction("885914.TI", "20260223")[0] == ""      # 起点 − 14
    assert scope.restriction("885914.TI", "20260222")[0] == "strong_only"
    mode, why = scope.restriction("885914.TI", "20260612")
    assert mode == "strong_only" and "只放行 strong" in why
    # `blocked_reason` 是"是否被完全拦下"的旧入口，strong_only 不算完全拦下
    assert scope.blocked_reason("885914.TI", "20260612") == ""


def test_strong_only_uses_the_same_window_function() -> None:
    """两套季节性规则必须共用窗口函数 —— 只给 `seasonal` 加冗余、
    忘了 `strong_only`，会让煤炭在旺季前两周被降级，等于白留冗余。"""
    scope = AlertScopeConfig(strong_only={"C": (("09-01", "11-17"),)},
                             lead_days=14)
    assert scope.restriction("C", "20260818")[0] == ""            # 09-01 − 14
    assert scope.restriction("C", "20260817")[0] == "strong_only"


def test_unlisted_board_is_unrestricted() -> None:
    scope = AlertScopeConfig(excluded={"A": "x"},
                             seasonal={"B": (("01-01", "01-31"),)},
                             strong_only={"C": (("01-01", "01-31"),)})
    assert scope.restriction("885530.TI", "20260612") == ("", "")


def test_priority_is_excluded_then_seasonal_then_strong_only() -> None:
    """三个名单理论上互斥；同时命中时要按写死的顺序，不能不确定。"""
    span = (("01-01", "12-31"),)
    scope = AlertScopeConfig(excluded={"X": "关掉"}, seasonal={"X": span},
                             strong_only={"X": span})
    assert scope.restriction("X", "20260612")[0] == "block"
    assert "关掉" in scope.restriction("X", "20260612")[1]


def test_bad_trade_date_does_not_crash() -> None:
    """日期格式异常时判为"不在窗口内" —— 落到安全方向（不发）。"""
    scope = AlertScopeConfig(seasonal={"B": (("01-01", "01-31"),)})
    assert scope.restriction("B", "")[0] == "block"
    assert scope.restriction("B", "2026")[0] == "block"
    assert scope.restriction("B", "abcdefgh")[0] == "block"


# ==================================================================
# 二、从 YAML 读取
# ==================================================================


def test_missing_file_is_reported_not_silent(tmp_path: Path) -> None:
    """名单文件不存在时必须留下说明，**不能**静默变成"没有限制"。"""
    scope = _load_alert_scope(str(tmp_path / "nope.yaml"))
    assert scope.excluded == {} and scope.seasonal == {}
    assert "不存在" in scope.load_note


def test_yaml_parses_all_three_sections(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(
        "version: 1\n"
        "lead_days: 14\n"
        "excluded:\n"
        "  - {code: \"A.TI\", name: \"甲\", reason: \"r\", decided: \"2026-09-22\"}\n"
        "seasonal:\n"
        "  - code: \"B.TI\"\n"
        "    windows:\n"
        "      - {start: \"02-06\", end: \"04-15\"}\n"
        "strong_only:\n"
        "  - code: \"C.TI\"\n"
        "    windows:\n"
        "      - {start: \"03-09\", end: \"03-26\"}\n"
        "      - {start: \"09-30\", end: \"11-17\"}\n",
        encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert scope.excluded["A.TI"] == "r"
    assert scope.seasonal["B.TI"] == (("02-06", "04-15"),)
    assert scope.strong_only["C.TI"] == (("03-09", "03-26"), ("09-30", "11-17"))
    assert scope.lead_days == 14
    assert "1 个板块完全关闭" in scope.load_note


def test_months_shorthand_expands_to_full_month_windows(tmp_path: Path) -> None:
    """便捷写法 `months: [2, 11]` 展开成整月窗口（含月末天数正确）。"""
    path = tmp_path / "scope.yaml"
    path.write_text("seasonal:\n  - {code: \"B.TI\", months: [2, 11]}\n",
                    encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert scope.seasonal["B.TI"] == (("02-01", "02-28"), ("11-01", "11-30"))


def test_invalid_windows_are_dropped(tmp_path: Path) -> None:
    """格式不对的窗口不能变成"永远放行"或"永远拦下"这种意外行为。"""
    path = tmp_path / "scope.yaml"
    path.write_text(
        "seasonal:\n"
        "  - code: \"B.TI\"\n"
        "    windows:\n"
        "      - {start: \"2026-02-06\", end: \"2026-04-15\"}\n"
        "  - {code: \"C.TI\", months: [0, 13, \"x\"]}\n",
        encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert "B.TI" not in scope.seasonal          # 长度不对 → 丢弃
    assert "C.TI" not in scope.seasonal          # 月份全非法 → 不登记


def test_lead_days_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text("lead_days: 21\n", encoding="utf-8")
    assert _load_alert_scope(str(path)).lead_days == 21


def test_shipped_config_loads_and_covers_the_four_boards() -> None:
    """线上那份配置必须真的被解析出来（否则等于没生效）。

    四个板块按各自被指定的方式配置：旅游用**用户给的月份**（展开成整月窗口），
    煤炭/绿色电力用**数据推导的月-日窗口**。这条测试同时钉住这个分工 ——
    谁被改成什么口径，一看就知道。
    """
    config = load_config(force=True)
    scope = config.alert_scope
    assert scope.lead_days == 14
    assert set(scope.excluded) >= {"885312.TI", "885428.TI", "885976.TI"}
    # 旅游：`months` 便捷写法展开成整月窗口。
    # 用户 2026-09-22 定稿：**关掉 1 月、把 2 月列进来**（与数据一致：
    # 1 月是本样本最差的一个月、2 月是最好的一个）。
    assert scope.seasonal.get("885497.TI") == (
        ("02-01", "02-28"), ("03-01", "03-31"), ("04-01", "04-30"),
        ("08-01", "08-31"), ("09-01", "09-30"))
    # 煤炭 / 绿色电力：数据推导的窗口
    # 煤炭额外并入了**用户给的两段实盘窗口**（2026-01-05~03-12、
    # 2026-07-01~08-31）—— 数据推导不能当全集，1/2 月与 7/8 月靠经验补。
    assert scope.strong_only.get("885914.TI") == (
        ("01-05", "03-26"), ("07-01", "08-31"), ("09-30", "11-17"))
    assert scope.strong_only.get("885936.TI") == (
        ("03-12", "03-28"), ("09-30", "11-14"))


def test_shipped_config_blocks_the_second_batch_of_four() -> None:
    """第二批 4 个（误报榜前 10 里用户点名的）必须被整块关闭。

    ⚠️ 这条测试要区分**两个不同的开关**，否则很容易改错地方：
    * 本文件（告警级）→ 板块照常打分、照常进候选池，只是不再产生告警；
    * `configs/sector_blacklist.yaml`（池级）→ 板块**根本不进池**、数据也不同步。

    用户要的是**前者**（这个取舍写在配置头部），另外再加「不进拥挤度看板」，
    所以这里只钉告警级；池级那条**故意不钉** —— 将来真要整块删掉时，
    改的应该是黑名单而不是这里。
    """
    config = load_config(force=True)
    scope = config.alert_scope
    second_batch = {"885767.TI", "885761.TI", "886003.TI", "885748.TI"}
    assert second_batch <= set(scope.excluded)
    # 关闭 = 任何一天、任何档位都是 `block`（而不是 `strong_only` 的降级）。
    for code in sorted(second_batch):
        for day in ("20231103", "20250610", "20260918"):
            mode, why = scope.restriction(code, day)
            assert mode == "block", f"{code} {day} 应为 block，实际 {mode}"
            assert why, "block 必须带原因，否则排障时看不出为什么被拦"
    # 不在名单上的板块仍然不受限（防止把整份名单误写成通配）。
    # 885418 文化传媒概念是某个真值事件的首报来源，特意拿它做反例。
    assert scope.restriction("885418.TI", "20231103")[0] == ""


def test_shipped_config_blocks_the_third_batch_of_seven() -> None:
    """第三批 7 个（用户按**深亏率**点名的）必须被整块关闭。

    用户原话：「删除土地流转 44%、光伏概念 36%、BC电池、动物疫苗、
    工业大麻、网络游戏、国产航母概念。」

    ⚠️ 这批的判据是**深亏率**（20 日收益 < −10% 的占比），不是误报率 ——
    两者的名单不同：土地流转误报率只有 78%（榜上第 13），
    是 44% 的深亏率把它排到第一。所以这条测试同时钉住
    "深亏率判据不会被后来的人按误报率重排掉"。
    """
    config = load_config(force=True)
    scope = config.alert_scope
    third_batch = {"885439.TI", "885531.TI", "886053.TI", "885846.TI",
                   "885818.TI", "885603.TI", "885795.TI"}
    assert third_batch <= set(scope.excluded)
    for code in sorted(third_batch):
        assert not scope.permits(code, "20250610", "strong")
        assert not scope.permits(code, "20250610", "weak")


def test_shipped_config_keeps_pork_and_shipbuilding_on_medium_up() -> None:
    """猪肉 885573 / 中船系 885860：**只留中强**（用户指示）。

    这两个是用户**特意保下来**的（大周期板块、深亏率只有 6% / 2%），
    所以既不能出现在 `excluded` 里，也不能误用 `strong_only`（那会把
    medium 一起挡掉）。猪肉 52 条告警里 strong_only 留 17 条、
    medium_up 留 26 条 —— 差 9 条，是可观察的差别。
    """
    config = load_config(force=True)
    scope = config.alert_scope
    for code in ("885573.TI", "885860.TI"):
        assert code in scope.medium_up, f"{code} 应在 medium_up 名单里"
        assert code not in scope.excluded
        assert code not in scope.strong_only
        for day in ("20231101", "20250610", "20260918"):
            assert scope.permits(code, day, "strong")
            assert scope.permits(code, day, "medium")
            assert not scope.permits(code, day, "weak")



def test_min_score_raises_the_bar_for_one_board_only() -> None:
    """`min_score` 只影响配置里的那个板块，不动其余板块的政策。

    这是它存在的理由：全局抬阈值每少 5~8pp 误报要付出 7~27pp 真值覆盖
    （已量过并否掉），而误报集中在少数板块上 —— 按板块抬才划算。
    """
    scope = AlertScopeConfig(min_score={"885808.TI": (78.0, "养鸡误报多")})
    # 该板块：低于 78 一律不放行
    assert not scope.permits("885808.TI", "20250610", "strong", 77.9)
    assert scope.permits("885808.TI", "20250610", "strong", 78.0)
    assert scope.permits("885808.TI", "20250610", "weak", 90.0)
    # 别的板块完全不受影响（同一天、同一分数、同一档位）
    assert scope.permits("885903.TI", "20250610", "weak", 50.0)
    assert scope.restriction("885903.TI", "20250610")[0] == ""


def test_min_score_fails_closed_when_score_is_missing() -> None:
    """配了 `min_score` 但调用方没传分数 → **不放行**（并记 warning）。

    为什么不是"放行"：静默放行会让"按板块抬阈值"看起来生效其实没生效，
    而静默拦截至少方向保守、且能从日志里发现。这与"未知档位一律不通过"
    是同一条原则。
    """
    scope = AlertScopeConfig(min_score={"X.TI": (80.0, "测试")})
    assert not scope.permits("X.TI", "20250610", "strong")          # 没传 score
    assert not scope.permits("X.TI", "20250610", "strong", None)
    # 没配 min_score 的板块，不传分数也照常放行（向后兼容）
    assert scope.permits("Y.TI", "20250610", "weak")


def test_min_score_composes_with_other_modes() -> None:
    """`min_score` 与档位限制是**与**关系，不是互相覆盖。"""
    scope = AlertScopeConfig(medium_up={"M.TI": "只留中强"},
                             min_score={"M.TI": (85.0, "再抬分数")})
    assert not scope.permits("M.TI", "20250610", "weak", 99.0)      # 档位挡掉
    assert not scope.permits("M.TI", "20250610", "strong", 84.9)    # 分数挡掉
    assert scope.permits("M.TI", "20250610", "strong", 85.0)


def test_min_score_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(
        "min_score:\n"
        "  - code: \"885808.TI\"\n"
        "    name: \"养鸡\"\n"
        "    score: 78\n"
        "    reason: \"误报集中在低分档\"\n",
        encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert scope.min_score["885808.TI"][0] == 78.0
    assert "1 个抬高分数线" in scope.load_note
    assert not scope.permits("885808.TI", "20260101", "strong", 77.0)


def test_min_score_with_bad_value_is_skipped_not_crashing(tmp_path: Path) -> None:
    """`score` 写成非数字时跳过该条并告警，而不是让整份名单失效。"""
    path = tmp_path / "scope.yaml"
    path.write_text(
        "min_score:\n"
        "  - code: \"A.TI\"\n"
        "    score: \"不是数字\"\n"
        "  - code: \"B.TI\"\n"
        "    score: 75\n",
        encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert "A.TI" not in scope.min_score
    assert scope.min_score["B.TI"][0] == 75.0


def test_max_score_caps_the_high_band() -> None:
    """`max_score` 给分数**封顶**：高于上限的一律不放行。

    用户的假设是「高分档 = 概念大涨一波的顶部」→ 阻止高位报主线。
    实测**只在 20% 的板块上成立**（全池最高分档的收益其实最好），
    所以这个开关只能按板块、凭该板块自己的分档收益来配。
    """
    scope = AlertScopeConfig(max_score={"885343.TI": (82.0, "高分档收益为负")})
    assert scope.permits("885343.TI", "20250610", "strong", 81.9)
    assert scope.permits("885343.TI", "20250610", "strong", 82.0)   # 边界含
    assert not scope.permits("885343.TI", "20250610", "strong", 82.1)
    assert not scope.permits("885343.TI", "20250610", "weak", 99.0)
    # 别的板块不受影响
    assert scope.permits("885808.TI", "20250610", "strong", 99.0)


def test_min_and_max_score_form_a_band() -> None:
    """上下限同时配 = 只放行一个分数带（两个都是**与**关系）。"""
    scope = AlertScopeConfig(min_score={"X.TI": (75.0, "低分档最差")},
                             max_score={"X.TI": (88.0, "高分档是顶")})
    assert not scope.permits("X.TI", "20250610", "strong", 74.9)
    assert scope.permits("X.TI", "20250610", "strong", 75.0)
    assert scope.permits("X.TI", "20250610", "strong", 88.0)
    assert not scope.permits("X.TI", "20250610", "strong", 88.1)


def test_max_score_fails_closed_without_score() -> None:
    scope = AlertScopeConfig(max_score={"X.TI": (80.0, "测试")})
    assert not scope.permits("X.TI", "20250610", "strong")
    assert scope.permits("Y.TI", "20250610", "strong")


def test_max_score_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(
        "max_score:\n"
        "  - code: \"885343.TI\"\n"
        "    name: \"稀土永磁\"\n"
        "    score: 82\n"
        "    reason: \"高分档 14 条均值 -1.09%，低分档 +1.54%\"\n",
        encoding="utf-8")
    scope = _load_alert_scope(str(path))
    assert scope.max_score["885343.TI"][0] == 82.0
    assert "1 个封顶分数线" in scope.load_note
    assert not scope.permits("885343.TI", "20260101", "strong", 85.0)
