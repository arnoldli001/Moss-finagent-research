"""两阶段LLM事件分析器（domain服务，仅经LLMGateway，FR-5）。

阶段一 medium：批量分类/情感/实体（失败回退本地关键词分类，不阻断）；
阶段二 reasoning：批量风险/机会打分、受影响个股、传导路径（失败返回空）。
单事件JSON非法或评分数值越界（FR-6）→ 丢弃该事件，不影响其他事件。
每次扫描网关调用：阶段一 ⌈N/10⌉ 次 + 阶段二 ⌈N/8⌉ 次（N = 候选事件数）。
空候选零调用。
证券名称解析器由装配层注入（domain不依赖infrastructure）。

## 成本口径：**两个阶段都只用本地模型**（2026-09-26 改）

用户口径：

> 舆情情报 · 事件告警中心为什么会花费 tokens？信息是网络搜索获取的，
> 如果需要多空判断，可以用本地大模型。

审计证实了这个问题（`data/audit/llm_audit.jsonl`，53 次调用 / 0.32 元）：

| 阶段 | 层级 | 实际用了什么 | 花费 |
|---|---|---|---|
| 阶段一 | `medium` | 24× Ollama qwen3:8b + 1× deepseek-flash（本地失败时兜底） | 0.0196 元 |
| 阶段二 | **`reasoning`** | **28× deepseek**（+4× 本地） | **0.30 元** |

根因是阶段二写死了 `"reasoning"` —— 而 `configs/models.yaml` 里
`reasoning` 的 **primary 就是 deepseek-flash（付费）**，本机 Ollama 只是它的
fallback。也就是说这条链路是"先花钱，本地模型闲着"。

所以两处都改成**本地层 + `local_only=True`**：`medium` 的 primary 是
Ollama qwen3:8b，`local_only` 把降级链裁到只剩本地模型（`PAID_PROVIDERS`
之外），本地不可用时**明确失败**而不是悄悄去调云端。实测本地模型完全够用
（3 条真实事件，23 秒，852/992 token，0 元，输出 JSON 合法且分数有区分度：
政策利好→机会 60/把握 0.7 并给出受益标的，业绩暴雷→风险 85/把握 0.9，
海外花絮→双低分 15/把握 0.3）。

要重新允许付费云端必须**显式**打开 `MOSS_ALERT_ALLOW_CLOUD=1` ——
与 `src/domain/intel/llm_policy.py` 的 L0/L1/L2 口径一致（云端要过闸门，
而不是被 fallback 顺手带上去）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.domain.agents.analysis.base import parse_llm_json
from src.domain.alerts import prompts
from src.domain.alerts.models import (
    AffectedStock,
    Event,
    EventAssessment,
    EventType,
)
from src.infrastructure.llm import LLMGateway

logger = logging.getLogger(__name__)

AGENT_ID = "alert_analyzer"
_EVENT_TYPES = {t.value for t in EventType}
_SENTIMENTS = {"positive", "negative", "neutral"}
_IMPACTS = {"positive", "negative", "mixed"}
_MAX_STOCKS_PER_EVENT = 5
_RESOLVE_CONCURRENCY = 4

#: 两个阶段使用的任务层级。**都用本地层**（`medium` 的链尾 = `local_medium`，
#: 2026-09-28 起是 `qwen3.5:4b`，见 `configs/models.yaml` 的组合表）。
STAGE1_TIER: str = "medium"
STAGE2_TIER: str = "medium"

#: 阶段二每次调用处理的事件数上限。
#:
#: 为什么必须分批（实测 2026-09-26，本机本地模型）：
#:   · **输出预算**：`local_medium` 的 `max_tokens=4096`，而本地模型每条事件
#:     约 330 个输出 token（**该数含思考 token**，见下方 ⚠️）→ 一次塞 30 条
#:     必然截断，只能靠 `salvage_assessments` 抢救前缀，尾部事件**静默丢失评估**；
#:   · **超时**：本地实测 ~44 token/s，8 条约 2.6k 输出 ≈ 30~40 秒，
#:     而 `llm_timeout_seconds` 是 120 秒；30 条会直接撞超时 → 本轮无告警。
#: 分批的代价只是多几次调用 —— 本地模型**不按 token 计费**，换来的是
#: "每条事件都有评估"而不是"前十几条有、后面的没有"。
#:
#: ⚠️ **2026-09-28 第二十三轮：思维链已默认关闭（`think=false`），
#: 上面"含思考 token"的前提已不成立** —— 实测同一 prompt 下输出 token
#: 从 729 降到 321（−56%），单条耗时 18.3s → 9.1s。
#: 分批常数（`STAGE1_BATCH` / `STAGE2_BATCH`）**刻意保持不变**：
#:   · 方向是安全的（预算只用掉一半，不会有新的截断风险）；
#:   · 调大它属于**吞吐优化**，需要"每轮扫描耗时 / 告警条数 / 截断率"的
#:     生产实测支撑，不是本轮"消除掷硬币"目标的一部分（AGENTS.md：
#:     顺手优化是风险放大器）。
#: 待办：生产跑几轮后按实测重新标定这两个常数，并把依据写回这里。
STAGE2_BATCH: int = 8

#: 阶段一每次调用处理的事件数上限。**按实测输出开销算出来的**：
#:
#:     本地模型实测（**含思考 token**）：
#:       10 条短事件 → 1792 token 输出（179/条）
#:        生产真实事件（标题+正文 300 字+行业/公司实体）≈ 350~400 token/条
#:     预算：local_medium 的 max_tokens = 4096，其中约 600 花在思考上
#:       → 可用于正文 ≈ 3500 token → 3500 / 400 ≈ 8 条，取 6 留出余量
#:     时间：6 条 ≈ 2400 token ÷ 44 t/s ≈ 55 秒 < llm_timeout_seconds(120)
#:
#: ⚠️ 同上：`think=false` 之后"约 600 花在思考上"已变成 0，
#: 常数暂不动（安全侧），待生产实测再标定。
#:
#: 为什么必须分批（**生产日志实证**，2026-09-26）：
#:
#:     阶段一分类失败，使用本地兜底: alert_analyzer LLM输出非合法JSON:
#:     Unterminated string starting at: line 76 column 5 (char 1487)
#:
#: 140 条事件的一轮扫描里，阶段一一条调用要输出全部事件的
#: `{event_id,event_type,sentiment,entities,summary}`。本地模型是**思考型**
#: （实测约 560 个输出 token 花在思考上、正文只有 ~320 字符），事件一多就被
#: `max_tokens=4096` **从字符串中间截断** —— schema 只保证"结构合法"，
#: **不保证生成能跑完**。截断后整批退回关键词兜底（情感恒 neutral、
#: 摘要为空、行业/公司全丢）。分批把每批输出压回预算内。
STAGE1_BATCH: int = 6

#: 允许降级到**付费**云端模型的环境开关。默认关闭。
ALLOW_CLOUD_ENV: str = "MOSS_ALERT_ALLOW_CLOUD"

#: 阶段一的最大尝试次数（**本地模型，重试免费**）。
#:
#: 实测 qwen3:8b 三次里有一次返回 `{ }`（思考 token 花了 564 个、正文是空
#: 对象）→ 解析失败 → 整批退回关键词兜底（情感恒 neutral、摘要为空）。
#: 配上 `prompts.STAGE1_SCHEMA` 后结构已稳定，重试是第二道保险：
#: 本地调用不花 token 费，多试一次的代价只是十几秒。
STAGE1_ATTEMPTS: int = 2


def allow_cloud() -> bool:
    """是否允许用付费云端模型（默认 **False**：全本地，零 token 费用）。"""
    raw = os.environ.get(ALLOW_CLOUD_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


Resolver = Callable[[str], Awaitable[tuple[str, str] | None]]


class ScoreOutOfRange(ValueError):
    """LLM评分超出合法区间（FR-6：丢弃该事件评估，不做静默钳制）。"""


def _bounded(value: Any, lo: float, hi: float, default: float) -> float:
    """非数值/NaN→default；越界→ScoreOutOfRange（由调用方丢弃事件）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    if num < lo or num > hi:
        raise ScoreOutOfRange(f"score {num} out of [{lo}, {hi}]")
    return num


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:50] for v in value
            if v is not None and str(v).strip()][:10]


