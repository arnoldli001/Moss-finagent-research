"""主线提纯：`select()` 的**放行原因**要能区分「个股新股」与「板块太年轻」。

## 为什么要单独钉这两条

`corr=None` 在旧实现里只有一种解释：这只**股票**没有价格历史（新股）。
但实测发现还有第二种成因 —— **板块自己**的行情短于 `MIN_CORR_SAMPLES`(60)
时，共同交易日永远凑不够，于是该概念的**每一只**成分股都拿不到 corr。
整块概念等于一次过滤都没做，却和"某只新股"记成同一个 `decision="new"`、
同一句 note（"新股无价格历史"）。

实测规模：886111 玻璃基板（板块行情 58 日）56/56 只 corr=NULL、
886112 MLCC概念（36 日）33/33 只 corr=NULL —— 而全表 corr 为 NULL 的行
只占 0.4%。也就是说这两种情况的**放行面差两个数量级**，混在一起会让
"提纯已生效"这句话变得不可验证。

本组测试钉住：两者的 `decision` 与 note 必须不同，且**放行行为都不变**
（都是全量放行），因为它只是可审计性修复，不是过滤口径变更。
"""

from __future__ import annotations

from src.mainline.member_pure import MIN_CORR_SAMPLES, select


def _corr(count: int, *, value: float | None) -> dict[str, float | None]:
    return {f"{i:06d}": value for i in range(count)}


def test_young_board_members_are_labelled_board_young() -> None:
    picks = select(_corr(56, value=None), board_name="玻璃基板", cache={},
                   total=56, board_samples=58)
    assert {pick.decision for pick in picks} == {"board_young"}
    note = picks[0].note
    assert "58" in note and "未过滤" in note
    # 不能再说"新股"——那会把板块问题说成个股问题
    assert "新股" not in note


def test_new_stock_is_still_labelled_new() -> None:
    """板块行情充足、只有个别股票没有历史 → 仍是 `new`。"""
    corr = {f"{i:06d}": 0.9 for i in range(30)}
    corr["999999"] = None
    picks = select(corr, board_name="芯片概念", cache={}, total=31,
                   board_samples=719)
    by_code = {pick.code: pick for pick in picks}
    assert by_code["999999"].decision == "new"
    assert "新股" in by_code["999999"].note


def test_missing_board_samples_keeps_legacy_behaviour() -> None:
    """不传 `board_samples`（旧调用方）时退回原语义，不引入新分支。"""
    picks = select(_corr(12, value=None), board_name="某概念", cache={},
                   total=12, board_samples=None)
    assert {pick.decision for pick in picks} == {"new"}


def test_young_board_is_still_included() -> None:
    """可审计性修复**不改放行口径**：年轻板块的成分股仍然全部纳入。"""
    picks = select(_corr(20, value=None), board_name="MLCC概念", cache={},
                   total=20, board_samples=36)
    assert len(picks) == 20
    assert all(pick.decision == "board_young" for pick in picks)


def test_board_at_threshold_is_not_young() -> None:
    """正好 `MIN_CORR_SAMPLES` 天算"够"，边界不能算年轻。"""
    picks = select(_corr(5, value=None), board_name="边界概念", cache={},
                   total=5, board_samples=MIN_CORR_SAMPLES)
    assert {pick.decision for pick in picks} == {"new"}


def test_young_board_marker_does_not_affect_normal_filtering() -> None:
    """有 corr 时该走哪条路还是哪条路 —— `board_samples` 不参与打分。"""
    corr = {f"{i:06d}": 0.2 for i in range(60)}
    corr["000000"] = 0.9          # 直接归属
    picks = select(corr, board_name="大概念", cache={}, total=61,
                   board_samples=58)
    by_code = {pick.code: pick for pick in picks}
    assert by_code["000000"].decision == "direct"
    assert "board_young" not in {pick.decision for pick in picks}


