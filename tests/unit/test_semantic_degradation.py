"""语义缓存**降级结论**的判据（`CHG-0203`）—— 纯函数、不联网。

## 守的是什么

`CHG-0199` 的现场：余额耗尽时系统真实状态是「配置上一切正常 +
`calls=0 / failures=1` + 返回 None」，而**唯一能看出这件事的地方**
是一个需要人主动去翻的计数。⇒ 三级退化成两级，**没有任何人知道**。

本判据不测"有没有告警"（那需要活服务），只测**结论算得对不对**：
四种状态两两可分、病因不混、处置指向正确方向。

## 为什么"四个状态两两可分"是硬要求

    尝试了 & 判成了      ⇒ 正常
    尝试了 & 没判成      ⇒ **每次都在降级**（端点被拒 / 全桶无向量）
    没尝试 & 没传 anchor ⇒ 调用点的问题
    没尝试 & 也没跳过    ⇒ **没量到**（不许读成"量到 0"）

把它们合并成一个数，"端点挂了"就会和"没接上线"长得一样 ——
而它们的处置一个是查额度、一个是改代码。

## ★ 第五态：`lookups` 带来的"L1 全包了"（`CHG-0208`）

上面四态全是"**语义层内部**"的状态。加了 `lookups`（查找总数）之后，
多出一对**方向相反**却曾经报同一句话的情况：

    查了 200 次、200 次都由 L1 精确命中 ⇒ 缓存工作得**很好**（用户重复问同一句）
    一次都没查过                        ⇒ **没量到**

没有 `lookups` 时两者都报"语义层一次都没被走到"。这是"没量到 ≠ 量到 0"
在**反方向**上的同一个错误：把"好"读成了"没有"。
"""
from __future__ import annotations

from src.infrastructure.llm.degradation import describe


def _stats(**kw) -> dict:
    base = {"l3_attempts": 0, "l3_judged": 0, "l3_skipped_prompt_mode": 0,
            "semantic_disabled": 0, "rerank_fallbacks": 0, "judge": "cross-encoder",
            "rerank_client": None, "embed_client": None}
    base.update(kw)
    return base


def test_ok_when_judging() -> None:
    r = describe(_stats(l3_attempts=10, l3_judged=10))
    assert r["state"] == "ok"
    assert "10/10" in r["why"]
    assert r["counters"]["l3_judged"] == 10


def test_ok_but_reports_partial_fallbacks() -> None:
    """判定正常**也要报出退回次数** —— 否则"偶发抖动"会积累成"质量下降"。"""
    r = describe(_stats(l3_attempts=10, l3_judged=8, rerank_fallbacks=2))
    assert r["state"] == "ok"
    assert "2 次退回" in r["why"]
    assert "failures" in r["action"]


def test_degraded_when_attempted_but_never_judged() -> None:
    """★ **每一次都在降级**必须单独成一态 —— 这正是 `CHG-0202` 读错的那个。"""
    r = describe(_stats(
        l3_attempts=7, l3_judged=0,
        rerank_client={"configured": True, "calls": 0, "failures": 7}))
    assert r["state"] == "degraded", (
        "「尝试了 7 次、一次都没判成」没被判成降级")
    assert "7 次" in r["why"] and "一次都没拿到" in r["why"]
    assert "每次都失败" in r["why"], (
        "`calls=0 & failures>0` 这个签名没被认出来 —— 它是"
        "「端点在被拒」与「从没被调用过」的唯一分界")
    # 处置必须指向"查额度/权限"，而不是笼统地说"去看日志"
    assert "额度" in r["action"] or "余额" in r["action"]


def test_degraded_without_any_failure_means_no_vectors() -> None:
    """★ `failures=0` 而 `judged=0` ⇒ **全桶没有向量**，这是存量欠账、不是故障。

    两者处置完全相反（一个跑回填、一个查端点）。若混为一谈，
    运维会去查一个没坏的端点。
    """
    r = describe(_stats(
        l3_attempts=5, l3_judged=0,
        rerank_client={"configured": True, "calls": 0, "failures": 0},
        embed_client={"configured": True, "calls": 0, "failures": 0}))
    assert r["state"] == "degraded"
    assert "backfill" in r["action"], (
        "没把「全桶无向量」指向回填脚本 —— 那是存量欠账的标准处置")
    assert "不是故障" in r["action"]


def test_no_traffic_is_not_ok() -> None:
    """★ **没量到 ≠ 量到 0**：一次都没走到时**不许**报 `ok`。"""
    r = describe(_stats())
    assert r["state"] == "no_traffic", (
        "语义层一次都没被走到却报了 ok —— 那是把「没量到」当成「量到 0」")
    assert "没量到" in r["why"]