def _stage1_fallback(events: list[Event]) -> dict[str, dict[str, Any]]:
    """阶段一LLM不可用时的本地兜底：沿用normalizer的类型，情感中性。"""
    return {
        e.event_id: {
            "event_type": e.event_type.value, "sentiment": "neutral",
            "entities": e.entities.model_dump(), "summary": "",
        }
        for e in events
    }


def _salvage_objects(content: str, key: str) -> list[dict[str, Any]]:
    """从被 max_tokens 截断的输出里抢救 `key` 数组里**完整的对象**。

    `salvage_assessments` 的通用版（阶段一 / 阶段二共用）。逐项 `raw_decode`，
    遇到第一个不完整对象即停止 —— 剩下的保持未处理，下一轮重试。

    为什么两段都要它：schema 只约束"结构合法"，**不约束生成能跑完**；
    本地思考型模型的输出预算被思考吃掉一截，长批次必然截断。
    有了抢救，截断只是"这一批少几条"，而不是"整批退回关键词兜底"。
    """
    marker = content.find(key)
    start = content.find("[", marker) if marker >= 0 else -1
    if start < 0:
        return []
    decoder = json.JSONDecoder()
    pos, out = start + 1, []
    while pos < len(content):
        while pos < len(content) and content[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(content) or content[pos] == "]":
            break
        try:
            obj, end = decoder.raw_decode(content, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict) and obj.get("event_id"):
            out.append(obj)
        pos = end
    return out


def salvage_assessments(content: str) -> list[dict[str, Any]]:
    """从可能被max_tokens截断的输出中抢救assessments数组里完整的对象。

    reasoning模型思维链挤占输出预算时尾部JSON易截断；逐项raw_decode，
    遇到第一个不完整对象即停止（其余事件保持未分析，下轮重试）。
    """
    return _salvage_objects(content, "assessments")


def salvage_events(content: str) -> list[dict[str, Any]]:
    """同上，抢救阶段一的 `events` 数组。"""
    return _salvage_objects(content, "events")


class EventAnalyzer:
    """事件风险/机会分析器（无状态：每次analyze独立）。"""

    def __init__(
        self, gateway: LLMGateway,
        resolver: Resolver | None = None,
    ) -> None:
        self._gateway = gateway
        # 解析器由composition root注入；未注入时仅保留名称不补代码（D11）
        self._resolve = resolver

    async def analyze(self, events: list[Event],
                      force: bool = False) -> list[EventAssessment]:
        """分析事件；`force=True` 时绕过 LLM 缓存（手动"强制重扫"用）。"""
        if not events:
            return []
        stage1 = await self._run_stage1(events, force=force)
        stage2, model_used = await self._run_stage2(events, stage1, force=force)
        assessments: list[EventAssessment] = []
        for event in events:
            scored = stage2.get(event.event_id)
            if not scored:
                continue
            info = stage1.get(event.event_id, {})
            stocks = await self._resolve_stocks(scored.get("affected_stocks"))
            try:
                assessment = self._build_assessment(
                    event, info, scored, stocks, model_used)
            except ScoreOutOfRange:
                # FR-6：越界评分丢弃该事件（不静默钳制饱和成告警）
                logger.info("事件%s评分越界，丢弃该评估", event.event_id)
                continue
            assessments.append(assessment)
        logger.info(
            "alert_analyzer: %d 候选 -> %d 条有效评估", len(events), len(assessments))
        return assessments

    def _build_assessment(
        self, event: Event, info: dict, scored: dict,
        stocks: list[AffectedStock], model_used: str,
    ) -> EventAssessment:
        etype_value = info.get("event_type", event.event_type.value)
        try:
            etype = EventType(etype_value) if etype_value in _EVENT_TYPES \
                else event.event_type
        except ValueError:
            etype = event.event_type
        entities = info.get("entities") or {}
        return EventAssessment(
            event_id=event.event_id,
            event_type=etype,
            sentiment=info.get("sentiment", "neutral")
            if info.get("sentiment") in _SENTIMENTS else "neutral",
            risk_score=_bounded(scored.get("risk_score"), 0, 100, 0.0),
            opportunity_score=_bounded(
                scored.get("opportunity_score"), 0, 100, 0.0),
            confidence=round(_bounded(scored.get("confidence"), 0, 1, 0.0), 2),
            affected_stocks=stocks,
            affected_industries=_str_list(entities.get("industries")),
            impact_path=str(scored.get("impact_path", ""))[:200],
            summary=str(info.get("summary", ""))[:200],
            model_used=model_used,
        )

    async def _run_stage1(
        self, events: list[Event], *, force: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """分类/情感/实体/摘要。**分批**（每批 `STAGE1_BATCH` 条）。

        ## 为什么带 `json_schema`、要重试、还要分批

        提示词里写了"只输出JSON"，但那对小模型只是**建议**（实测 qwen3:8b
        有一次返回 `{ }`）。传 schema 后 Ollama 走受约束解码，结构由采样器
        保证 —— 可它**只保证结构合法，不保证生成能跑完**：思考 token 先吃掉
        一部分预算，事件一多就被 `max_tokens` 从字符串中间截断
        （生产日志实证：`Unterminated string starting at: line 76`）。
        所以：schema 管结构、**分批**管预算、重试管抖动；
        三者都没有才退回 `_stage1_fallback`（情感恒 neutral、摘要为空，
        属于**质量下降但功能可用**，不是失败）。

        单批失败**只影响那一批**：其余批次的分类结果照常保留。
        """
        out: dict[str, dict[str, Any]] = {}
        chunks = [events[i:i + STAGE1_BATCH]
                  for i in range(0, len(events), STAGE1_BATCH)]
        for index, chunk in enumerate(chunks, 1):
            out.update(await self._classify_batch(
                chunk, force=force, tag=f"{index}/{len(chunks)}"))
        return out

    async def _classify_batch(
        self, events: list[Event], *, force: bool = False, tag: str = "",
    ) -> dict[str, dict[str, Any]]:
        last = ""
        for attempt in range(1, max(1, STAGE1_ATTEMPTS) + 1):
            resp = None
            try:
                resp = await self._gateway.complete(
                    STAGE1_TIER, prompts.STAGE1_SYSTEM,
                    prompts.build_stage1_prompt(events),
                    agent_id=AGENT_ID, json_mode=True,
                    json_schema=prompts.STAGE1_SCHEMA,
                    # 缓存**打开**（2026-09-21 改）。原来硬写 False，理由是"每次
                    # 扫描都该重判"；但 prompt 只由 event_id/标题/正文/来源拼成，
                    # 同一批事件两次调用逐字节相同（event_id 本身是内容的 sha1），
                    # 实测 47 次调用 0 命中、白花 20 万 token。
                    # 内容真的变了 → prompt 变 → 自然换 key；要强制重判传 force=True。
                    use_cache=not force,
                    # ★ 绝不降级到付费云端（见模块 docstring 的成本口径）。
                    local_only=not allow_cloud(),
                )
                data = parse_llm_json(AGENT_ID, resp.content)
                return self._parse_stage1(data.get("events"), events)
            except Exception as exc:  # noqa: BLE001 降级为本地分类，扫描不中断
                last = brief(exc, BRIEF_DEFAULT)
                # ★ 截断抢救：schema 保证结构合法，但**不保证生成跑得完**
                #   （思考 token 挤占预算 → 尾部截在字符串中间）。
                #   抢救出完整前缀，总比整批退回关键词兜底强。
                salvaged = salvage_events(resp.content) if resp is not None \
                    else []
                if salvaged:
                    logger.info("阶段一JSON截断（第%s批），抢救出%d/%d条分类",
                                tag, len(salvaged), len(events))
                    return self._parse_stage1(salvaged, events)
                if attempt < STAGE1_ATTEMPTS:
                    logger.info("阶段一第 %d 次失败（第%s批，重试）：%s",
                                attempt, tag, last)
        logger.warning("阶段一分类失败（第%s批，%d条），使用本地兜底: %s",
                       tag, len(events), last)
        return _stage1_fallback(events)

    def _parse_stage1(
        self, raw: Any, events: list[Event],
    ) -> dict[str, dict[str, Any]]:
        valid_ids = {e.event_id for e in events}
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, list):
            return _stage1_fallback(events)
        for item in raw:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id", ""))
            if event_id not in valid_ids:
                continue
            etype = item.get("event_type", "")
            out[event_id] = {
                "event_type": etype if etype in _EVENT_TYPES else "",
                "sentiment": item.get("sentiment", "neutral"),
                "entities": {
                    "industries": _str_list(
                        (item.get("entities") or {}).get("industries")),
                    "companies": _str_list(
                        (item.get("entities") or {}).get("companies")),
                    "regions": _str_list(
                        (item.get("entities") or {}).get("regions")),
                },
                "summary": str(item.get("summary", ""))[:200],
            }
        # 未覆盖的事件用本地兜底补齐
        for event in events:
            out.setdefault(event.event_id, _stage1_fallback([event])[event.event_id])
        return out

    async def _run_stage2(
        self, events: list[Event], stage1: dict[str, dict[str, Any]],
        *, force: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], str]:
        """分批评分（每批 `STAGE2_BATCH` 条），合并结果。

        **单批失败只丢那一批**（原来一条链路失败就整轮没有评估）：
        本地模型偶发超时/坏 JSON 时，其余批次的事件仍然能出告警。
        """
        out: dict[str, dict[str, Any]] = {}
        model_used = ""
        chunks = [events[i:i + STAGE2_BATCH]
                  for i in range(0, len(events), STAGE2_BATCH)]
        for index, chunk in enumerate(chunks, 1):
            batch, model = await self._score_batch(
                chunk, stage1, force=force,
                tag=f"{index}/{len(chunks)}")
            out.update(batch)
            model_used = model or model_used
        return out, model_used

    async def _score_batch(
        self, events: list[Event], stage1: dict[str, dict[str, Any]], *,
        force: bool = False, tag: str = "",
    ) -> tuple[dict[str, dict[str, Any]], str]:
        try:
            resp = await self._gateway.complete(
                STAGE2_TIER, prompts.STAGE2_SYSTEM,
                prompts.build_stage2_prompt(events, stage1),
                agent_id=AGENT_ID, json_mode=True,
                json_schema=prompts.STAGE2_SCHEMA,
                use_cache=not force,
                # ★ 绝不降级到付费云端（见模块 docstring 的成本口径）。
                local_only=not allow_cloud(),
            )
        except Exception as exc:  # noqa: BLE001 打分层不可用→该批无评估
            logger.warning("阶段二评估失败（第%s批，%d条），该批不产生评估: %s",
                           tag, len(events), brief(exc, BRIEF_DEFAULT))
            return {}, ""
        try:
            data = parse_llm_json(AGENT_ID, resp.content)
        except Exception:  # noqa: BLE001 截断/非法JSON→抢救完整前缀
            salvaged = salvage_assessments(resp.content)
            if salvaged:
                logger.info("阶段二JSON截断（第%s批），抢救出%d/%d条评估",
                            tag, len(salvaged), len(events))
                return self._parse_stage2(salvaged), resp.model_used
            logger.warning("阶段二输出非法JSON且无可抢救内容（第%s批）", tag)
            return {}, resp.model_used
        return self._parse_stage2(data.get("assessments")), resp.model_used

    def _parse_stage2(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, list):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict) or not item.get("event_id"):
                continue
            out[str(item["event_id"])] = item
        return out

    async def _resolve_stocks(self, raw: Any) -> list[AffectedStock]:
        if not isinstance(raw, list):
            return []
        semaphore = asyncio.Semaphore(_RESOLVE_CONCURRENCY)

        async def _one(item: Any) -> AffectedStock | None:
            if not isinstance(item, dict):
                return None
            name = str(item.get("name", "")).strip()
            if not name:
                return None
            impact = item.get("impact", "mixed")
            stock = AffectedStock(
                code="", name=name[:20],
                impact=impact if impact in _IMPACTS else "mixed",
                reason=str(item.get("reason", ""))[:100])
            async with semaphore:
                if self._resolve is None:
                    return stock
                try:
                    resolved = await self._resolve(name)
                except Exception:  # noqa: BLE001 名称解析尽力而为
                    resolved = None
            if resolved:
                stock.code = resolved[0]
            return stock

        results = await asyncio.gather(
            *[_one(it) for it in raw[:_MAX_STOCKS_PER_EVENT]])
        return [s for s in results if s is not None]
