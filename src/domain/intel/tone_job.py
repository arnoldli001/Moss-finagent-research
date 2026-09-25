"""原文倾向的**抽取编排**（定时任务调这个）。

## 职责边界

    tone.py        单条的判定与拦截（纯函数，可单测）
    tone_store.py  结果存储（JSONL）
    本模块         取哪些条、限多少量、调模型、落库

## 用户口径（2026-09-25）

> "原则上有幻觉风险的可以不显示数值，可信度低的也不做倾向分析。"

两条都在这里落地：

  · **可信度低的直接跳过**，连模型都不调（省算力，也避免"抽错了没人发现"）
  · **有幻觉风险的项不显示数值** —— 由 `tone.py` 的交叉验证保证：
    两层不一致时给 `未定`，`confidence` 为 `None`

## 为什么必须限流

本地模型单条 ~770ms。一次抽 200 条就是 2.5 分钟 ——
定时任务可以慢，但**不能占着模型不放**（其它任务与用户请求都要用）。
所以 `MAX_PER_RUN` 默认 40 条，2 小时一批在情报量上是够的；
不够时下一批继续（水位线就是"哪些还没抽过"，天然去重）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Final

from src.domain.intel import tone_store
from src.domain.intel.tone import (
    SYSTEM_PROMPT,
    TONE_UNKNOWN,
    build_prompt,
    extract_tone,
)

logger = logging.getLogger(__name__)

#: 单次抽取条数上限（防占着模型不放，见模块 docstring）
MAX_PER_RUN: Final = 40

#: 并发度。**刻意是 1**：本地 Ollama 单实例，并发只会互相排队，
#: 而且把 GPU/CPU 打满会拖慢用户请求。串行 770ms/条，40 条约 31 秒。
CONCURRENCY: Final = 1

#: 送模型的文本长度上限（与 `tone.build_prompt` 内的截断一致）
_HEAD: Final = 500


async def _extract_one(gateway: Any, item: dict[str, Any]) -> dict[str, Any]:
    """抽一条。**模型失败不影响规则层** —— 拿不到 JSON 就只交规则结果。"""
    text = f"{item.get('title') or ''} {item.get('summary') or ''}".strip()
    cred = item.get("credibility") or {}
    try:
        score = int(cred.get("score"))
    except (TypeError, ValueError):
        score = 0

    obj: dict[str, Any] | None = None
    if score >= 50 and text:
        try:
            resp = await gateway.complete(
                "light", SYSTEM_PROMPT, build_prompt(text[:600]),
                agent_id="intel_tone", json_mode=True, max_tokens=200)
            raw = str(getattr(resp, "content", "") or "").strip()
            if raw.startswith("```"):
                import re
                raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                obj = parsed
        except Exception as exc:  # noqa: BLE001 模型不可用不该让任务失败
            logger.info("倾向抽取模型失败（退回规则层）：%s", type(exc).__name__)

    res = extract_tone(text=text, credibility_score=score, llm_obj=obj)
    pub = res.to_public()
    return {
        "content_hash": str(item.get("content_hash") or ""),
        "tone": pub["tone"],
        "has_tone": pub["has_tone"],
        "phrases": pub["phrases"],
        "codes": pub["codes"],
        "confidence": pub["confidence"],
        "source": pub["source"],
        "explain": pub["explain"],
    }


async def run_once(*, gateway: Any, items: list[dict[str, Any]],
                   max_items: int = MAX_PER_RUN,
                   root: Any = None) -> dict[str, Any]:
    """抽一批并落库。返回统计。

    `items` 是**已经带 `credibility` 的情报条目**（即 `build_feed` 的产物）。
    没有 `content_hash` 的条目跳过 —— 没有指纹就查不回来，抽了也没用。
    """
    known = tone_store.load(root=root)
    todo: list[dict[str, Any]] = []
    skipped_low = 0
    skipped_done = 0
    skipped_no_hash = 0
    for it in items:
        h = str(it.get("content_hash") or "")
        if not h:
            skipped_no_hash += 1
            continue
        if h in known:
            skipped_done += 1
            continue
        cred = it.get("credibility") or {}
        try:
            score = int(cred.get("score"))
        except (TypeError, ValueError):
            score = 0
        if score < 50:
            # 低可信：**不做倾向分析**（用户口径）。这里就跳过，
            # 连模型都不调 —— 省算力，也避免"抽错了没人发现"。
            skipped_low += 1
            continue
        todo.append(it)

    # ★ **优先抽最新的**。
    #
    # 实测问题：池子 301 条而单次上限 40 —— 按传入顺序取前 40 的话，
    # 要 8 批（16 小时）才覆盖一遍，而**默认视图看的是最近 60 条**，
    # 于是用户打开页面看到的绝大多数是"倾向尚未抽取"。
    # 按时间倒序取，每批都先把"用户马上会看到的那批"抽掉。
    from src.infrastructure.connectors.intel_sources import sort_key

    todo.sort(key=lambda x: sort_key(x.get("published_at")), reverse=True)
    todo = todo[:max(1, max_items)]

    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(item: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await _extract_one(gateway, item)

    if todo:
        results = await asyncio.gather(*(_guarded(i) for i in todo))

    written = tone_store.save_many(results, root=root)
    pruned = tone_store.prune(root=root)
    tones: dict[str, int] = {}
    for r in results:
        t = str(r.get("tone") or TONE_UNKNOWN)
        tones[t] = tones.get(t, 0) + 1
    return {
        "considered": len(items),
        "extracted": len(results),
        "written": written,
        "skipped_low_credibility": skipped_low,
        "skipped_already_done": skipped_done,
        "skipped_no_hash": skipped_no_hash,
        "pruned": pruned,
        "tones": tones,
    }


__all__ = ["CONCURRENCY", "MAX_PER_RUN", "run_once"]
