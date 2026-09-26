"""主线挖掘：板块成分股池（`pool.py`）的单元测试。

对应需求：把"蹭概念"的噪声股从概念板块里去掉，并允许人工增删。

每条测试钉住一个具体的失效方式：

1. **裁剪只影响打分，不动原始留档** —— `ml_member` 一条都不能删。
   这由"裁剪返回新 dict"保证；这里用"不修改入参"钉住。
2. **小板块不裁** —— 6 只成分股再取 30% 就剩 2 只，板块级统计失去意义。
3. **龙头强制保留** —— 否则会出现"把龙头裁掉、然后问为什么没有龙头共振"。
4. **人工 include 优先于自动规则** —— 这是"让我来配置"的核心用途：
   自动规则漏掉的票，人工加进来必须生效。
5. **ST 剔除**，且名称判定的全角/半角星号都要认。
"""

from __future__ import annotations

import pytest

from src.mainline.config import load_config
from src.mainline.pool import (
    PoolRule,
    PoolRules,
    is_st,
    load_rules,
    resolve_pool,
)


def _stats(**kwargs) -> dict[str, dict[str, float]]:
    return {code: dict(value) for code, value in kwargs.items()}


def _mk(n: int, *, amount_start: float = 1.0e8) -> dict[str, dict[str, float]]:
    """造 n 只股票：成交额依次递减，市值都够大。"""
    out: dict[str, dict[str, float]] = {}
    for index in range(n):
        code = f"6000{index:02d}"
        out[code] = {"amount_5d": amount_start - index * 1.0e6,
                     "net_5d": 1.0e7, "circ_mv": 1.0e10, "name": f"股票{index}"}
    return out


def _members(n: int) -> list[str]:
    return [f"6000{index:02d}" for index in range(n)]


# ==================================================================
# 一、基本裁剪
# ==================================================================


def _cfg(**overrides):
    """取一份**启用成交额口径**的配置。

    `configs/mainline.yaml` 里 `pool.enabled` 已改为 `false`（现行口径是
    `relevance`），但本文件的测试全部针对 `pool` 成交额口径，所以显式打开 ——
    让测试钉住「pool 的行为」，而不是「配置文件当前的开关状态」。

    ⚠️ 不要试图用 monkeypatch 改 `load_config`：`pool.py` 是
    `from ... import load_config` 的**值导入**，patch 模块属性不会影响
    它已经绑定的引用。显式传 `_cfg()` 才是可靠的写法。
    """
    cfg = load_config(force=True)
    cfg.pool.enabled = True
    for key, value in overrides.items():
        setattr(cfg.pool, key, value)
    return cfg


def test_top_pct_keeps_the_most_traded() -> None:
    """按成交额取前 30%：真正在动的票留下，长尾剔掉。"""
    cfg = _cfg(top_pct=0.30, always_keep_top=0)
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "测试板块"},
                           market_stats=_mk(20), config=cfg, rules=PoolRules())
    assert outcome.detail["B"]["before"] == 20
    assert outcome.detail["B"]["after"] == 6
    # 成交额最高的那几只必须在保留列表里
    assert "600000" in outcome.members["B"]
    assert "600019" in outcome.detail["B"]["dropped"]


def test_small_board_is_not_trimmed() -> None:
    """成分股少于 min_members 时不裁剪（再裁就没剩几只）。"""
    cfg = _cfg(min_members=8)
    outcome = resolve_pool({"B": _members(6)}, board_names={"B": "小板块"},
                           market_stats=_mk(6), config=cfg, rules=PoolRules())
    assert outcome.detail["B"]["after"] == 6
    assert "不裁剪" in outcome.detail["B"]["reason"]


def test_keep_min_floor_applies() -> None:
    """裁剪后至少保留 keep_min 只（30% 的 10 只只有 3 只）。"""
    cfg = _cfg(top_pct=0.30, keep_min=5, always_keep_top=0, min_members=8)
    outcome = resolve_pool({"B": _members(10)}, board_names={"B": "板块"},
                           market_stats=_mk(10), config=cfg, rules=PoolRules())
    assert outcome.detail["B"]["after"] == 5


def test_top_ranked_are_always_kept() -> None:
    """成交额前 always_keep_top 名强制保留 —— 龙头不该被裁掉。"""
    cfg = _cfg(top_pct=0.30, always_keep_top=3, keep_min=1, min_members=8)
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "板块"},
                           market_stats=_mk(20), config=cfg, rules=PoolRules())
    for code in ("600000", "600001", "600002"):
        assert code in outcome.members["B"]


def test_disabled_pool_keeps_everything() -> None:
    cfg = _cfg(enabled=False)
    outcome = resolve_pool({"B": _members(50)}, board_names={"B": "板块"},
                           market_stats=_mk(50), config=cfg, rules=PoolRules())
    assert outcome.members["B"] == _members(50)
    assert outcome.enabled is False


# ==================================================================
# 二、人工规则优先
# ==================================================================