def test_prompt_mode_skips_are_not_reported_as_ok() -> None:
    """只有 prompt 模式查找 ⇒ 也是 `no_traffic`，且**指向 anchor 没传**。"""
    r = describe(_stats(l3_skipped_prompt_mode=42))
    assert r["state"] == "no_traffic"
    assert "anchor" in r["why"]
    assert "anchor" in r["action"]


def test_semantic_disabled_is_designed_not_a_fault() -> None:
    """`semantic_cache=False` 的调用点**是设计如此**，不许报成故障。

    否则"输出逐字引用输入的调用点"（`intel_extract` / `A06_extractor`）
    每次正常工作都会让面板变红 —— 那是**狼来了**。
    """
    r = describe(_stats(semantic_disabled=99))
    assert r["state"] == "no_traffic"
    assert "设计如此" in r["why"]
    assert "无需处置" in r["action"]


def test_four_states_are_pairwise_distinguishable() -> None:
    """★ 四态**两两可分**（这是本文件存在的理由）。"""
    states = {
        "ok": describe(_stats(l3_attempts=3, l3_judged=3)),
        "degraded": describe(_stats(l3_attempts=3, l3_judged=0)),
        "prompt_only": describe(_stats(l3_skipped_prompt_mode=3)),
        "nothing": describe(_stats()),
    }
    seen = {(k, v["state"], v["why"]) for k, v in states.items()}
    whys = [v["why"] for v in states.values()]
    assert len(set(whys)) == len(whys), f"有状态的结论文字重复了：{whys}"
    assert len(seen) == 4
    # 每一条都必须给得出"该怎么办"，而不是只报症状
    for name, v in states.items():
        assert v["action"].strip(), f"{name} 没有给处置建议"


# ===========================================================================
# ★ 复用率带来的两个新分支（`CHG-0208`）
# ===========================================================================


def test_all_exact_hits_is_ok_not_no_traffic() -> None:
    """★★ **L1 全命中是好消息，不许报成"没量到"。**

    没有 `lookups` 之前，`attempts == 0` 一律报"语义层一次都没被走到 ——
    没量到，不是量到 0"。但"查了 200 次、200 次都由 L1 满足"说明缓存
    **工作得很好**（调用方在重复问同一句话），报"没量到"会把好消息说成坏消息。
    """
    r = describe(_stats(lookups=200, hits_exact=200, hits_semantic=0,
                        misses=0, reuse_rate=1.0))
    assert r["state"] == "ok", f"L1 全命中却报了 {r['state']}"
    assert "全部由 L1 精确命中" in r["why"]
    assert "正常" in r["why"]
    assert r["counters"]["lookups"] == 200


def test_lookups_without_candidates_is_distinguishable_from_never_asked() -> None:
    """★ "查了 50 次但桶里没候选" 必须与 "一次都没查过" **文字可分**。

    两者的运维动作完全不同：前者要去看为什么桶是空的（TTL 过期？
    `agent_id` 没传？），后者只是"还没流量"。
    """
    asked = describe(_stats(lookups=50, hits_exact=0, hits_semantic=0,
                            misses=50, reuse_rate=0.0))
    never = describe(_stats())
    assert asked["state"] == never["state"] == "no_traffic"
    assert "查了 **50 次**" in asked["why"]
    assert "桶里没有候选" in asked["why"]
    assert asked["why"] != never["why"], "两种情况报了同一句话"


def test_absent_lookups_keeps_the_legacy_behaviour() -> None:
    """⚠️ **默认值即护栏**：`lookups` 缺失时行为与改动前**逐字一致**。

    既有调用方（含本文件上面那些用例）传的 stats 里没有这个键 ——
    新分支不许改变它们的结论，否则这是一次静默的行为变更。
    """
    r = describe(_stats())            # 注意：`_stats()` 里**没有** lookups
    assert r["state"] == "no_traffic"
    assert "没量到，不是量到 0" in r["why"]
    assert r["counters"]["lookups"] == 0


def test_reuse_rate_none_is_not_flattened_to_zero() -> None:
    """★ `reuse_rate=None`（没查过）不许被压成 `0.0`（缓存完全没用）。

    这条与 `LLMCache.stats()` 的实现是一对：那边在 `lookups == 0` 时给 `None`，
    这边必须**原样透传**。任何一处 `or 0` 都会把两者混起来。
    """
    assert describe(_stats())["counters"]["reuse_rate"] is None
    assert describe(_stats(lookups=4, hits_exact=3, reuse_rate=0.75)
                    )["counters"]["reuse_rate"] == 0.75