def test_corr_direct_does_not_override_a_low_business_score() -> None:
    """**Task 1**（`business_veto=True` 时）：判过且不达标则相关性不得覆盖。

    用户 2026-09-22 看到氟化工概念的龙头里有雅克科技（主营半导体材料）
    提出质疑。根因之一就是这条：原规则的 `corr >= corr_direct` 分支
    **完全不看业务分**，于是 LLM 明确给了 <70 分的股票只要股价跟着板块涨
    就照样进池。实测在池成员里这类有 **2880 条**（占已纳入的 23.5%）。

    ⚠️ 三种情况必须分清 —— 这条测试把三种都钉住，因为最容易写错的是
    "把没判过（score is None）也一起罚"：那会误伤 52% 的 corr 成员，
    它们只是没排上 LLM 候选，不代表业务不相关。

    ⚠️ 本测试**显式打开** `business_veto=True`。它默认是**关闭**的
    （实测按预设判据回退，见 `test_business_veto_is_off_by_default`）。
    """
    from src.mainline.member_pure import BUSINESS_PASS, CORR_DIRECT, select

    corr = {"LOWBIZ": 0.90, "NOJUDGE": 0.90, "HIGHBIZ": 0.90}
    cache = {("LOWBIZ", "测试概念"): 40.0,        # 判过、不达标 → 拦
             ("HIGHBIZ", "测试概念"): 90.0}       # 判过、达标 → 放
    picks = {p.code: p for p in select(corr, board_name="测试概念", cache=cache,
                                       total=3, business_veto=True)}
    assert picks["LOWBIZ"].decision == "weak", "业务不达标不得靠 corr 直通"
    assert "业务判定优先" in picks["LOWBIZ"].note
    assert str(BUSINESS_PASS) in picks["LOWBIZ"].note
    assert picks["NOJUDGE"].decision == "direct", \
        "LLM 没判过的不该罚（corr 照旧直通）"
    assert picks["HIGHBIZ"].decision == "direct"
    assert CORR_DIRECT <= 0.90


def test_business_veto_is_off_by_default() -> None:
    """⚠️ **默认关闭**，且默认行为必须与现行 `ml_member_pure` 一致。

    这条不是为了"少拦几只"，而是为了**代码与数据不错位**：
    Task 1 实测跑过一次完整全流程（提纯 + 903 天全量重打分），
    **按工具里事先写死的判据回退了**（启动集 FP/TP 留出窗口 1.27→1.38 变差）。
    库里现存 5322 条 `source='corr'` 且业务分 <70 的成员，就是"未否决"的产物
    （含用户报障的 002140 东华科技 @ 885652 钛白粉概念）。

    如果哪天把默认值改成 `True`，池子会在**没人预期**的情况下变一次，
    而历史分数不会自动重算 —— 正是 §16.74 警告的"新池子 + 旧分数"混合态。
    """
    from src.mainline.config import load_config
    from src.mainline.member_pure import select

    # ① 配置默认值（唯一真源：configs/mainline.yaml）
    cfg = load_config(force=True)
    assert cfg.relevance.business_veto is False, \
        "mainline.yaml 的 relevance.business_veto 必须是 false"

    # ② 不传参时不得拦截（= 现行口径）
    corr = {"LOWBIZ": 0.90}
    cache = {("LOWBIZ", "测试概念"): 40.0}
    picks = {p.code: p for p in select(corr, board_name="测试概念", cache=cache,
                                       total=1)}
    assert picks["LOWBIZ"].decision == "direct", \
        "默认必须放行 —— 与现行 ml_member_pure 的 source='corr' 保持一致"
    assert "业务判定优先" not in picks["LOWBIZ"].note

    # ③ 显式传 False 与不传必须同结论（开关而不是三态）
    explicit_off = {p.code: p for p in select(corr, board_name="测试概念",
                                              cache=cache, total=1,
                                              business_veto=False)}
    assert explicit_off["LOWBIZ"].decision == picks["LOWBIZ"].decision

    # ④ 但详情层仍然能把"判过且不及格"如实标出来（展示不受开关影响）：
    #    入池通道由 `datastore._admission(source, score)` 推出，
    #    与是否否决无关 —— 这正是 §16.75 那次报障要的信息。
    assert picks["LOWBIZ"].business_score == 40.0


def test_theme_name_mismatch_is_resolved_not_silently_missed() -> None:
    """板块名与 LLM 题材名写法不一致时也必须命中（460 条曾经漏掉）。

    板块叫「氟化工**概念**」，而 `ml_stock_theme` 里可能只写了「氟化工」。
    原来 `load_cache` 按 `(code, theme)` 精确建键、`select` 用 `board_name`
    精确查 —— 那条判定**永远查不到**，股票被当成"没判过"转走 corr。
    实测在池成员里 460 条属于这种，其中既有正向（芯片概念=95）也有
    **反向**（AIGC概念=58、光纤概念=62）—— 后者本该被业务否决权拦住。

    ⚠️ 本测试**显式打开** `business_veto=True`：它要验证的是"归一化命中之后
    否决权能生效"，而否决权默认是关的。归一化本身与开关无关，仍然恒生效。
    """
    from src.mainline.member_pure import (
        business_of,
        normalize_theme,
        select,
    )

    assert normalize_theme("氟化工概念") == "氟化工"
    assert normalize_theme("车联网(车路协同)") == "车联网"
    assert normalize_theme("AIGC概念") == "AIGC"
    cache = {("X", "氟化工"): 30.0}
    assert business_of(cache, "X", "氟化工概念") == 30.0, "归一化兜底必须生效"
    picks = {p.code: p for p in select({"X": 0.9}, board_name="氟化工概念",
                                       cache=cache, total=1,
                                       business_veto=True)}
    assert picks["X"].decision == "weak", \
        "归一化命中之后，业务否决权必须照样生效"
