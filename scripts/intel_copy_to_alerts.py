"""试验：把**当前情报流条目**强制复制成事件告警（一次性，可一键回滚）。

    python scripts/intel_copy_to_alerts.py --dry-run          # 只看会发生什么
    python scripts/intel_copy_to_alerts.py                    # 真写（默认 pilot）
    python scripts/intel_copy_to_alerts.py --target dev
    python scripts/intel_copy_to_alerts.py --purge            # 一键删干净

## 这是**试验**，不是生产链路

用户口径（看「情报中心 → 热点&研报小作文」之后）：

> "其数据复制一份到事件告警，我先看下效果，不行就删除实现。"

所以这里**刻意绕开**生产链路 `AlertScanService.ingest_assessed()`：那条路
`_dispatch()` 会做 WebSocket 广播，而且邮件那条 `await self._emailer.send(alert)`
在 `quiet`（非交易时段静音）分支**之外**照发 —— 一次试验写入会变成
"用户收到一批真邮件"。本脚本只做三件事，零推送、零邮件：

    repo.upsert_events(events) → engine.evaluate(event, assessment)
                               → repo.upsert_alerts(alerts)

## 为什么默认写 **pilot**（`data/pilot/moss_pilot.db`）

用户在 `moss.wujiaitool.cn` 上看的就是 pilot 实例 —— "我先看下效果"要看的
是**那个页面**。写进 dev 库他永远看不到，写进生产库则越过了"试验"的边界。
`--target dev` 是给"先在本地看一眼"用的。

## 环境变量必须在导入 `src.core.config` **之前**设置

`Settings.sqlite_path` 用 `_env_field("MOSS_SQLITE_PATH", …)` 声明，即
`validation_alias=AliasChoices("MOSS_SQLITE_PATH", …)`。pydantic-settings 在
**实例化 `Settings()`** 时按 alias 去环境里取值，而不是像
`os.environ.get(...)` 当默认值那样在**模块导入时**求值。
结论（已读 `src/core/config.py` 的 `_env_field` 与 `@lru_cache get_settings`
核对过）：先设 env、再调 `get_settings()` 就一定能生效；本文件的 `src.*`
导入**全部放在函数内**、`os.environ` 在 `_main()` 第一行就写好，
两道保险都不依赖"谁先 import"这种脆弱的顺序。

## 已跑过的服务实例上小批量写入安全吗

安全。仓储的写连接显式设了 `busy_timeout`（20s）并在 `retry_on_locked`
里对 locked/busy 做 3 次线性退避重试 —— 本脚本**不自己写 sleep 重试**，
直接用这套既有锁等待。单批 60 条是毫秒级写入，撞上主库写事务最多多等一会儿。
（若将来换成没有 `retry_on_locked` 的仓储实现，则建议在低峰期跑。）

## 前缀纪律（`--purge` 就是靠它工作的）

本脚本写进去的**每一个** `event_key` / `content_key` 都以 `intelcopy:` 开头，
`alert_key` 由 `dedup.alert_dedup_key(event_key, type)` 推导因此也带前缀。
`content_key` 也换名是刻意的：真实告警的跨源同文冷却表按 `content_key`
查询，沿用情报流的 `content_key` 会让这批试验数据**抑制真实告警**。
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import hashlib
import os
import sys
from typing import Any

# 直接 `python scripts/xxx.py` 时 sys.path[0] 是 `scripts/`，仓库根不在上面
# （本项目不以包方式安装，`package = false`）。补上这一句，`src.*` 才解析得到。
sys.path.insert(0, ".")

# ⚠️ 这里 import `alert_bridge` 是**纯领域层**（无 IO、无配置实例化），
# 不会抢先读 `MOSS_SQLITE_PATH` —— 决定库路径的 `Settings()` 只在
# `_main()` 里经 `get_settings()` 构造，而那时 env 早已设好（见 docstring 的
# "环境变量必须在导入 src.core.config 之前设置"一节）。
from src.domain.intel import alert_bridge  # noqa: E402

PREFIX = "intelcopy:"

#: `--target` → 目标库路径。pilot 是用户在看的那个实例。
TARGETS = {
    "dev": "data/moss_finagent.db",
    "pilot": "data/pilot/moss_pilot.db",
}

#: 默认目标：pilot（理由见模块 docstring）。
DEFAULT_TARGET = "pilot"

#: 未定/中性条目在 `impact_path` 里的标注。
#:
#: 为什么必须写出来：`--include-undetermined` 造出来的告警与"有明确方向"的
#: 告警在界面上长得一模一样，而它们的可信度完全不同（一个是我们不知道方向、
#: 只是"复制一份给你看"，另一个是模型判了方向）。用户在列表里必须能一眼
#: 分清，否则他会以为"系统现在连没方向的消息都当信号了"。
UNDETERMINED_FLAG = "未定/中性，仅试验展示（无方向判断，勿当信号）"


def _hash_of(item: dict[str, Any]) -> str:
    """条目的稳定指纹（`content_hash` 缺失时才现算，与桥同一套降级）。"""
    h = str(item.get("content_hash") or "").strip()
    if h:
        return h
    raw = f"{item.get('title')}|{item.get('published_at')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dir_of(item: dict[str, Any]) -> str:
    """`positive` / `negative` / `""`（未定）。

    方向取自 `tone.tone`（用户口径里的"偏多→positive / 偏空→negative"），
    **不猜**：`中性` 与 `未定` 都返回空串，由调用方决定收不收。
    """
    tone = item.get("tone") or {}
    if not isinstance(tone, dict):
        return ""
    value = str(tone.get("tone") or "")
    if value == "偏多":
        return "positive"
    if value == "偏空":
        return "negative"
    return ""


def _score_of(item: dict[str, Any]) -> int:
    """条目可信度（0~100，取不到就是 0）。

    ⚠️ **不抬分**。桥对知识星球强制抬到 80（`alert_bridge.FORCED_SCORE`），
    理由是"这个来源的判据不看置信度"；试验副本没有那条用户口径，
    抬分等于伪造信号强度。分数不够（<70 时引擎的 `alert_confidence_min`
    会把它挡掉）就如实不出告警，并在输出里记一笔。
    """
    raw = item.get("credibility") or {}
    try:
        return max(0, min(100, int(raw.get("score"))))
    except (TypeError, ValueError, AttributeError):
        return 0


def _side_bucket(item: dict[str, Any], side: str) -> dict[str, Any]:
    """取方向桶（与 `alert_bridge._bucket_stocks` 同一套两种布局的兼容读法）。"""
    key = "bullish" if side == "positive" else "bearish"
    holder: Any = item
    if not isinstance(holder.get(key), dict):
        holder = item.get("tone") or {}
    bucket = holder.get(key) if isinstance(holder, dict) else None
    return bucket if isinstance(bucket, dict) else {}


def _industries_for(item: dict[str, Any], side: str) -> list[str]:
    """取**该方向桶**里的行业名（保序去重；未定时两侧都收）。

    只取方向桶、不取顶层 `industry`：当 `--include-undetermined` 打开时
    方向桶是空的，顶层 `industry` 会被当作"这条的行业"写进告警 ——
    那会让一条没有方向的条目看起来像"板块级信号"。
    """
    sides = ("positive", "negative") if not side else (side,)
    out: list[str] = []
    for one in sides:
        for raw in (_side_bucket(item, one).get("industries") or []):
            name = str(raw or "").strip()
            if name and name not in out:
                out.append(name)
    return out


def _people_of(item: dict[str, Any]) -> list[str]:
    """命中的分析师/机构名（**逐字来自条目已有的字段**，不再扫一遍原文）。

    `service.build_feed` 已经把 `institutions` / `analysts` 挂在条目上
    （`alert_rules` 的确定性规则层算的）。这里只读不算：再扫一遍等于
    多一份判据，而两份判据迟早会漂。
    """
    out: list[str] = []
    for key in ("analysts", "institutions"):
        for raw in (item.get(key) or []):
            name = str(raw or "").strip()
            if name and name not in out:
                out.append(name)
    return out


def _impact_path_of(item: dict[str, Any], side: str) -> str:
    """`impact_path` = **触发依据**，逐字拼接输入里已有的东西。

    ⚠️ 这里**不编造因果链**。桥的 `impact_path` 装的也是"触发依据"
    （`reason.describe()`）而不是模型因果链 —— 这是既有先例，照用。
    但本函数比桥更严格：桥只在**有理由**时才写，本函数没有理由可写
    （`reason` 不 fire 才会走到这里），所以只把"原文里真有的东西"
    摆出来：`tone.explain`（判定说明）+ `tone.phrases`（逐字命中词）+
    条目上已识别的人名/机构名。用户拿去原文一定能核对到。
    """
    tone = item.get("tone") or {}
    parts: list[str] = []
    if not side:
        parts.append(UNDETERMINED_FLAG)
    explain = str(tone.get("explain") or "").strip() if isinstance(tone, dict) else ""
    if explain:
        parts.append(f"倾向判定：{explain}")
    phrases = [
        str(p).strip()
        for p in ((tone.get("phrases") or []) if isinstance(tone, dict) else [])
        if str(p).strip()
    ]
    if phrases:
        parts.append("命中词：" + "、".join(phrases))
    people = _people_of(item)
    if people:
        parts.append("命中的人/机构：" + "、".join(people))
    return "；".join(parts)


def _summary_of(item: dict[str, Any], stocks: list[Any], impact_path: str) -> str:
    """告警正文：理由在前 + 原始摘要（与桥的正文纪律一致）。

    无标的必须明说（`alert_bridge.NO_STOCK_FLAG`）：一条"利多"却不点名
    个股的告警，与一条点名个股的告警不是一个量级的东西，显示成同一个
    样子会让用户以为"系统找到了票"。
    """
    head = impact_path
    if not stocks:
        head = (f"{head}；{alert_bridge.NO_STOCK_FLAG}" if head
                else alert_bridge.NO_STOCK_FLAG)
    body = str(item.get("summary") or "")
    return f"{head}。{body}" if head else body


def _force_assessment(item: dict[str, Any], *, include_undetermined: bool) -> Any:
    """**试验专用**的本地评估构造（明确不参与生产）。

    ## 为什么不能拿它去改生产闸门

    生产闸门（`alert_bridge.is_alertable` / `build_assessment`）是**用户
    逐条确认过的口径**的落点："明确方向 + 信度达标 + 仅 high 级"，
    知识星球另按"命中分析师名或模型给方向"旁路。那套口径的每一次放宽都
    有用户的原话背书（见 `alert_bridge` 的模块 docstring）。

    本函数恰恰相反 —— 它的目的是**无视闸门**，把"当前情报流里有什么"
    原样复制一份给用户看效果。把这里的任何一条（比如"未定也归机会侧"）
    挪进生产闸门，等于在没有用户确认的情况下把"没有方向的消息"变成
    弹窗，那正是闸门要挡的东西。所以它只存在于这个一次性脚本里，
    名字里带 `_force_`，且**不进 `alert_bridge`**。

    ## 方向 / 分数映射（`--include-undetermined` 关闭时未定条目直接跳过）

        tone       告警方向      分数                   置信度
        偏多       opportunity  credibility.score      score / 100
        偏空       risk         credibility.score      score / 100
        未定/中性  opportunity  credibility.score      score / 100
                   （仅试验展示，`impact_path` 里写明；不伪造高分）
    """
    side = _dir_of(item)
    if not side and not include_undetermined:
        return None
    score = float(_score_of(item))
    positive = side != "negative"
    stocks = alert_bridge.stock_entries(item)
    impact_path = _impact_path_of(item, side)
    return alert_bridge.EventAssessment(
        event_id=f"intelcopy_{_hash_of(item)}",
        event_type=alert_bridge.build_event(item).event_type,
        sentiment="positive" if positive else "negative",
        risk_score=0.0 if positive else score,
        opportunity_score=score if positive else 0.0,
        # 置信度 = 可信度/100，与桥同一个代理（"我们有多确定这条判断"）。
        # 分数低就置信度低 —— 引擎的 alert_confidence_min 会如实挡掉它。
        confidence=min(1.0, score / 100.0),
        affected_stocks=stocks,
        affected_industries=_industries_for(item, side),
        impact_path=impact_path,
        summary=_summary_of(item, stocks, impact_path),
        model_used="intelcopy_trial",
    )


def _force_pair(item: dict[str, Any], *, include_undetermined: bool) -> Any:
    """`(Event, EventAssessment)`；不该收的条目返回 None。

    两条路：
      ① 桥能出评估（闸门过了）→ 用**桥的**事件与评估，只把
         `event_key` / `content_key` / `event_id` 换成 `intelcopy:` 前缀；
      ② 桥返回 None（绝大多数条目）→ 用 `_force_assessment` 本地造，
         但事件骨架仍走桥的 `build_event`（`event_type` 的"个股优先"
         降级映射与生产一致），再改前缀。
    """
    reason = alert_bridge.reason_of(item)
    assessment = alert_bridge.build_assessment(item, reason)
    if assessment is None:
        assessment = _force_assessment(
            item, include_undetermined=include_undetermined)
    if assessment is None:
        return None

    event = alert_bridge.build_event(item)
    h = _hash_of(item)
    # `content_key` **必须**跟着换名：真实告警的跨源同文冷却按它查
    # （`repo.last_content_alert_time`），沿用原值会让试验数据抑制真实告警。
    event.event_key = f"{PREFIX}{h}"
    event.event_id = f"intelcopy_{h}"
    event.content_key = f"{PREFIX}{event.content_key or h}"
    assessment.event_id = event.event_id
    return event, assessment


def _skip_reason(item: dict[str, Any], *, include_undetermined: bool) -> str:
    """这条为什么没进结果（**只用于输出**，不参与任何判定）。"""
    if not _dir_of(item) and not include_undetermined:
        return "未定/中性（未开 --include-undetermined）"
    return "其它"


def _level_of(engine: Any, event: Any, assessment: Any) -> str:
    """dry-run 里显示引擎会判成什么档（没有 engine 就显示 `-`）。"""
    if engine is None:
        return "-"
    alert = engine.evaluate(event, assessment)
    if alert is None:
        return "不出告警"
    return f"{alert.alert_type.value}/{alert.alert_level.value}"


def _footer(say: Any) -> None:
    """每条通路都必须打印的提醒 —— 这是"怎么删"的唯一提示。"""
    say(f"⚠️  这批数据带 {PREFIX!r} 前缀；一条命令删干净："
        f"python scripts/intel_copy_to_alerts.py --purge")
    say("⚠️  试验数据**不进**生产告警链路（无推送、无邮件）。")


async def run(
    *, target: str = DEFAULT_TARGET, limit: int = 60, dry_run: bool = False,
    include_undetermined: bool = False, purge: bool = False,
    feed_items: list[dict[str, Any]] | None = None, repo: Any = None,
    engine: Any = None, out: Any = None,
) -> dict[str, Any]:
    """跑一轮（或只清理）。返回一份"发生了什么"的汇总 dict。

    `feed_items` / `repo` / `engine` 可注入：单测据此避开真实情报源聚合
    （六源网络请求）与真实库装配，只验本脚本自己的映射与写入语义。
    `out` 可注入（`io.StringIO`）以便断言输出。
    """
    stream = out or sys.stdout

    def say(text: str) -> None:
        builtins.print(text, file=stream)

    if purge:
        if repo is None:
            raise RuntimeError("purge 需要 repo")
        removed = await repo.delete_alerts(PREFIX)
        say(f"[purge] 删除前缀 {PREFIX!r} 的告警 {removed['alerts']} 条、"
            f"事件 {removed['events']} 条")
        if removed["alerts"] == 0 and removed["events"] == 0:
            say("        （库里本来就没有这批试验数据）")
        return {"purge": removed}

    if feed_items is None:
        # 惰性导入：`build_feed` 会拉起六源连接器；dry-run 之外的路径才需要它。
        # 这里（而不是模块顶层）导入，也让 `_main` 的 env 一定先落好。
        from src.domain.intel.service import build_feed

        feed = await build_feed(limit=500, group_undetermined=False)
        feed_items = list(feed.items)

    window = feed_items[:max(0, int(limit))]
    say(f"[feed] 情报流平铺池 {len(feed_items)} 条（目标库 {target}）")

    pairs: list[tuple[Any, Any]] = []
    skipped: dict[str, int] = {}
    by_side = {"positive": 0, "negative": 0, "undetermined": 0}
    for item in window:
        by_side[_dir_of(item) or "undetermined"] += 1
        pair = _force_pair(item, include_undetermined=include_undetermined)
        if pair is None:
            key = _skip_reason(item, include_undetermined=include_undetermined)
            skipped[key] = skipped.get(key, 0) + 1
            continue
        pairs.append(pair)

    beyond = len(feed_items) - len(window)
    say(f"[候选] 参与复制 {len(pairs)} 条；方向分布（本窗口 {len(window)} 条）"
        f"偏多 {by_side['positive']} / 偏空 {by_side['negative']} / "
        f"未定·中性 {by_side['undetermined']}；跳过 {skipped or '无'}"
        + (f"；窗口外未计 {beyond} 条（--limit 可调）" if beyond else ""))

    if dry_run:
        for event, assessment in pairs:
            say(f"  [dry-run] {event.event_key} type={event.event_type.value} "
                f"sent={assessment.sentiment} risk={assessment.risk_score:.0f} "
                f"opp={assessment.opportunity_score:.0f} "
                f"conf={assessment.confidence:.2f} "
                f"level={_level_of(engine, event, assessment)} "
                f"| {str(event.title)[:36]}")
        say("[dry-run] **没有写库**（一行都没写）")
        _footer(say)
        return {"dry_run": True, "candidates": len(pairs), "by_side": by_side,
                "skipped": skipped}

    if repo is None or engine is None:
        raise RuntimeError("写入需要 repo 与 engine")

    events = [e for e, _ in pairs]
    by_id = {e.event_id: (e, a) for e, a in pairs}
    known = await repo.existing_event_keys([e.event_key for e in events])
    fresh = [e for e in events if e.event_key not in known]
    saved = await repo.upsert_events(events)

    alerts = []
    no_alert = 0
    for event in fresh:
        _, assessment = by_id[event.event_id]
        alert = engine.evaluate(event, assessment)
        if alert is None:
            # 引擎挡掉的（置信度 < alert_confidence_min，或分数没到 medium 档）
            no_alert += 1
            continue
        alerts.append(alert)
    written = await repo.upsert_alerts(alerts) if alerts else {
        "inserted": 0, "skipped": 0}

    say(f"[写入] 事件 {saved['inserted']} 新增 / {saved['skipped']} 已存在；"
        f"新事件 {len(fresh)} 条里引擎产出告警 {len(alerts)} 条"
        f"（入库 {written['inserted']}），另 {no_alert} 条被引擎门槛挡下")
    for alert in alerts:
        say(f"  [{alert.alert_type.value}/{alert.alert_level.value}] "
            f"{alert.alert_key} | {str(alert.title)[:36]}")
    _footer(say)
    return {"events": saved, "alerts": written, "candidates": len(pairs),
            "engine_skipped": no_alert, "by_side": by_side, "skipped": skipped}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把当前情报流条目强制复制成事件告警（试验，可一键回滚）")
    parser.add_argument("--limit", type=int, default=60,
                        help="最多复制多少条（默认 60）")
    parser.add_argument("--purge", action="store_true",
                        help=f"只清理 {PREFIX!r} 前缀的告警与事件，然后退出")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将写入什么，不写库")
    parser.add_argument("--include-undetermined", action="store_true",
                        help="包含未定/中性条目（默认不包含：无方向的告警"
                             "没有意义）")
    parser.add_argument("--target", choices=sorted(TARGETS),
                        default=DEFAULT_TARGET,
                        help=f"目标库（默认 {DEFAULT_TARGET}：用户在看的试点实例）")
    args = parser.parse_args(argv)
    if args.purge and (args.dry_run or args.include_undetermined):
        parser.error("--purge 与写入开关互斥：清理时 --dry-run / "
                     "--include-undetermined 都没有意义")
    return args


async def _main(args: argparse.Namespace) -> int:
    # ★ 必须在任何 `src.*` 导入**之前**落 env（理由见模块 docstring）。
    rel = TARGETS[args.target]
    os.environ["MOSS_SQLITE_PATH"] = rel
    sys.path.insert(0, ".")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from src.core.config import get_settings
    from src.domain.alerts.thresholds import AlertEngine
    from src.infrastructure.repositories.repository_factory import (
        build_event_repository,
    )

    settings = get_settings()
    resolved = settings.sqlite_path
    builtins.print(f"[配置] MOSS_SQLITE_PATH={os.environ['MOSS_SQLITE_PATH']!r} "
                   f"→ Settings.sqlite_path={resolved!r}"
                   f"（--target {args.target}）", flush=True)
    if args.target == "dev" and os.path.abspath(resolved) != os.path.abspath(
            TARGETS["dev"]):
        # 只在"想写 dev、实际解析到别处"时告警：写错库是这里最贵的错。
        builtins.print(f"[配置] ⚠️ --target dev 但解析到的库是 {resolved!r}，"
                       "请核对环境变量", flush=True)
    repo = build_event_repository(settings)

    await run(target=args.target, limit=args.limit, dry_run=args.dry_run,
              include_undetermined=args.include_undetermined,
              purge=args.purge, repo=repo, engine=AlertEngine(settings))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
