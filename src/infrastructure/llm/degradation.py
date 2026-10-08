"""语义缓存**降级状态**：把一堆计数凝成一句可读的结论 + 病因 + 处置（`CHG-0203`）。

## ⚠️ 先分清："降级"在这个仓库里有**两个不相干的东西**（`CHG-0192`）

| | 叫什么 | 在哪 | 管什么 |
|---|---|---|---|
| **模型降级链** | `chain = self._routing[task_tier]` | `gateway.py` 的 `complete()` | 主模型失败 → 换备模型 → 再换 → 本地地板；结果落在 `LLMResponse.provider_chain` / `fallback_used` |
| **（本模块）缓存降级** | `describe()` → `state ∈ {ok, degraded, no_traffic}` | 这里 | 语义缓存/精排器**还能不能干活**、为什么不能、该怎么办 |

**两者没有任何调用关系**。本模块的模块名 `degradation` 容易被读成前者 ——
所以在这里点明：**要看"这次调用降级到哪一跳"，去 `gateway.py`；
要看"缓存层还健不健康"，才是这里。**
判据 `tests/unit/test_degradation_scope.py` 钉住这条边界（防止有人
按名字把两者当成一件事，顺手删掉其中一个）。

## 为什么需要它（`CHG-0199` 的现场）

余额耗尽那次，系统的真实状态是：

    精排器 `configured=True`（**配置上一切正常**）
    调用后 `{calls: 0, failures: 1}`
    返回 `None` ⇒ L3 拿不到向量 ⇒ **静默退回 3-gram 阈值规则**

而**唯一能看出这件事的地方**是 `/health` 里一个需要人主动去翻的
`embed_client.failures` 计数。⇒ 三级缓存退化成两级，**没有任何人知道**。

## 本模块的立场：**不新建告警通道**（`CHG-0199` 建议 #6）

本仓库已有 `collection_anomalies` + `/health` + `metrics.kind_labels` 三套面。
再加一个通道 = 多一个要维护、且**最容易被忽略**的地方。
所以这里只做一件事：**把"降级了没有 / 为什么 / 该怎么办"算出来**，
由既有面去呈现。

## 为什么"病因"必须分开（同一个 `failures` 计数藏了三种病）

    HTTP 402 余额不足   ⇒ 只能**充值**（重试、调超时都无用）
    网络抖动 / 超时      ⇒ 重试或调超时；持续则**接受降级**
    全桶没有向量        ⇒ **不是故障**，是存量欠账（跑回填，`CHG-0203`）

三种处置完全不同。把它们压成"failures>0 ⇒ 告警"的后果是：
**运维一定会去查错方向**（本项目为此付过代价：9 条告警阈值红灯被当噪音两天）。
"""
from __future__ import annotations

from typing import Any, Final

#: 判定器状态 → (结论, 病因, 该怎么办)
#: `None` 的病因 = 不是故障。
_OK: Final = "ok"
_DEGRADED: Final = "degraded"
_NO_TRAFFIC: Final = "no_traffic"


def _client_failure_reason(stats: dict[str, Any] | None, name: str) -> str:
    """从客户端计数里读病因。

    ⚠️ **口径要记牢**：失败时 `calls` **不增**、只有 `failures` 增 ⇒
    `calls=0 & failures>0` 是"每次都失败"的签名，
    与"从没被调用过"（两者都是 0）**是两件完全不同的事**（`CHG-0199`）。

    ⚠️ 这里**读不出 402 还是超时** —— 客户端只记了数量、没记分类。
    要细分必须让客户端按错误类别计数（本模块**未做**，已登记为缺口）。
    所以 `why` 给的是**待查方向**，不是结论。
    """
    if not stats:
        return ""
    failures = int(stats.get("failures") or 0)
    calls = int(stats.get("calls") or 0)
    if failures and not calls:
        return (f"{name} **每次都失败**（calls=0 / failures={failures}）—— "
                "先分清是「额度/权限被拒」（立即返回）还是「网络黑洞」（等满超时），"
                "两者的处置完全相反")
    if failures:
        return f"{name} 部分失败（calls={calls} / failures={failures}）"
    return ""


