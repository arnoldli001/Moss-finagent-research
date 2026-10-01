"""换源（reroute）：**联网兜底也找不到**时，自动在已有源里找替代并**记住它**。

## 用户口径（2026-09-30）

> 「源停更 → 换源，这个可以在**每次联网找不到数据时**，做个**自动搜索其他网址**，
>   寻找数据源，**找到后就更新数据源地址**」

## ★ 先说清能力边界（照实登记，不假装能做）

~~本仓库**没有任何搜索引擎能力**~~ —— **这句话在 2026-09-30 之后不再成立**
（`CHG-0113`：用户提供博查 Web Search 凭据）。三条路径的现状：

| 路径 | 花钱？ | 现在有吗 | 覆盖面 |
|---|---|---|---|
| **A 在已有连接器里找替代**（`supports()` 枚举 + 真取一次） | **免费** | ✅ **本模块实现** | 24 个连接器覆盖的族 |
| **B 用 LLM 提议新 URL/接口**（`DataGapResolverAgent` 生成连接器代码） | 花钱（reasoning 层） | ✅ 已有（A19） | 长尾族 |
| **C 真·搜索引擎找网址** | 花钱（**免费包 1000 次总量**，闸门写在代码里） | ✅ **2026-09-30 起有** —— `infrastructure/search/bocha.py` | 最广 |

⇒ 本模块做 **A**（免费、确定性、可离线单测）；**C 由 `discover_candidate_urls()`
提供候选网址线索**；B/C 产出的候选一律走**同一套**口径校验与落盘纪律。

⚠️ **C 的能力边界（照实说，别夸大）**：搜索引擎只能给出**网址**，而
`CandidateSource` 要的是**能产点的连接器** —— 中间那一步"网址 → 可执行的取数"
**仍然是 B/A19 的活**。所以 C 现在的产物是"**待复核的候选源线索**"（进换源审计），
**不是**"已经能用的源"，也**不自动改** `indicators.yaml`。
**接线纪律**：C 只允许**后台路径**调用（搜索有延迟且花钱），绝不挂交互路径。

## ★ 三条硬纪律（每条对应一个已付代价的缺陷）

1. **口径一致性校验**：新源必须与旧序列在**频率 / 量级 / 重叠期**上过得去 ——
   否则就是"**指数当同比**""**水平值当增量**"。本项目的教训是
   **错口径比缺数据更危险**（数字看着有据，语义是错的）；
2. **影子期**：**不立刻切主源**。先记 `shadow`，并行取若干期比对一致后才 `promoted`；
3. **落覆盖层，不自动改 `indicators.yaml`**：登记表是**人工复核**的单一真值源，
   自动改它会破坏"改口径必须留废止痕迹"的纪律 ⇒ 落**覆盖层**
   （`<登记的 run_dir>/source_overrides.yaml`），并在管理端给出**可直接并入 YAML 的一行**。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 覆盖层文件名（放在**登记的** `run_dir` 下 ⇒ 不写路径字面量）。
OVERRIDE_NAME = "source_overrides.yaml"
#: 换源决策审计（同样的目录，JSONL，便于 grep）。
DECISION_LOG_NAME = "source_reroute_log.jsonl"

#: 单次换源最多探测几个候选源（有上限 —— 每次探测都是真取一次，可能很慢）。
MAX_PROBE = 3
#: 单个候选源的探测超时（秒）。换源是**后台补救**，不该拖住交互路径。
PROBE_TIMEOUT_SEC = 8.0
#: 影子期需要的**比对期数**（重叠期少于这个数就不敢切，只能 shadow）。
SHADOW_MIN_OVERLAP = 3

# ---- 口径一致性阈值（都是**实测口径**，不是拍的）----

#: 频率一致：两条序列的**中位期间间隔**比值必须落在 [1/R, R]。
#: 3.0 足以区分"月频 vs 日频"（30 vs 1 ⇒ 比值 30，必拒），
#: 又不会把"月末 vs 月初"这种正常抖动判死。
FREQ_RATIO_MAX = 3.0
#: 量级一致：两条序列 `median(|value|)` 的比值必须落在 [1/R, R]。
#: 10.0 的依据：`fred:CPILFESL`（指数 337.8）vs `us_core_cpi`（同比 ~3）
#: 比值 ≈ 110 ≫ 10 ⇒ **必须拒**（这正是"指数当同比"的现场）；
#: 而同一口径的两次取数比值应当 ≈ 1。
MAGNITUDE_RATIO_MAX = 10.0
#: 重叠期一致：同一 `period_date` 上两源的相对差必须 ≤ 这个值。
OVERLAP_REL_TOL = 0.05
#: 影子期的默认时长（秒）：比对一致后**先 shadow**，人工或下一轮再提升。
SHADOW_SECONDS = 7 * 24 * 3600


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _period_key(p: Any) -> str:
    return str(getattr(p, "period_date", "") or "")


def _value_of(p: Any) -> float | None:
    v = getattr(p, "value", None)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _median_interval_days(points: list[Any]) -> float:
    """期间的中位间隔（天）。少于 2 个期间 ⇒ 0（未知）。"""
    periods = sorted({_period_key(p) for p in points if _period_key(p)})
    if len(periods) < 2:
        return 0.0
    from datetime import date

    def _d(s: str) -> date | None:
        try:
            y, m, d = (int(x) for x in s[:10].split("-"))
            return date(y, m, d)
        except (TypeError, ValueError):
            return None

    gaps = [(_d(b) - _d(a)).days for a, b in zip(periods, periods[1:])
            if _d(a) and _d(b)]
    gaps = [g for g in gaps if g > 0]
    return _median([float(g) for g in gaps])


def _values_by_period(points: list[Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in points:
        v = _value_of(p)
        k = _period_key(p)
        if v is not None and k:
            out[k] = v          # 同期间取最后一条（与原口径一致：不平均）
    return out


@dataclass
class CaliberVerdict:
    """口径一致性判定（**机器可读**，理由进审计与覆盖层）。"""

    ok: bool
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)

    def log_line(self) -> str:
        return f"ok={self.ok} reason={self.reason} {self.checks}"


def check_caliber(old: list[Any], new: list[Any]) -> CaliberVerdict:
    """新源能不能替代旧源（**口径一致性**，不是"有没有数据"）。

    判据（按顺序，任一条不过就拒 —— **宁可留着缺口，也不要错口径**）：

      ① 新源必须**有值**，且最新期**不早于**旧源（否则换它没有意义）；
      ② **重叠期**比对：同一 `period_date` 上相对差 ≤ `OVERLAP_REL_TOL`；
         重叠 ≥ `SHADOW_MIN_OVERLAP` 期时，这一条是**主判据**；
      ③ 重叠不足时退到**频率**（中位间隔比值 ≤ `FREQ_RATIO_MAX`）
         与**量级**（`median(|value|)` 比值 ≤ `MAGNITUDE_RATIO_MAX`）。
    """
    old_v, new_v = _values_by_period(old), _values_by_period(new)
    if not new_v:
        return CaliberVerdict(False, "新源没有有效值（空结果不算换源）")
    checks: dict[str, Any] = {"old_points": len(old_v), "new_points": len(new_v)}

    old_latest = max(old_v) if old_v else ""
    new_latest = max(new_v)
    checks["old_latest"] = old_latest or "-"
    checks["new_latest"] = new_latest
    if old_latest and new_latest < old_latest:
        return CaliberVerdict(
            False, f"新源最新期 {new_latest} 早于旧源 {old_latest}（换了也没前进）",
            checks)

    common = sorted(set(old_v) & set(new_v))
    checks["overlap_periods"] = len(common)
    if len(common) >= SHADOW_MIN_OVERLAP:
        worst, worst_at = 0.0, ""
        for k in common:
            a, b = old_v[k], new_v[k]
            denom = max(abs(a), abs(b), 1e-9)
            rel = abs(a - b) / denom
            if rel > worst:
                worst, worst_at = rel, k
        checks["overlap_max_rel"] = round(worst, 6)
        checks["overlap_worst_at"] = worst_at
        if worst > OVERLAP_REL_TOL:
            return CaliberVerdict(
                False,
                f"重叠期口径不一致：{worst_at} 上相对差 {worst:.1%} "
                f"> 容差 {OVERLAP_REL_TOL:.0%}（同期间两源应給同一个数）",
                checks)
        return CaliberVerdict(True, f"重叠 {len(common)} 期一致（最大相对差 "
                                    f"{worst:.2%}）", checks)

    # 重叠不足 ⇒ 退到频率 + 量级（**弱判据**，所以只给 shadow，不给 promoted）
    f_old = _median_interval_days(old)
    f_new = _median_interval_days(new)
    checks["median_interval_days"] = [f_old, f_new]
    if f_old > 0 and f_new > 0:
        ratio = max(f_old, f_new) / max(1e-9, min(f_old, f_new))
        checks["freq_ratio"] = round(ratio, 3)
        if ratio > FREQ_RATIO_MAX:
            return CaliberVerdict(
                False, f"频率不一致：中位间隔 {f_old:.0f} 天 vs {f_new:.0f} 天"
                       f"（比值 {ratio:.1f} > {FREQ_RATIO_MAX}）", checks)
    m_old = _median([abs(v) for v in old_v.values()])
    m_new = _median([abs(v) for v in new_v.values()])
    checks["median_abs"] = [round(m_old, 6), round(m_new, 6)]
    if m_old > 0 and m_new > 0:
        ratio = max(m_old, m_new) / max(1e-9, min(m_old, m_new))
        checks["magnitude_ratio"] = round(ratio, 3)
        if ratio > MAGNITUDE_RATIO_MAX:
            return CaliberVerdict(
                False, f"量级不一致：中位 {m_old:.4g} vs {m_new:.4g}"
                       f"（比值 {ratio:.1f} > {MAGNITUDE_RATIO_MAX}）"
                       " —— 典型是「指数当同比」/「水平值当增量」", checks)
    return CaliberVerdict(
        True, f"重叠只有 {len(common)} 期（< {SHADOW_MIN_OVERLAP}）⇒ "
              "只凭频率+量级通过，**只能进影子期**", checks)


@dataclass
class CandidateSource:
    """一个候选源（可能是"已有连接器"，也可能是 B/C 路径提议出来的）。"""

    source_key: str          # 连接器**类名**（与 NetworkFallback 的 source_key 同口径）
    points: list[Any] = field(default_factory=list)
    origin: str = "connector"   # connector / llm / search
    error: str = ""


def _override_path(root: Path | None = None) -> Path:
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    base = Path(root) if root is not None else PROJECT_ROOT
    return base / store_rel("run_dir") / OVERRIDE_NAME


def _log_path(root: Path | None = None) -> Path:
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    base = Path(root) if root is not None else PROJECT_ROOT
    return base / store_rel("run_dir") / DECISION_LOG_NAME


def load_overrides(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """读覆盖层（失败一律当空 —— 覆盖层是**增强**，坏了不该拖垮取数）。"""
    import yaml

    p = _override_path(root)
    try:
        if not p.exists():
            return {}
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out = raw.get("overrides") if isinstance(raw, dict) else None
        return dict(out) if isinstance(out, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("换源覆盖层读取失败（按空处理）: %s", exc)
        return {}


def preferred_order(indicator: str, root: Path | None = None) -> list[str]:
    """该指标的**取数偏好顺序**（主源 → 备源…）；没有则空列表。

    ★ 用户口径（2026-09-30）：「**也可以联网查询获取两个数据源，作为备用**」
    ⇒ 覆盖层存的不是一个源，而是**有序的源列表**：第 0 个是主源，其后是备源。
    路由按这个顺序取数（备源不删除、只往后排），于是"主源挂了"不会立刻变成缺口
    —— 这正是多源备份要买的东西。

    三条约束：
      * 只有 `promoted` 状态生效（`shadow` 是观察期，**不许**改取数顺序）；
      * 返回的源若不在实际匹配集合里，路由侧**跳过它继续往后**（不硬失败）；
      * 覆盖层读失败 ⇒ 空列表（增强坏了不该拖垮取数）。
    """
    item = load_overrides(root).get(str(indicator))
    if not isinstance(item, dict):
        return []
    if str(item.get("status") or "") != "promoted":
        return []
    srcs = item.get("sources")
    if isinstance(srcs, list) and srcs:
        return [str(s) for s in srcs if str(s).strip()]
    # 老格式（单源）兼容：`source_key`
    one = str(item.get("source_key") or "")
    return [one] if one else []


def preferred_source(indicator: str, root: Path | None = None) -> str:
    """该指标**当前主源**（连接器类名）；没有则空串（= `preferred_order` 的第 0 个）。"""
    order = preferred_order(indicator, root)
    return order[0] if order else ""


#: 覆盖层里最多保留几个备源（用户要两个备用 ⇒ 主源 + 2 备源）。
MAX_BACKUP_SOURCES = 2


def record_decision(indicator: str, verdict: CaliberVerdict, *,
                    source_key: str = "", origin: str = "",
                    status: str = "", root: Path | None = None,
                    backups: list[str] | None = None) -> None:
    """把一次换源决策写进审计（JSONL）+ 覆盖层（仅当通过）。**绝不抛**。

    `backups`：用户口径要的**备用源**（最多 `MAX_BACKUP_SOURCES` 个），
    与主源一起写进 `sources` 有序列表 ⇒ 路由按"主源 → 备源 → 其余"取数。
    """
    try:
        entry = {
            "ts": time.time(), "indicator": str(indicator),
            "source_key": source_key, "origin": origin, "status": status,
            "backups": list(backups or []),
            "ok": verdict.ok, "reason": verdict.reason, "checks": verdict.checks,
        }
        lp = _log_path(root)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("换源审计写入失败（忽略）: %s", exc)

    if not verdict.ok or not source_key:
        return
    try:
        import yaml

        overrides = load_overrides(root)
        #: 主源在前、备源在后（去重保序）；**上限**由 `MAX_BACKUP_SOURCES` 定住
        ordered = [source_key] + [b for b in (backups or []) if b != source_key]
        ordered = ordered[: 1 + MAX_BACKUP_SOURCES]
        overrides[str(indicator)] = {
            "source_key": source_key,
            "sources": ordered,
            "status": status or "promoted",
            "origin": origin or "connector",
            "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": verdict.reason,
            "checks": verdict.checks,
            #: 给人看的一行：可直接并入 `configs/indicators.yaml`（纪律 3）
            "yaml_hint": (f"primary_source: \"{source_key}\""
                          + (f" | backup: {', '.join(ordered[1:])}"
                             if len(ordered) > 1 else "")
                          + f"  # 由换源流水线于 {time.strftime('%Y-%m-%d')} 找到："
                            f"{verdict.reason}"),
        }
        p = _override_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump({"version": 1, "overrides": overrides},
                                    allow_unicode=True, sort_keys=True),
                     encoding="utf-8")
        logger.info("换源登记：%s → 主 %s / 备 %s（status=%s，%s）", indicator,
                    source_key, ordered[1:] or "无", status or "promoted",
                    verdict.reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("换源覆盖层写入失败（忽略）: %s", exc)


async def probe_connectors(router: Any, indicator: str, *, skip: Any = None,
                           limit: int = MAX_PROBE,
                           timeout: float = PROBE_TIMEOUT_SEC,
                           ) -> list[CandidateSource]:
    """**路径 A**：在已有连接器里找替代源（免费、确定性）。

    `skip`：`(connector) -> bool`，跳过"已经试过/在冷却里"的源
    （不重复付一次必然失败的延迟）。
    """
    out: list[CandidateSource] = []
    for route in getattr(router, "_routes", []) or []:
        if len(out) >= max(1, int(limit)):
            break
        # `_routes` 的元素是 `(connector, supports)` 对（生产的构造形态）；
        # 兼容"只给连接器对象"的测试替身。
        if isinstance(route, tuple):
            conn, predicate = route[0], route[1]
        else:
            conn, predicate = route, getattr(route, "supports", None)
        key = type(conn).__name__
        if skip is not None:
            try:
                if skip(conn):
                    continue
            except Exception:  # noqa: BLE001
                pass
        # ⚠️ 必须调**实例/路由持有的那个判据**，不能写 `type(conn).supports(...)`
        #   —— 生产的 `supports` 是 `@staticmethod`（在类上调用没问题），
        #   但"实例方法"形态的实现在类上调用会把 `indicator` 当成 `self`
        #   ⇒ 判据静默失真（本轮实测被自己的替身抓出来）。
        try:
            if callable(predicate):
                if not predicate(indicator):
                    continue
            elif not conn.supports(indicator):  # type: ignore[attr-defined]
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            points = await asyncio.wait_for(conn.fetch(indicator), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 单源失败不阻断其它候选
            out.append(CandidateSource(key, [], origin="connector",
                                       error=f"{type(exc).__name__}: {exc}"[:160]))
            continue
        out.append(CandidateSource(key, list(points or []), origin="connector"))
    return out


def _append_search_lead(indicator: str, hits: list[dict[str, str]],
                        note: str, root: Path | None = None) -> None:
    """把 path C 搜到的候选网址写进**同一份换源审计**（`kind` 区分来源）。

    为什么与 `record_decision` 分开：那份记录的是**口径判定**（有 `verdict`），
    而这里记的是**线索**（还没有任何取数、谈不上口径）。
    混在一起会让"判过口径的源"与"只是个网址"长得一样 —— 本项目最忌讳这个。
    写失败**一律吞掉**：换源是补救路径，审计写不进去不该把它拖垮。
    """
    try:
        p = _log_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "kind": "search_candidates",
                "indicator": indicator, "note": note, "hits": hits,
                #: ⚠️ 明确标注"这只是线索" —— 消费方（人/A19）不要当成可用源
                "actionable": False,
                "next_step": "网址 → 连接器（A19 的活）；或人工确认后登记",
            }, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("换源线索写入失败（不影响主流程）: %s", exc)


def _record_anomaly(indicator: str, outcome: Any, hits: list[dict[str, str]],
                    root: Path | None = None) -> None:
    """把这次搜索的结果**分门别类**记进采集异常库（给管理员界面看）。

    ## 为什么分两类而不是一类（`CHG-0120`）

    | 情况 | kind | 它是什么 |
    |---|---|---|
    | 搜索源自己被拒（`budget`/`no_key`/`config_error`/`http`/`transport`/`parse`/`all_failed`） | `search_source` | **运维故障** —— 额度用尽、凭据没配、连不上。**不是"某个指标取不到"** |
    | 搜到了候选网址 | `source_lead` | **不是故障** —— 是"待人工复核的候选源" |

    混进 `gap`（采集缺口）会让排查方向跑偏：`gap` 会被读成
    "这条指标的数据源没给数据"，而真相可能是"**找数据源这条路断了**"。
    ⚠️ `collection_anomalies.record()` 对未知 kind **静默改写成 `gap`**
    （已加告警），所以这里的两个常量必须**先登记进 `KINDS`**。

    本函数**绝不抛异常**：观测坏掉不许拖垮换源。
    """
    try:
        from src.core import collection_anomalies as CA

        blocked = str(getattr(outcome, "blocked_by", "") or "")
        if not getattr(outcome, "ok", False):
            CA.record(
                CA.KIND_SEARCH_SOURCE, indicator or "(未知指标)",
                f"联网换源搜索不可用（blocked_by={blocked or 'unknown'}）："
                f"{getattr(outcome, 'reason', '')}"[:280],
                source=str(getattr(outcome, "served_by", "") or ""),
                extra={"blocked_by": blocked,
                       "spent": bool(getattr(outcome, "spent", False))},
                root=root)
            return
        if hits:
            top = hits[0].get("url", "")
            CA.record(
                CA.KIND_SOURCE_LEAD, indicator or "(未知指标)",
                f"无连接器支持，联网找到 {len(hits)} 条候选源网址（待人工/A19 复核）；"
                f"首条：{top}"[:280],
                source=str(getattr(outcome, "served_by", "") or ""),
                extra={"count": len(hits), "top_url": top,
                       "urls": [h.get("url", "") for h in hits[:10]]},
                root=root)
    except Exception as exc:  # noqa: BLE001 观测坏掉不许影响换源
        logger.debug("换源观测记录失败（忽略）: %s", exc)


def _enqueue_lead_for_a19(indicator: str, hits: list[dict[str, str]]) -> bool:
    """把候选源网址喂进**既有**的 A19 管道（`CHG-0127`）。返回是否新入队。

    ## 为什么是"喂进既有管道"而不是"另造一条"

    `AGENTS.md`：**只调用既有入口，不另造一套**。本轮先确认了既有管道**确实存在且真跑过**：

      · `data/dynamic_connectors/` 里有 **5 个 `gap_*.py`**（A19 生成的连接器），
        其中两个的 mtime 是 **2026-09-29 22:00:32 / 22:02:40** —— **正好落在
        `gap_drain`（`0 22 * * 1-5`）的运行点上** ⇒ 这条链是通的。

    ## 网址怎么"搭上"那条管道：走 `reason`

    实测 `gap_queue.drain_gaps()` 里那一行是：

        result = await resolver.resolve(entry.indicator, entry.reason)

    也就是说 **A19 收到的就是 `(indicator, reason)`** —— `reason` 是既有契约里
    **唯一**能捎带上下文的地方。所以这里把候选网址写进 `reason`，
    **不需要改 `GapEntry` 的字段、不需要新的路由**。

    ## 上限与影响面（照实写清，别让它悄悄烧钱）

      · 只带 **前 3 个**网址（`reason` 入队时被截到 200 字，多了也留不下）；
      · **去重**：`GapQueue` 按 `(indicator, reason[:80])` 去重 24h ⇒ 同一批网址
        一天只入队一次；
      · **重试上限**：`MAX_ATTEMPTS=3`，失败 3 次后不再入队；
      · **单轮上限**：`drain_gaps(max_items=5)` ⇒ 每晚最多送 5 条给 A19；
      · ⚠️ **影响面**：A19 走的是**付费 reasoning 层**，而且它的产物
        （生成的连接器）会被 `dynamic_loader` **装进 `ConnectorRouter`** ——
        即一个生成得不好的连接器**会进到取数链里**。这是既有 gap 路径**本来就有**的
        行为（已有 5 个 `gap_*.py` 就是这么来的），本条只是多了"网址"这一种线索来源。
      · 因此**必须可观测**：入队成功记 INFO，入队被拒（去重/超限）记 DEBUG。

    绝不抛异常：换源是补救路径。
    """
    try:
        from src.domain.agents.decision.gap_queue import get_gap_queue

        urls = [str(h.get("url") or "") for h in (hits or [])[:3]]
        urls = [u for u in urls if u]
        if not urls:
            return False
        reason = ("候选源网址（联网搜索得到，待 A19 判定能否建成连接器）："
                  + "；".join(urls))
        ok = get_gap_queue().enqueue(indicator, reason=reason,
                                     source="path_c_search")
        if ok:
            logger.info("[换源线索] 已入 A19 缺口队列：indicator=%s（%d 个候选网址）",
                        indicator, len(urls))
        else:
            logger.debug("[换源线索] 未入队（去重/超限/已放弃）：%s", indicator)
        return bool(ok)
    except Exception as exc:  # noqa: BLE001 入队失败不许影响换源
        logger.debug("换源线索入队失败（忽略）: %s", exc)
        return False


async def discover_candidate_urls(
    indicator: str, *, context: str = "", limit: int = 5,
    search: Any = None, root: Path | None = None,
) -> tuple[list[dict[str, str]], str]:
    """**path C**：联网搜索给一个"取不到"的指标找**候选数据源网址**。

    ## 它返回什么、不返回什么（别把线索当源）

    返回 `(hits, note)`：`hits` 是 `[{title,url,snippet,site}]`，
    **只是一组网址**。它**不产点**、**不做口径校验**、**不落覆盖层** ——
    因为 `CandidateSource` 要的是能产点的连接器，而"网址 → 连接器"是 B/A19 的活。
    所以本函数的产物进的是**换源审计的线索区**（`kind="search_candidates"`），
    供人工/A19 复核。

    ## 三条硬约束

    1. **绝不抛异常**：搜索是不可靠的外部依赖，它挂掉不能把换源链拖垮；
    2. **只走后台路径**：带闸门与缓存的 `web_search` 是花钱且有延迟的，
       交互路径有 10s 防撞钟预算，两者不是一回事；
    3. **花不花钱由调用方看得见**：`note` 里带上 `spent`/`blocked_by`，
       这样"没搜到"与"额度闸门关了"在日志里不会长得一样。
    """
    from src.infrastructure.search import router

    q = f"{indicator} 数据 官方 统计 来源"
    if context:
        q = f"{indicator} {context} 数据来源"

    #: ★ 走 **router**（主源挂了自动换备用）—— 不是直连某一家。
    #: 这就是"第二个搜索源真的会被调用"的落点（R2：备用的定义是真会被调用）。
    fn = search or router.web_search
    try:
        #: 搜索是**同步阻塞**的（urllib），在事件循环里直接调会把心跳拖住 ⇒ 丢线程池
        outcome = await asyncio.to_thread(fn, q, count=limit)
    except Exception as exc:  # noqa: BLE001 绝对不能把换源链拖垮
        note = f"搜索调用异常（不是'没有这个源'）：{type(exc).__name__}: {exc}"
        _append_search_lead(indicator, [], note, root=root)
        #: 抛异常也是**搜索源自己的问题** ⇒ 同样归到 `search_source`，
        #: 而不是让它悄悄消失（"没搜过"与"搜了但炸了"必须能分开）。
        _record_anomaly(indicator,
                        type("_O", (), {"ok": False, "blocked_by": "transport",
                                        "reason": note, "served_by": "",
                                        "spent": False})(),
                        [], root=root)
        return [], note

    hits = [h.to_dict() if hasattr(h, "to_dict") else dict(h)
            for h in (getattr(outcome, "hits", None) or [])]
    flags = (f"served_by={getattr(outcome, 'served_by', '') or '-'} "
             f"spent={getattr(outcome, 'spent', None)} "
             f"blocked_by={getattr(outcome, 'blocked_by', '') or '-'} "
             f"from_cache={getattr(outcome, 'from_cache', None)}")
    note = f"搜索「{q}」→ {len(hits)} 条候选网址（{flags}）"
    _append_search_lead(indicator, hits, note, root=root)
    _record_anomaly(indicator, outcome, hits, root=root)
    #: ★ 线索的**终点不再只是人**：同时喂进既有的 A19 缺口队列
    #: （`drain_gaps` 会把 `reason` 交给 `resolver.resolve`）。
    _enqueue_lead_for_a19(indicator, hits)
    return hits, note


async def reroute(
    indicator: str, router: Any, old_points: list[Any], *,
    skip: Any = None, limit: int = MAX_PROBE, root: Path | None = None,
    allow_promote: bool = True, allow_search: bool = True,
    search: Any = None,
) -> tuple[list[Any], str]:
    """换源主流程：探测 → 口径校验 → 落覆盖层。返回 `(可用的点, 说明)`。

    **提升规则**（纪律 2 的落点）：
      * 重叠 ≥ `SHADOW_MIN_OVERLAP` 期且一致 ⇒ `promoted`（下一轮直接优先用它）；
      * 只有频率+量级过（重叠不足）⇒ `shadow`（**不改取数顺序**，继续观察）；
      * 不过 ⇒ 不落盘，只写审计（理由保留，供人工判断）。

    **`allow_search`**（path C 的总开关，默认开）：A 路径（免费）全挂之后，
    是否再去**联网搜索**候选源线索。默认开是因为用户口径要"找不到就联网找"；
    花钱的闸门不在这里，而在 `infrastructure/search/bocha.py` 的
    `MAX_CALLS_TOTAL / MAX_CALLS_PER_DAY`（上限写在代码里，另有 24h 结果缓存）。
    """
    candidates = await probe_connectors(router, indicator, skip=skip,
                                        limit=max(limit, 1 + MAX_BACKUP_SOURCES))
    usable = [c for c in candidates if c.points]
    if not usable:
        why = "；".join(f"{c.source_key}:{c.error}" for c in candidates if c.error) \
            or "没有任何连接器认这个指标"
        record_decision(indicator, CaliberVerdict(False, f"无可用候选源（{why}）"),
                        root=root)
        #: ★ **path C 的接线点**（回答"谁调它"）：A 走到头了才去联网找线索。
        #: 顺序是刻意的 —— A 免费且确定性，C 花钱且有延迟；能免费解决就不花钱。
        #: 这里**只拿线索**（`actionable=False`），绝不把它当成已可用的源。
        leads, lead_note = (await discover_candidate_urls(indicator, root=root,
                                                          search=search)
                            if allow_search else ([], "未开启联网搜索（allow_search=False）"))
        tail = (f"；已联网搜到 {len(leads)} 条候选源线索（待复核，见换源审计）"
                if leads else f"；{lead_note}")
        return [], f"换源失败：{why}{tail}"

    # ★ 用户口径（2026-09-30）：「**也可以联网查询获取两个数据源，作为备用**」
    #   ⇒ 一次换源要把**主源 + 最多 `MAX_BACKUP_SOURCES` 个备源**一起定下来：
    #   逐个过口径校验，**通过的都收**（先到者为主源）；全部不过才判失败。
    passed: list[tuple[str, CaliberVerdict, list[Any]]] = []
    for cand in usable:
        verdict = check_caliber(old_points, cand.points)
        if verdict.ok:
            passed.append((cand.source_key, verdict, cand.points))
        else:
            #: 被拒的候选也要留痕（人工判断"是不是我们口径搞错了"）
            record_decision(indicator, verdict, source_key=cand.source_key,
                            origin=cand.origin, root=root)
        if len(passed) >= 1 + MAX_BACKUP_SOURCES:
            break

    if not passed:
        return [], "换源失败：所有候选源口径校验未通过（详见换源审计）"

    primary_key, primary_verdict, primary_points = passed[0]
    backup_keys = [k for k, _v, _p in passed[1:]]
    #: 主源那一条用**主判据**（重叠期）决定 promoted / shadow；备源不单独提升
    strong = int(primary_verdict.checks.get("overlap_periods") or 0) >= SHADOW_MIN_OVERLAP
    status = "promoted" if (strong and allow_promote) else "shadow"
    record_decision(indicator, primary_verdict, source_key=primary_key,
                    origin="connector", status=status, backups=backup_keys,
                    root=root)
    tail = f"，备源 {backup_keys}" if backup_keys else ""
    return primary_points, (
        f"换源{'成功' if status == 'promoted' else '进影子期'}：主源 {primary_key}"
        f"{tail}（{primary_verdict.reason}）")


__all__ = [
    "CandidateSource",
    "CaliberVerdict",
    "DECISION_LOG_NAME",
    "FREQ_RATIO_MAX",
    "MAGNITUDE_RATIO_MAX",
    "MAX_PROBE",
    "OVERLAP_REL_TOL",
    "OVERRIDE_NAME",
    "PROBE_TIMEOUT_SEC",
    "SHADOW_MIN_OVERLAP",
    "SHADOW_SECONDS",
    "check_caliber",
    "discover_candidate_urls",
    "load_overrides",
    "preferred_source",
    "probe_connectors",
    "record_decision",
    "reroute",
]