def test_manual_include_overrides_automatic_trim() -> None:
    """人工 include 的票必须在保留列表里 —— 这正是"让我来配置"的用途。"""
    cfg = _cfg(top_pct=0.10, always_keep_top=0, keep_min=1)
    rules = PoolRules(by_board={"B": PoolRule(include=["600019"])})
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "板块"},
                           market_stats=_mk(20), config=cfg, rules=rules)
    assert "600019" in outcome.members["B"]


def test_manual_exclude_wins_even_over_top_rank() -> None:
    """人工 exclude 优先级最高：连成交额第一名也要剔掉。"""
    cfg = _cfg()
    rules = PoolRules(by_board={"B": PoolRule(exclude=["600000"])})
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "板块"},
                           market_stats=_mk(20), config=cfg, rules=rules)
    assert "600000" not in outcome.members["B"]
    assert "600000" in outcome.detail["B"]["dropped"]


def test_keep_all_skips_filtering() -> None:
    cfg = _cfg()
    rules = PoolRules(by_board={"B": PoolRule(keep_all=True)})
    outcome = resolve_pool({"B": _members(50)}, board_names={"B": "板块"},
                           market_stats=_mk(50), config=cfg, rules=rules)
    assert outcome.members["B"] == _members(50)
    assert "不过滤" in outcome.detail["B"]["reason"]


def test_keyword_rule_matches_by_longest_name() -> None:
    """按板块名关键字匹配，取最长命中（与 board_profiles 同口径）。"""
    rules = PoolRules(by_keyword=[("医疗", PoolRule(exclude=["600000"])),
                                  ("医疗服务", PoolRule(keep_all=True))])
    assert rules.rule_for("X", "医疗服务").keep_all is True
    assert rules.rule_for("X", "医疗器械").exclude == ["600000"]
    assert rules.rule_for("X", "银行").exclude == []


def test_board_code_rule_beats_keyword_rule() -> None:
    rules = PoolRules(by_board={"881175.TI": PoolRule(keep_all=True)},
                      by_keyword=[("医疗服务", PoolRule(exclude=["600000"]))])
    assert rules.rule_for("881175.TI", "医疗服务").keep_all is True


def test_missing_rule_file_is_not_an_error() -> None:
    """没有规则文件是正常状态（默认全走自动规则），不该报 gap。"""
    cfg = load_config(force=True)
    cfg.pool.rule_file = "definitely_missing_file.yaml"
    rules = load_rules(cfg)
    assert rules.loaded is False
    assert rules.gap == ""


def test_shipped_rule_file_loads() -> None:
    cfg = load_config(force=True)
    cfg.pool.rule_file = "mainline_pools.yaml"
    rules = load_rules(cfg)
    assert rules.loaded is True


# ==================================================================
# 三、ST 与市值
# ==================================================================


@pytest.mark.parametrize("name,expected", [
    ("ST诺泰", True), ("*ST万方", True), ("＊ST海投", True),
    ("退市海润", True), ("药明康德", False), ("STAR股份", False),
])
def test_st_detection(name: str, expected: bool) -> None:
    """ST 判定要认全角星号，且不能把以 ST 开头的正常名误判。"""
    assert is_st(name) is expected


def test_st_members_are_dropped() -> None:
    cfg = _cfg(exclude_st=True)
    stats = _mk(20)
    stats["600019"]["name"] = "ST退市"
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "板块"},
                           market_stats=stats, config=cfg, rules=PoolRules())
    assert "600019" not in outcome.members["B"]


def test_min_circ_mv_filters_small_caps_when_enabled() -> None:
    """市值门槛默认关闭；开启后小市值票被剔除。

    默认关闭是刻意的：小市值票常是题材的**早期龙头**，一刀切会把它们筛掉。
    """
    cfg = _cfg()
    assert cfg.pool.min_circ_mv == 0.0, "默认不该设市值门槛"
    stats = _mk(20)
    for code in _members(20)[10:]:
        stats[code]["circ_mv"] = 1.0e9      # 10 亿，低于 50 亿门槛
    cfg = _cfg(min_circ_mv=5.0e9, top_pct=1.0, always_keep_top=0)
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "板块"},
                           market_stats=stats, config=cfg, rules=PoolRules())
    assert outcome.detail["B"]["after"] == 10


# ==================================================================
# 四、不改动原始数据
# ==================================================================


def test_original_member_map_is_not_mutated() -> None:
    """裁剪返回新 dict，**不修改入参** —— 原始成分股留档不能被改掉。"""
    cfg = _cfg()
    original = {"B": _members(20)}
    snapshot = {key: list(value) for key, value in original.items()}
    outcome = resolve_pool(original, board_names={"B": "板块"},
                           market_stats=_mk(20), config=cfg, rules=PoolRules())
    assert original == snapshot
    assert outcome.members["B"] is not original["B"]


def test_summary_reports_ratio() -> None:
    cfg = _cfg(top_pct=0.30, always_keep_top=0, keep_min=1)
    outcome = resolve_pool({"B": _members(20)}, board_names={"B": "板块"},
                           market_stats=_mk(20), config=cfg, rules=PoolRules())
    text = outcome.summary()
    assert "20 → 6" in text
    assert outcome.before_total == 20 and outcome.after_total == 6