def describe(cache_stats: dict[str, Any]) -> dict[str, Any]:
    """`LLMCache.stats()` → 降级结论。

    Returns: `{"state", "judge", "why", "action", "counters"}`。
    `state` ∈ `{ok, degraded, no_traffic}`。

    ## 判据（四个计数器两两可分，`CHG-0203`）

        attempts>0 & judged>0            ⇒ ok（真的在判）
        attempts>0 & judged=0            ⇒ **degraded**（每次都在降级）
        attempts=0 & skipped_prompt>0    ⇒ no_traffic（调用点没传 anchor）
        attempts=0 & skipped_prompt=0    ⇒ no_traffic（语义层没被走到）

    `CHG-0202` 里 **0/5007 带向量却被读成"L3 没跑"** 正是缺了第二行那个状态。
    """
    attempts = int(cache_stats.get("l3_attempts") or 0)
    judged = int(cache_stats.get("l3_judged") or 0)
    skipped = int(cache_stats.get("l3_skipped_prompt_mode") or 0)
    disabled = int(cache_stats.get("semantic_disabled") or 0)
    fallbacks = int(cache_stats.get("rerank_fallbacks") or 0)
    judge = str(cache_stats.get("judge") or "?")
    # ★ 复用率（`CHG-0208`）。上面那五个数全是"**某一层内部**"的数，
    #   这一组才是"整条链路"的数 —— 没有它，上面的数再多也算不出比率。
    #   ⚠️ `lookups` 缺失时默认 0 ⇒ **既有调用点（含测试）行为逐字不变**。
    lookups = int(cache_stats.get("lookups") or 0)
    hits_exact = int(cache_stats.get("hits_exact") or 0)
    hits_semantic = int(cache_stats.get("hits_semantic") or 0)

    counters = {
        "l3_attempts": attempts,
        "l3_judged": judged,
        "l3_skipped_prompt_mode": skipped,
        "semantic_disabled": disabled,
        "rerank_fallbacks": fallbacks,
        # ★ 复用率：让"越搜越聪明"从感觉变成数字（`CHG-0208`）
        "lookups": lookups,
        "hits_exact": hits_exact,
        "hits_semantic": hits_semantic,
        "misses": int(cache_stats.get("misses") or 0),
        # `None` = **没查过**，不是 0%（「没量到 ≠ 量到 0」）
        "reuse_rate": cache_stats.get("reuse_rate"),
        # ★★ 复用**年龄**（`CHG-0209`）—— **TTL 的直接依据**。
        #    `reuse_rate` 答"有没有复用"，这一组答"复用的是多久以前写的"。
        #    每个桶的上界就是一个候选 TTL。
        #    ⚠️ 这是**白名单**：不加进来，缓存层算得再对，面板上也看不到。
        "reuse_age_exact": cache_stats.get("reuse_age_exact") or {},
        "reuse_age_semantic": cache_stats.get("reuse_age_semantic") or {},
        # 存量条目（无 `created_at`）的命中数 —— 不报它，
        # 那部分会被读成"复用都发生在 1 分钟内"。
        "reuse_age_unknown": int(cache_stats.get("reuse_age_unknown") or 0),
    }

    if attempts == 0:
        # ★★ 新增分支（`CHG-0208`）：**先判"L1 全包了"这种好情况**。
        #
        # 没有 `lookups` 之前，`attempts == 0` 一律报"语义层一次都没被走到"。
        # 但"查了 200 次、200 次都由 L1 精确命中满足"与"一次都没查过"
        # **是完全相反的两件事**，却报同一句话 —— 前者说明缓存工作得很好
        # （用户重复问同一句话），后者说明根本没流量。
        # ⇒ 这个新分支是 `lookups` 计数器带来的直接收益。
        if lookups > 0 and hits_exact == lookups:
            return {"state": _OK, "judge": judge, "counters": counters,
                    "why": f"{lookups} 次查找**全部由 L1 精确命中满足**"
                           f"（复用率 100%）⇒ 语义层没被走到是**正常的**，"
                           f"它这一批不需要上场",
                    "action": "无需处置。⚠️ 别把它读成'语义层没生效' —— "
                              "语义层要等'**同一件事换个说法**'出现才有活干；"
                              "L1 全命中说明调用方在重复问**同一句话**"}
        # 「没量到」不是「量到 0」（本项目铁律）⇒ 不报 ok。
        if skipped:
            return {"state": _NO_TRAFFIC, "judge": judge, "counters": counters,
                    "why": f"有 {skipped} 次 prompt 模式的语义层查找，"
                           f"但 **anchor 模式的判定一次都没发生** —— "
                           f"调用点没传 anchor，或 bucket 里没有候选",
                    "action": "先确认调用点传了 anchor（A08–A20 / A17 已接线）；"
                              "若已接线，说明这些查找落在空桶里（不是故障）"}
        if disabled:
            return {"state": _NO_TRAFFIC, "judge": judge, "counters": counters,
                    "why": f"语义层被调用点显式关掉 {disabled} 次（`semantic_cache=False`）"
                           f"—— 这是**设计如此**，不是故障",
                    "action": "无需处置；这类调用点的输出逐字引用输入，本来就不该复用"}
        return {"state": _NO_TRAFFIC, "judge": judge, "counters": counters,
                "why": ("语义层一次都没被走到 —— **没量到，不是量到 0**"
                        if lookups == 0 else
                        f"查了 **{lookups} 次**、一次都没进判定阶段"
                        f"（L1 精确命中 {hits_exact} 次）⇒ "
                        f"**桶里没有候选**，不是判定器不工作"),
                "action": "确认缓存开关与调用路径；不要把它读成'语义层没用'"}

    if judged > 0:
        # 真的在判。但 `rerank_fallbacks` 说明有几次退回了 —— 仍要报出来。
        if fallbacks:
            return {"state": _OK, "judge": judge, "counters": counters,
                    "why": f"判定正常（{judged}/{attempts} 次拿到分数），"
                           f"但有 **{fallbacks} 次退回**了较弱判据",
                    "action": "看 rerank_client.failures 找退回原因；"
                              "偶发抖动可接受，持续退回要查端点"}
        return {"state": _OK, "judge": judge, "counters": counters,
                "why": f"判定正常：{judged}/{attempts} 次拿到判定分数"
                       f"（判定器 = {judge}）",
                "action": "无需处置"}

    # judged == 0 且 attempts > 0 ⇒ **每一次都在降级**
    why = (f"进了判定阶段 **{attempts} 次，一次都没拿到判定分数** ⇒ "
           f"语义层每次都在降级")
    reasons = [
        r for r in (
            _client_failure_reason(cache_stats.get("rerank_client"), "cross-encoder"),
            _client_failure_reason(cache_stats.get("embed_client"), "embedding"),
        ) if r
    ]
    return {
        "state": _DEGRADED,
        "judge": judge,
        "counters": counters,
        "why": why + ("；" + "；".join(reasons) if reasons else ""),
        "action": ("按 `rerank_client` / `embed_client` 的 `calls` 与 `failures` 分档："
                   "① `calls=0 & failures>0` ⇒ **端点在被拒**（查额度/权限，"
                   "本项目实测过余额耗尽 `HTTP 402 code:30001`）；"
                   "② 两者都是 0 ⇒ **全桶没有向量**，跑 "
                   "`scripts/backfill_cache_embeddings.py`（不是故障，是存量欠账）；"
                   "③ `failures=0` 而 judged=0 ⇒ 候选为空，退回了 3-gram 阈值规则"),
    }


__all__ = ["describe"]
