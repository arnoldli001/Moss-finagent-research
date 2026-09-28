"""情报分析的**成本策略** —— 本地优先，云端要过闸门。

## 用户口径（2026-09-25）

> 要考虑 token 费用问题，能做成免费获取和分析的就做成免费的，
> 少用云端付费大模型

## 三层成本模型

| 层 | 用什么 | 成本 | 什么时候用 |
|---|---|---|---|
| **L0 规则** | 纯代码（关键词、正则、计数、排序） | **零** | **默认**。能在这一层解决的绝不往上走 |
| **L1 本地模型** | Ollama（**具体是哪个模型见 `configs/models.yaml` 的 `local_light` / `local_medium`**） | **零**（只吃电） | L0 做不了的语义任务：情绪归类、要点抽取 |
| **L2 云端模型** | DeepSeek 等 | **按 token 计费** | **仅**需要跨源综合推理，且过了下面的闸门 |

> ⚠️ **本地模型的名字只写在 `configs/models.yaml` 一处。**
> 本模块曾经有两个常量 `LOCAL_MODEL = "qwen3:8b"` / `LOCAL_MODEL_SMALL = "qwen2.5:1.5b"`
> —— 它们**生产代码零引用**（只被测试与文档提到），却在本地模型从 8B 换成
> `qwen3.5:4b` 之后变成**过期路标**：下一个人会照着它去找一个已经不在链上的模型。
> 2026-09-28 已删除，并加了护栏
> `tests/unit/test_local_model_single_source.py`（配置键必须存在 + 名字只能来自配置）。

## 闸门（防止"顺手调云端"变成习惯）

L2 必须**同时**满足：

  1. 调用方显式声明 `allow_cloud=True`（默认 False，**必须主动打开**）
  2. 预估输入 token 在预算内（`CLOUD_TOKEN_BUDGET_PER_TASK`）
  3. 当日云端调用未超上限（`CLOUD_CALLS_PER_DAY`）

任一条不满足 → **降级到 L1**，而不是报错。
理由是：情报分析降级到本地只是质量略降，**报错却是功能不可用**。

## 为什么把 L0 摆在最前

情报流的绝大多数需求其实是**计数与排序**：
"哪个板块被提及最多"、"今天有几条政策信号"、"环比多少" ——
这些**用规则就能算准**，而且比模型更可复算（用户能自己验算）。

把这类问题丢给模型，既费钱又不可复现 —— 同一个问题两次问会有不同答案。
所以 `classify_by_rules()` 优先，`needs_llm()` 只在规则确实做不到时才返回 True。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 单任务云端 token 预算（输入+输出）。超了降级本地。
CLOUD_TOKEN_BUDGET_PER_TASK: Final = 8000

#: 每日云端调用上限。**默认很低** —— 逼着走本地，
#: 需要时由运维显式调高（`INTEL_CLOUD_CALLS_PER_DAY`）。
CLOUD_CALLS_PER_DAY: Final = 20

#: ★ 本地模型的名字**刻意不在这里定义**（2026-09-28 删除 `LOCAL_MODEL` /
#: `LOCAL_MODEL_SMALL`）。理由有两条，缺一条都不足以删：
#:
#:   ① **它们是死的**：生产代码零引用（全仓库只有测试函数名与文档提过），
#:      所以删掉不影响任何行为 —— 这一点必须先查清，否则删常量就是改功能。
#:   ② **它们是过期路标**：值写的是 `qwen3:8b` / `qwen2.5:1.5b`，而
#:      `configs/models.yaml` 里真实登记的是 `qwen3:8b-q4_K_M` /
#:      `qwen2.5:1.5b-instruct-q4_K_M`，且本地地板已于同日换成 `qwen3.5:4b`。
#:      留着它，下一个人会照着它去找一个**已经不在路由链上**的模型。
#:
#: 单一真值源 = `configs/models.yaml` 的 `models` / `routing` 两段。
#: 护栏：`tests/unit/test_local_model_single_source.py`
#: （配置键必须存在 + 名字只能来自配置 + 这两个常量不许长回来）。

#: 本地模型单次送分析的条数上限（防卡死，见 zsxq_incremental 的同款理由）
LOCAL_MAX_ITEMS: Final = 20

# ── 规则层：能在 L0 解决的，绝不往上走 ──

#: 情绪/方向词表。**只做"原文倾向"归类**，不代表平台判断。
_BULL = ("上调", "超预期", "增长", "突破", "放量", "中标", "订单", "扩产",
         "扭亏", "新高", "提价", "获批", "量产")
_BEAR = ("下调", "低于预期", "下滑", "亏损", "减持", "质押", "违规", "处罚",
         "退市", "停产", "降价", "解禁", "商誉减值")

#: 政策信号词（新闻联播/上期所这类政务文本的快速判别）
_POLICY_HINT = re.compile(
    r"(国务院|发改委|工信部|财政部|央行|证监会|统计局|部署|印发|通知|"
    r"座谈会|会议指出|审议通过|规划|意见|办法)")

#: 快讯里的"无信息量"条目（盘面播报等），L0 直接过滤，不送模型
_NOISE = re.compile(r"(午评|收评|早盘|开盘|涨停潮|跌停潮|指数(low)?震荡|"
                    r"沪指|深指|创业板指).{0,20}(涨|跌)")


@dataclass
class AnalysisDecision:
    """该条内容该走哪一层。"""

    tier: str            # rules | local | cloud
    reason: str = ""
    #: L0 的结果（tier == "rules" 时有值）
    verdict: dict[str, Any] | None = None


def classify_by_rules(title: str, summary: str) -> AnalysisDecision:
    """L0：用规则判断，能定就定。**绝大多数条目在这里就结束。**"""
    text = f"{title} {summary}".strip()

    # ① 盘面播报类：无分析价值，直接标记跳过（连模型都不用进）
    if _NOISE.search(text):
        return AnalysisDecision(
            "rules", "盘面播报类，无需分析",
            {"kind": "noise", "skip_llm": True})

    # ② 政策信号：政务词表命中即可归类
    if _POLICY_HINT.search(text):
        return AnalysisDecision(
            "rules", "命中政策词表",
            {"kind": "policy", "skip_llm": False})

    # ③ 原文倾向：正负词计数。**只统计"原文这么说"，不是我们的判断。**
    bull = sum(1 for w in _BULL if w in text)
    bear = sum(1 for w in _BEAR if w in text)
    if bull or bear:
        if bull > bear:
            lean, conf = "偏多", min(0.5 + 0.1 * (bull - bear), 0.9)
        elif bear > bull:
            lean, conf = "偏空", min(0.5 + 0.1 * (bear - bull), 0.9)
        else:
            lean, conf = "中性", 0.4
        return AnalysisDecision(
            "rules", f"词表计数（多{bull}/空{bear}）",
            {"kind": "sentiment", "lean": lean, "confidence": round(conf, 2),
             "bull_hits": bull, "bear_hits": bear})

    # ④ 规则定不了 → 才考虑本地模型
    return AnalysisDecision("local", "规则无法归类，需语义判断")


def needs_llm(decision: AnalysisDecision) -> bool:
    """是否需要模型（本地或云端）。"""
    if decision.tier == "rules" and decision.verdict:
        # 政策类虽然 L0 归了类，但要抽取要点 → 仍需本地模型
        return decision.verdict.get("kind") == "policy"
    return decision.tier in ("local", "cloud")


def _cloud_calls_today() -> int:
    """当日云端调用计数（从审计链读，不额外建状态）。"""
    try:
        import json
        from pathlib import Path

        p = Path("data/audit/llm_audit.jsonl")
        if not p.exists():
            return 0
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        n = 0
        # 只读尾部若干行，避免大文件全量扫描
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
        for ln in lines:
            if today not in ln:
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if str(rec.get("provider") or "").lower() not in ("", "ollama", "local"):
                n += 1
        return n
    except Exception:  # noqa: BLE001 计数失败不该阻断分析
        return 0


def pick_tier(*, allow_cloud: bool = False,
              estimated_tokens: int = 0) -> AnalysisDecision:
    """决定用哪一层。**默认本地** —— 云端必须显式允许且过闸门。"""
    if not allow_cloud:
        return AnalysisDecision("local", "未显式允许云端（默认本地）")

    cap = int(os.environ.get("INTEL_CLOUD_CALLS_PER_DAY",
                             str(CLOUD_CALLS_PER_DAY)))
    used = _cloud_calls_today()
    if used >= cap:
        return AnalysisDecision(
            "local", f"当日云端调用已达上限（{used}/{cap}），降级本地")

    if estimated_tokens > CLOUD_TOKEN_BUDGET_PER_TASK:
        return AnalysisDecision(
            "local",
            f"预估 {estimated_tokens} tokens 超单任务预算 "
            f"{CLOUD_TOKEN_BUDGET_PER_TASK}，降级本地")

    return AnalysisDecision(
        "cloud", f"已允许且未超限（今日 {used}/{cap}）")


__all__ = [
    "CLOUD_CALLS_PER_DAY",
    "CLOUD_TOKEN_BUDGET_PER_TASK",
    "LOCAL_MAX_ITEMS",
    "AnalysisDecision",
    "classify_by_rules",
    "needs_llm",
    "pick_tier",
]
