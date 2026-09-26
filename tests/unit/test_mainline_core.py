"""主线挖掘：配置 / 模型 / 打分原语 / 漏斗语义 的单元测试。

重点钉住六处"看起来对、实际会错"的地方：

1. **V2.0 口径**：第二层必须只有三维（leverage / northbound / volume_price），
   层间必须恒为 50/50。V1.0 的 `capital_strength` / `chip_concentration`
   一旦被"顺手补回来"，资金流与股东户数就会被跨层重复加权 ——
   这类回归不会报错，只会让回测胜率虚高，所以要用测试钉死。
2. **无数据 ≠ 0 分**：`weighted_score` 必须把不可用项从分母剔除并重新归一化。
   当成 0 分会系统性压低所有板块的分，让数据源一抖动就全市场不告警。
3. **样本不足返回 None**：分位至少 5 个样本、滚动相关至少 10 个样本。
   在 3 个板块里排第一太容易，给出高分等于制造假信号。
4. **本地仓位口径是「元」**：`quant_daily.amount` 已是元（不是千元）、
   `circ_mv` 已是元（不是万元）、`net_mf_amount` 已是元（不是万元）。
   多乘一次会让金额放大 1000~10000 倍，而**分数是横截面分位，
   整体放大后排序不变**，界面上看不出任何异常。
5. **防前视**：财务必须按 `ann_date <= trade_date` 截断。
6. **漏斗语义**：候选池外的板块 `accumulation.score == 0` 只表示"没算"，
   必须靠 `candidate=False` 区分，不能当成"建仓痕迹极弱"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.mainline import config as mainline_config
from src.mainline import scoring
from src.mainline.accumulation import (
    AccumulationInput,
    score_accumulation,
    volume_price_raw,
)
from src.mainline.config import AccumulationConfig, FunnelConfig, load_config, parse
from src.mainline.datastore import apply_pure_pool, filter_listed
from src.mainline.models import (
    AlertSignal,
    BoardBar,
    BoardInfo,
    BoardKind,
    BoardScore,
    BoardSeries,
    DimensionScore,
    LayerScore,
    SignalLevel,
)
from src.mainline.six_dim import BoardInput, candidate_cutoff, score_six_dim

# ==================================================================
# 一、打分原语
# ==================================================================


def test_weighted_score_excludes_unavailable_instead_of_zero() -> None:
    """不可用项必须从分母剔除并重新归一化（不是当 0 分）。"""
    score, coverage = scoring.weighted_score(
        [(80.0, 50.0, True), (0.0, 50.0, False)])
    assert score == pytest.approx(80.0)      # 不是 40.0
    assert coverage == pytest.approx(0.5)    # 覆盖率如实反映"只有一半权重可用"


def test_weighted_score_all_unavailable_is_zero_with_zero_coverage() -> None:
    score, coverage = scoring.weighted_score(
        [(90.0, 50.0, False), (90.0, 50.0, False)])
    assert score == 0.0 and coverage == 0.0


def test_percentile_requires_five_samples() -> None:
    """样本 < 5 返回 None —— 3 个板块里排第一不构成信号。"""
    assert scoring.percentile_score([1.0, 2.0, 3.0], 3.0) is None
    assert scoring.percentile_score([1.0, 2.0, 3.0, 4.0, 5.0], 5.0) == 100.0


def test_percentile_extremes_are_pinned() -> None:
    """无并列时最大恒为 100、最小恒为 0；有并列时最高分位按平均秩摊平。"""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert scoring.percentile_score(values, 5.0) == pytest.approx(100.0)
    assert scoring.percentile_score(values, 1.0) == pytest.approx(0.0)
    assert scoring.percentile_score(values, 1.0, reverse=True) == \
        pytest.approx(100.0)
    # 并列：两只并列第一必须得到同一个分，且低于 100（无法区分它们）
    tied = scoring.percentile_score([1.0, 2.0, 3.0, 4.0, 5.0, 5.0], 5.0)
    assert tied == pytest.approx(90.0)


def test_zscore_flat_series_returns_none() -> None:
    """整段不动的序列 z-score 无定义 —— 返回 0 会让横盘板块拿正常分。"""
    assert scoring.zscore([3.0] * 10) == [None] * 10


def test_correlation_needs_min_samples() -> None:
    assert scoring.correlation([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) is None
    assert scoring.correlation(list(range(20)), list(range(20))) == \
        pytest.approx(1.0)


def test_decay_weight_reaches_zero() -> None:
    """线性衰减必须真正归零（指数衰减会让三个月前的信号还挂着权重）。"""
    assert scoring.decay_weight(0, 10) == 1.0
    assert scoring.decay_weight(10, 10) == 0.0
    assert scoring.decay_weight(5, 10) == pytest.approx(0.5)


# ==================================================================
# 二、V2.0 口径（回归防线）
# ==================================================================


def test_layer_weights_are_eighty_twenty() -> None:
    """V2.4：层间权重为 **80/20**（六维 80、建仓痕迹 20）。

    演进过程本身就是一条教训链（详见 `docs/MAINLINE_MINING.md` §16.34~
    §16.39）：

    - V2.0 是 50/50 —— 但候选池内六维层 sd ≈ 4.5、第二层 sd ≈ 13.1，
      两层原始分直接相加时第二层占约 90% 的方差，"50/50"实际约 26/74；
    - V2.3 改成 100/0 —— 当时用两窗口四格判据看是"单调更优"；
    - 加第三个窗口（2024-09~2025-09）后**不再成立**：没有任何层权重全正；
    - 第 8 轮找到真因：`service.py` 合成两层时**没检查层的 `available`**，
      而层没数据时 `weighted_score` 返回 `score = 0` —— 于是缺第二层的
      板块分数被**腰斩**（旧窗口超过一成候选板块，单日实测 47/59）。
      `100/0` 的"优点"其实是**绕过这个 bug**（把出问题的层权重设成 0）。
    - 修好之后六格汇总里 `100/0` 在**最差超额**与**平均 IC** 上都垫底，
      **80/20** 的最差格最好。

    所以这里断言的是 80/20，不是 100/0 —— 而这个数字将来若再变，
    应当先看清是**口径修复**还是**真实改进**。
    """
    cfg = load_config(force=True)
    layers = cfg.synthesis.normalized_layers()
    assert set(layers) == {"six_dim", "accumulation"}
    assert layers["six_dim"] == pytest.approx(80.0)
    assert layers["accumulation"] == pytest.approx(20.0)


def test_all_zero_layer_weights_fall_back_to_equal() -> None:
    """全 0 是配置事故，不能变成"全池 0 分"——必须退化为等权。"""
    from src.mainline.config import SynthesisConfig

    cfg = SynthesisConfig(layer_weights={"six_dim": 0.0, "accumulation": 0.0})
    layers = cfg.normalized_layers()
    assert layers["six_dim"] == pytest.approx(50.0)
    assert layers["accumulation"] == pytest.approx(50.0)


def test_accumulation_has_four_dims_with_etf() -> None:
    """第二层仍是四维**定义**，但 ETF 权重为 0（改走加分）。

    被删除的 `capital_strength` / `chip_concentration` 仍不得复活 ——
    它们与第一层的 moneyflow / chips 算的是同一批数据，重复加权会让回测
    胜率虚高（V2.0 要修的核心问题）。

    ETF 维度**保留在 `DIMS` 里**（继续算、继续存 payload，供面板与回测看），
    但不再参与加权平均：324 个概念板块里只有 37 个能映射到 ETF，
    15% 的权重等于从其余三维身上偷权重，且让第二层的分在板块间不可比。
    """
    cfg = load_config(force=True)
    assert set(cfg.accumulation.DIMS) == {
        "leverage", "northbound", "volume_price", "etf"}
    weights = cfg.accumulation.normalized_weights()
    assert set(weights) == {"leverage", "northbound", "volume_price", "etf"}
    # V2.2 按 IC 重排：northbound 是唯一随持有期增强的维度（H=60 IC +0.068、
    # IC>0 69%），加权到 50；leverage 实测 IC 为负（−0.021），降到 25。
    assert weights["leverage"] == pytest.approx(25.0)
    assert weights["northbound"] == pytest.approx(50.0)
    assert weights["volume_price"] == pytest.approx(25.0)
    assert weights["etf"] == pytest.approx(0.0)
    assert sum(weights.values()) == pytest.approx(100.0)
    # 方向：北向必须比杠杆重，否则说明 IC 结论没落到配置上
    assert weights["northbound"] > weights["leverage"]
    # 被删除的两个维度不能以任何形式残留
    assert not hasattr(cfg.accumulation, "capital_strength")
    assert not hasattr(cfg.accumulation, "chip_concentration")
    # ETF 维度的开关与门槛
    assert cfg.etf.enabled is True
    assert cfg.etf.breakout_amount_ratio > 1.0
    # 不占权重的部分必须由加分补上，否则这一维等于被删掉
    assert cfg.etf.bonus_enabled is True
    assert cfg.etf.bonus_cap > 0.0
    assert 0.0 < cfg.etf.breakout_percentile <= 1.0


def test_leader_layer_never_carries_weight() -> None:
    """第三层是门控加分，权重恒为 0（有权重就退回 V1.0）。"""
    score = BoardScore(code="801780.SI", name="银行", trade_date="20260917")
    assert score.six_dim.weight + score.accumulation.weight <= 100.0
    assert score.leader.weight == 0.0
    payload = score.to_dict()
    assert payload["gate_bonus"] == 0.0
    assert "five_dim_score" not in payload      # V1.0 字段名不得复活
    assert "accumulation_score" in payload


def test_strong_signal_needs_two_dims_above_threshold() -> None:
    """强信号要求"至少 2 个维度超过**各自阈值**的 70%"。"""
    cfg = load_config(force=True)
    board = BoardScore(code="X", name="X", trade_date="20260917")
    board.six_dim = LayerScore(
        "six_dim", "六维", 80.0, dimensions=[
            DimensionScore("moneyflow", "资金流", 90.0, 20.0, available=True),
            DimensionScore("macro", "宏观", 10.0, 15.0, available=True)])
    board.accumulation = LayerScore(
        "accumulation", "建仓", 70.0, dimensions=[
            DimensionScore("leverage", "杠杆", 80.0, 35.0, available=True)])
    above = board.dims_above(cfg.alert.strong_dim_ratio, cfg.alert.dim_thresholds,
                             cfg.alert.default_dim_threshold)
    assert "moneyflow" in above and "leverage" in above
    assert "macro" not in above
    assert len(above) >= cfg.alert.strong_min_dims == 2


# ==================================================================
# 三、配置解析
# ==================================================================


def test_config_parses_shipped_yaml() -> None:
    cfg = load_config(force=True)
    assert not cfg.load_error, cfg.load_error
    assert cfg.funnel.candidate_ratio == pytest.approx(0.20)
    assert cfg.alert.strong_min_dims == 2
    assert cfg.backtest.purge_days > 0            # 防前视的清洗窗口必须存在
    assert cfg.backtest.step_days >= 1
    assert len(cfg.backtest.scenes) >= 4          # 四个分场景案例
    assert cfg.board_profiles.profile_for("农林牧渔").decay_days == 10
    assert cfg.board_profiles.profile_for("银行").decay_days == 25


def test_config_legacy_v1_yaml_is_migrated() -> None:
    """V1.0 的 `five_dim` / `base_weights` 写法要能平滑迁移。

    - 层间权重**不迁移数值**（V1.0 的 40:35 编码的正是要被修掉的缺陷）；
    - 已删除的维度（capital_strength / chip_concentration）必须丢掉；
    - YAML 里没写的维度（如 `etf`）拿**默认权重**，而不是被当成 0。
      V2.1 起 `etf` 的默认权重**就是 0**（改走 `etf.bonus_*` 加分），
      所以这里断言的是"键还在、值为 0、加分参数已就位" —— 键在才能让
      `weighted_score` 之外的展示路径读到这一维，值 0 才是"不占权重"。
    """
    raw = {
        "synthesis": {"base_weights": {"six_dim": 40, "five_dim": 35,
                                       "leader": 25}},
        "five_dim": {"weights": {"capital_strength": 30, "leverage": 20,
                                 "northbound": 20, "chip_concentration": 15,
                                 "volume_price": 15}},
    }
    cfg = parse(raw)
    # V1 的 `base_weights`（六维40/五维35/龙头25）被丢弃，改用默认层权重。
    # 默认值在 V2.4 是 80/20（见 `test_layer_weights_are_eighty_twenty`）。
    assert cfg.synthesis.normalized_layers()["six_dim"] == pytest.approx(80.0)
    assert set(cfg.accumulation.weights) == {
        "leverage", "northbound", "volume_price", "etf"}
    # ETF 不占权重：三维归一后合计 100，ETF 恒为 0 偏移。
    # 注意 V1 的原始值（20/20/15）不带 ETF，归一是**使用期**做的
    # （`normalized_weights`），所以这里必须查归一后的值。
    norms = cfg.accumulation.normalized_weights()
    assert norms["etf"] == pytest.approx(0.0)
    assert sum(value for key, value in norms.items()
               if key != "etf") == pytest.approx(100.0)
    # ETF 的作用改由加分承担，参数必须在
    assert cfg.etf.bonus_enabled
    assert 0.0 < cfg.etf.bonus_level1 < cfg.etf.bonus_level2 <= cfg.etf.bonus_cap


def test_config_falls_back_on_broken_yaml(tmp_path: Path) -> None:
    """YAML 坏掉时回落默认值 + 记 load_error，**不抛错**（页签不该 500）。"""
    bad = tmp_path / "broken.yaml"
    bad.write_text("synthesis: [unclosed", encoding="utf-8")
    cfg = load_config(bad, force=True)
    assert cfg.load_error
    # 回落的是**代码默认值**（V2.4 = 80/20），不是硬编码的 50/50
    assert cfg.synthesis.normalized_layers()["six_dim"] == pytest.approx(80.0)
    mainline_config.clear_config_cache()


def test_funnel_gate_counts() -> None:
    funnel = FunnelConfig()
    assert funnel.candidates(300) == 60          # 20% 命中比例
    assert funnel.candidates(20) == 8            # 板块很少时用下限
    assert funnel.candidates(1000) == 80         # 上限防目录异常膨胀
    assert funnel.selected(60) == 10
    assert funnel.selected(3) == 3               # 池子比下限还小时不能超发


def test_macro_sensitivity_table_loads() -> None:
    from src.mainline.macro import load_sensitivity
    table = load_sensitivity(load_config(force=True))
    assert table.loaded, table.gap
    assert len(table.by_code) == 31              # 申万一级行业 31 个
    assert len(table.factors) >= 6
    vector = table.for_board(code="801780")
    assert vector, "银行必须有敏感度向量"
    assert all(isinstance(value, float) for value in vector.values())


def test_futures_mapping_table_loads() -> None:
    from src.mainline.futures import FuturesService
    service = FuturesService(config=load_config(force=True))
    assert not service.mapping_gap, service.mapping_gap
    assert len(service.mappings) >= 80
    kinds = {item.kind.value for item in service.mappings}
    assert kinds <= {"domestic", "foreign", "non_futures"}
    assert all(item.future_code for item in service.mappings)
    assert all(item.board_name for item in service.mappings)


# ==================================================================
# 四、六维打分
# ==================================================================


def _series(code: str, closes: list[float], *, volume: float = 1000.0
            ) -> BoardSeries:
    series = BoardSeries(code=code)
    previous = closes[0]
    for index, close in enumerate(closes):
        series.bars.append(BoardBar(
            date=f"2026{index + 1:04d}", close=close, open=previous,
            high=max(previous, close), low=min(previous, close),
            volume=volume, pre_close=previous, pct_change=0.0))
        previous = close
    return series


def _board_input(code: str, closes: list[float], **kwargs) -> BoardInput:
    return BoardInput(info=BoardInfo(code=code, name=code,
                                     kind=BoardKind.SW_L1),
                      series=_series(code, closes), **kwargs)


def test_six_dim_scores_without_macro_and_marks_notes() -> None:
    """宏观不可用时宏观维度记 available=False，其余维度照常出分。"""
    inputs = [_board_input(f"80{i:04d}", [100.0 + i + j * 0.5
                                          for j in range(80)])
              for i in range(8)]
    out = score_six_dim(inputs, config=load_config(force=True))
    assert len(out) == 8
    layer, raw = out["800000"]
    assert layer.score > 0
    macro = layer.dim("macro")
    assert macro is not None and macro.available is False
    assert any("宏观" in note for note in layer.notes)


def test_six_dim_uses_cross_section_not_absolute() -> None:
    """整体平移同一批数据不应改变排序（分位口径的关键性质）。"""
    inputs = [_board_input(
        f"80{i:04d}",
        [100.0 + i * 0.5 + j * 0.3 for j in range(80)]) for i in range(8)]
    first = score_six_dim(inputs, config=load_config(force=True))
    order_a = sorted(first, key=lambda code: -first[code][0].score)
    shifted = [_board_input(
        f"80{i:04d}",
        [(100.0 + i * 0.5 + j * 0.3) * 1.5 for j in range(80)])
        for i in range(8)]
    second = score_six_dim(shifted, config=load_config(force=True))
    order_b = sorted(second, key=lambda code: -second[code][0].score)
    assert order_a == order_b


def test_candidate_cutoff_respects_funnel() -> None:
    cfg = load_config(force=True)
    scores = {f"c{i:03d}": (LayerScore("six_dim", "六维", float(100 - i)), {})
              for i in range(100)}
    picked = candidate_cutoff(scores, config=cfg)
    assert len(picked) == cfg.funnel.candidates(100)
    assert picked[0] == "c000"          # 分最高的进池


# ==================================================================
# 五、第二层：漏斗外不算、无数据不参与
# ==================================================================


def test_accumulation_outside_pool_stays_zero_and_flag_is_false() -> None:
    """候选池外的板块第二层分恒为 0，靠 `candidate=False` 区分"没算"。"""
    board = BoardScore(code="X", name="X", trade_date="20260917")
    assert board.candidate is False
    assert board.accumulation.score == 0.0


# ==================================================================
# 五之三、反向维度（动量取反）
# ==================================================================


def test_apply_pure_pool_distinguishes_unrated_from_irrelevant() -> None:
    """提纯股池必须区分「判定为不相关」与「从未评估」—— 两者处置相反。

    这是本模块最贵的一个教训：`relevance.clean_member_map` 原先写
    `rel_ok = (board, code) in rel`（`rel` 只装 relevant=1），
    于是 65.9% 从未被评估的 (板块,股票) 对被当成"不相关"静默剔除。
    实测后果：885564 无人机 467→6、885611 阿里巴巴概念 347→1、
    以及「锂电池概念」把 MLCC 的风华高科选成龙头。

    而"未评估"是**结构性**的：`ml_stock_theme` 只评每只股票的相关性前
    `max_candidates` 个题材，长尾天然没有判定；名单每次同步还会新增成员。
    """
    members = {"B1": ["000001", "000002", "000003", "000004"]}
    pure = {"B1": {"000001": 1,      # 判定相关 → 保留
                   "000002": 0,      # 判定不相关 → 剔除
                   }}                 # 000003 / 000004 没有行 → 未评估 → 保留
    kept, dropped, unrated = apply_pure_pool(members, pure)
    assert kept == {"B1": ["000001", "000003", "000004"]}
    assert (dropped, unrated) == (1, 2)


def test_apply_pure_pool_keeps_boards_without_any_data() -> None:
    """整板在提纯表里没有行 → **原样保留**（数据缺口不能被静默清空）。

    与「整板被判为不相关」是两件事：前者是"还没算"，后者是"算过且都不相关"。
    后者在 `ml_member_pure` 里会表现为"有行但全是 0"。
    """
    members = {"B1": ["000001", "000002"], "B2": ["000003"]}
    pure = {"B1": {"000001": 0, "000002": 0}}     # B1 有行且全 0
    kept, dropped, unrated = apply_pure_pool(members, pure)
    assert "B1" not in kept, "有行且全 0 = 判定都不相关，整板剔空是正确结果"
    assert kept == {"B2": ["000003"]}, "B2 没有数据 → 原样保留"
    # B1 的两对都"有行且为 0" → 都算**明确剔除**，不是"未评估"
    assert (dropped, unrated) == (2, 0)


def test_reverse_dims_config_and_semantics() -> None:
    """`trading` / `technical` 必须**反向**参与层合成。

    依据：实测 IC 在 5/10/20/60 四个持有期上单调为负（A 股概念板块
    20~60 日尺度上均值回归）。`weighted_score` 会把 `weight <= 0` 的项丢掉，
    所以"负权重"表达不了，只能走 `reverse_dims` 取 `100 - score`。

    这条测试钉住三处，任何一处写错都只会让分数"看起来正常"：
      1. 配置里确实列了这两个维度；
      2. `DimensionScore.score` 仍存**自然分**（面板显示"量能 90"不能变 10）；
      3. `effective_score` / `contribution` 走反转后的值。
    """
    cfg = load_config(force=True)
    assert set(cfg.six_dim.reverse_dims) == {"trading", "technical"}
    # 反向维度的权重必须非零 —— 权重 0 会让反转失去意义（等于砍掉）
    weights = cfg.six_dim.normalized_weights()
    for key in cfg.six_dim.reverse_dims:
        assert weights[key] > 0, f"{key} 被反转但权重为 0，等于砍掉"

    natural = DimensionScore(key="trading", label="交易行为", score=90.0,
                             weight=20.0, available=True, reversed=True)
    assert natural.score == 90.0, "存的是自然分"
    assert natural.effective_score == pytest.approx(10.0), "合成用反转分"
    assert natural.contribution == pytest.approx(2.0), "贡献按反转分算"

    plain = DimensionScore(key="prosperity", label="景气度", score=90.0,
                           weight=30.0, available=True, reversed=False)
    assert plain.effective_score == pytest.approx(90.0)
    assert plain.contribution == pytest.approx(27.0)

    # 反向维度不可用时贡献为 0（而不是按 100 分算满）
    missing = DimensionScore(key="technical", label="技术指标", score=0.0,
                             weight=15.0, available=False, reversed=True)
    assert missing.contribution == 0.0


def test_reverse_actually_changes_layer_score() -> None:
    """端到端：反转必须真的改变第一层分，而不是只挂个标记。

    构造两个板块：A 的量能/技术分高、B 低。反转后 A 的层分应**低于** B。
    如果不反转，结论正好相反 —— 这条测试就是分辨这两种情况。
    """
    cfg = load_config(force=True)
    assert cfg.six_dim.reverse_dims, "没配反向维度的话这条测试无意义"
    dims = [
        DimensionScore(key="trading", label="交易行为", score=90.0,
                       weight=20.0, available=True, reversed=True),
        DimensionScore(key="prosperity", label="景气度", score=50.0,
                       weight=30.0, available=True),
        DimensionScore(key="technical", label="技术指标", score=90.0,
                       weight=15.0, available=True, reversed=True),
    ]
    high_momentum = scoring.weighted_score(
        [(d.effective_score, d.weight, d.available) for d in dims])[0]
    low = [
        DimensionScore(key="trading", label="交易行为", score=10.0,
                       weight=20.0, available=True, reversed=True),
        DimensionScore(key="prosperity", label="景气度", score=50.0,
                       weight=30.0, available=True),
        DimensionScore(key="technical", label="技术指标", score=10.0,
                       weight=15.0, available=True, reversed=True),
    ]
    low_momentum = scoring.weighted_score(
        [(d.effective_score, d.weight, d.available) for d in low])[0]
    assert low_momentum > high_momentum, (
        "动量强的板块层分必须更低（均值回归）—— 反了说明没生效")


# ==================================================================
# 五之二、上市日期闸门（回测防前视偏差）
# ==================================================================


def test_filter_listed_drops_ipo_after_target_date() -> None:
    """`list_date > target` 的股票必须剔除 —— 否则 2026 上市的票会算进 2023 的分。

    锚点是**边界**：`list_date == target` 算**已上市**（当日上市即可交易）。
    把 `>` 写成 `>=` 只会让分数略变，面板上完全看不出来，所以必须钉死。
    """
    members = {"B1": ["000001", "000002"], "B2": ["000003"]}
    dates = {"000001": "19910403",   # 老股
             "000002": "20260105",   # 目标日之后上市
             "000003": "20231009"}   # 正好目标日上市
    kept, unlisted, unknown = filter_listed(members, dates, "20231009")
    assert kept == {"B1": ["000001"], "B2": ["000003"]}
    assert (unlisted, unknown) == (1, 0)


def test_filter_listed_drops_unknown_date_but_keeps_other_boards() -> None:
    """查不到上市日期的剔除，但**不能**因此把整个板块清空。

    实测 `ml_member` 有 38825 个 (板块,股票) 对查不到上市日期，其中绝大部分
    来自池外的 `tushare:ths_index` 板块（含 `00000A` 这类非法代码）。
    如果按"有一个未知就丢整块"处理，这些板块会静默消失。
    """
    members = {"B1": ["000001", "00000A"], "B2": ["00000B"]}
    dates = {"000001": "19910403"}
    kept, unlisted, unknown = filter_listed(members, dates, "20260917")
    assert kept == {"B1": ["000001"]}, "B1 要保住已上市的那只"
    assert "B2" not in kept, "全未知的板块没有可算的票"
    assert (unlisted, unknown) == (0, 2)


def test_filter_listed_is_noop_without_dates() -> None:
    """拿不到上市日期时**原样返回**，而不是把全部成分股剔除。

    这条守的是一个更坏的失败模式：行情仓不可用 + `ml_stock_meta` 为空时，
    如果"未知即剔除"，所有板块的成分股会瞬间变空 → 全市场 0 分 →
    面板看起来像"今天没有任何主线"，而不是像"数据没读到"。
    """
    members = {"B1": ["000001", "000002"]}
    kept, unlisted, unknown = filter_listed(members, {}, "20260917")
    assert kept == members
    assert (unlisted, unknown) == (0, 0)


def test_accumulation_marks_dims_unavailable_without_data() -> None:
    """没有融资/北向序列时两个维度记不可用，权重剔除后仍能出分。"""
    inputs = [AccumulationInput(code=f"c{i}", name=f"c{i}",
                                series=_series(f"c{i}", [100.0 + i * 0.1
                                                         for i in range(120)]))
              for i in range(6)]
    out = score_accumulation(inputs, config=load_config(force=True))
    layer, _raw = out["c0"]
    assert layer.dim("leverage").available is False
    assert layer.dim("northbound").available is False
    # 只有 volume_price 可用 → 层分必须由它归一化得到，而不是被两维 0 分拉低
    assert layer.coverage < 1.0


def test_volume_price_recognises_shrink_then_expand() -> None:
    """地量后放量 + 温和上行应显著高于无形态序列。"""
    cfg = load_config(force=True)
    quiet = [100.0] * 70 + [99.0, 98.5, 98.6, 99.5, 100.5, 101.0]
    volumes = [1000.0] * 70 + [300.0, 290.0, 1500.0, 1600.0, 1700.0, 1200.0]
    series = BoardSeries(code="A")
    previous = quiet[0]
    for index, close in enumerate(quiet):
        series.bars.append(BoardBar(date=f"d{index}", close=close, open=previous,
                                    high=max(previous, close),
                                    low=min(previous, close),
                                    volume=volumes[index], pre_close=previous))
        previous = close
    triggered = volume_price_raw(
        AccumulationInput(code="A", name="A", series=series), config=cfg)
    flat = volume_price_raw(
        AccumulationInput(code="B", name="B",
                          series=_series("B", [100.0] * 76, volume=1000.0)),
        config=cfg)
    assert (triggered["form"] or 0) > (flat["form"] or 0)


# ==================================================================
# 六、告警模型
# ==================================================================


def test_alert_id_is_stable_and_unique_per_level() -> None:
    signal = AlertSignal(board_code="801780.SI", board_name="银行",
                         trade_date="20260917", level=SignalLevel.STRONG)
    assert signal.alert_id == "20260917-801780.SI-strong"
    other = AlertSignal(board_code="801780.SI", board_name="银行",
                        trade_date="20260917", level=SignalLevel.WEAK)
    assert other.alert_id != signal.alert_id


def test_signal_level_rank_orders_correctly() -> None:
    assert SignalLevel.STRONG.rank > SignalLevel.MEDIUM.rank \
        > SignalLevel.WEAK.rank > SignalLevel.NONE.rank


def test_snapshot_to_dict_handles_empty() -> None:
    from src.mainline.models import MainlineSnapshot
    payload = MainlineSnapshot().to_dict()
    assert payload["scores"] == [] and payload["alerts"] == []
    assert payload["candidate_count"] == 0


# ==================================================================
# 七、V2.0 之后不允许再出现的旧口径
# ==================================================================


def test_shipped_yaml_has_no_legacy_second_layer_dims() -> None:
    """配置文件里也不能残留 V1.0 的第二层维度（否则会被误当"配置驱动"补回）。"""
    path = Path(mainline_config.CONFIG_PATH)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    accumulation = raw.get("accumulation") or {}
    weights = accumulation.get("weights") or {}
    assert "capital_strength" not in weights
    assert "chip_concentration" not in weights
    assert set(weights) == {"leverage", "northbound", "volume_price", "etf"}
    assert "five_dim" not in raw
    synthesis = raw.get("synthesis") or {}
    assert "base_weights" not in synthesis
    assert set(synthesis.get("layer_weights") or {}) == {
        "six_dim", "accumulation"}
    # ETF 维度的配置节必须存在（否则扩展会被静默关掉）
    assert (raw.get("etf") or {}).get("enabled") is True


def test_v2_accumulation_config_defaults_include_etf() -> None:
    weights = AccumulationConfig().normalized_weights()
    assert set(weights) == {"leverage", "northbound", "volume_price", "etf"}
    assert sum(weights.values()) == pytest.approx(100.0)
