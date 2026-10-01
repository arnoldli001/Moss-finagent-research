"""换源线索（path C 的产物）的**读取与上报状态** —— 给线索一个消费方。

## 为什么需要这个文件（R2：禁止"声明式通路"）

`source_reroute.discover_candidate_urls()` 会把联网搜到的候选数据源网址写进
`source_reroute_log.jsonl`（`kind="search_candidates"`）。**但写进去之后没人读** ——
实测：`grep -r "source_reroute_log|search_candidates" src/` **零消费方**。
那正是 `.trae/skills/backup-path-availability/SKILL.md` 的 R2 要防的形状：
**一条通路"写了"但"没人看"，等于不存在**（本仓库已为此付过四次代价）。

本模块是那句"谁读它"的答案，`scheduler.jobs` 的 `source_leads_audit` 作业调它。

## ★ 噪音纪律（三层闸门，缺一不可）

`AGENTS.md`《告警/通知硬约束》要求任何"会打扰人的渠道"必须三层齐备。
线索上报走的是**后端日志**（运维通道），同样照此办理：

| 层 | 本模块怎么落 |
|---|---|
| ① **事件级去重**（同一件事别重复） | `lead_id = sha1(indicator｜url)`，**上报过一次就不再报** —— 状态落盘 |
| ② **渠道级速率/合并**（一天很多件别淹人） | 单轮**最多报 `MAX_PER_RUN` 条**，其余留给下一轮；按指标合并 |
| ③ **接收者级静默** | 只写**后端日志**（管理员通道），**不发邮件、不推前端** |

**闸门状态必须落盘**（放进程内存里的冷却重启即失效）⇒ `source_leads_state.json`，
并且**有界**（`MAX_STATE_ENTRIES`，老的先丢）。

## 两条不许越过的线

1. **只读，绝不改事实**：本模块**只读**审计、**只写**自己的状态文件；
   它**不会**去建连接器、**不会**改 `indicators.yaml`、**不会**落覆盖层。
   "搜到网址"到"能用"之间那一步永远是**人的复核**（或 A19 的活）。
2. **绝不抛异常**：审计文件可能被写坏、被截断。读失败就返回空 + 原因，
   **不能**因为它把调度作业打挂（换源是补救路径，不是主链路）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 上报状态文件名（放在**登记的** `run_dir` 下 ⇒ 不写路径字面量）。
STATE_NAME = "source_leads_state.json"
#: 单轮最多上报几条（渠道级限速 —— 别把日志刷成噪音）。
MAX_PER_RUN = 10
#: 状态文件最多记多少条（有界；老的先丢，丢了最坏是"再报一次"）。
MAX_STATE_ENTRIES = 2000
#: 只读最近多久的线索（更老的已经没意义：数据源地址会变）。
LOOKBACK_HOURS = 24 * 30.0


def _state_path(root: Path | None = None) -> Path:
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    base = Path(root) if root is not None else PROJECT_ROOT
    return base / store_rel("run_dir") / STATE_NAME


def _log_path(root: Path | None = None) -> Path:
    """复用 `source_reroute` 的审计路径（**单一真值源**，别另算一份）。"""
    from src.infrastructure.catalog.source_reroute import _log_path as p

    return p(root)


def lead_id(indicator: str, url: str) -> str:
    """线索的稳定标识：**同一指标 + 同一网址**才算同一条。"""
    return hashlib.sha1(f"{indicator}|{url}".encode()).hexdigest()[:16]


def _load_state(root: Path | None = None) -> dict[str, float]:
    p = _state_path(root)
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        seen = raw.get("surfaced") if isinstance(raw, dict) else None
        return {str(k): float(v) for k, v in (seen or {}).items()}
    except Exception as exc:  # noqa: BLE001
        #: 状态读不到 ⇒ **不能**当成"都报过"（会永久静默），也不能崩。
        #: 取"空状态"= 最坏情况重复报一次日志，代价最小。
        logger.warning("换源线索上报状态读取失败（按空处理，最坏重复报一次）: %s", exc)
        return {}


def _save_state(state: dict[str, float], root: Path | None = None) -> None:
    items = sorted(state.items(), key=lambda kv: kv[1], reverse=True)
    trimmed = dict(items[:MAX_STATE_ENTRIES])
    try:
        _state_path(root).write_text(
            json.dumps({"surfaced": trimmed}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("换源线索上报状态写入失败: %s", exc)


def read_leads(*, root: Path | None = None, since_hours: float = LOOKBACK_HOURS,
               limit: int = 500) -> list[dict[str, Any]]:
    """读审计里的候选源线索，**逐条网址展开**（带稳定 id）。**绝不抛异常**。"""
    p = _log_path(root)
    cutoff = time.time() - since_hours * 3600.0
    out: list[dict[str, Any]] = []
    try:
        if not p.exists():
            return []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001 单行坏了跳过，不影响其余
                continue
            if rec.get("kind") != "search_candidates":
                continue
            ts = float(rec.get("ts") or 0)
            if ts and ts < cutoff:
                continue
            indicator = str(rec.get("indicator") or "")
            for h in rec.get("hits") or []:
                url = str((h or {}).get("url") or "")
                if not url:
                    continue
                out.append({
                    "id": lead_id(indicator, url),
                    "indicator": indicator,
                    "url": url,
                    "title": str((h or {}).get("title") or ""),
                    "site": str((h or {}).get("site") or ""),
                    "ts": ts,
                })
                if len(out) >= limit:
                    return out
    except Exception as exc:  # noqa: BLE001 读失败不许把作业打挂
        logger.warning("换源线索读取失败（按无线索处理）: %s", exc)
        return []
    return out


def filter_new(leads: list[dict[str, Any]], *,
               root: Path | None = None) -> list[dict[str, Any]]:
    """挑出**尚未上报过**的线索（第①层：事件级去重）。"""
    seen = _load_state(root)
    return [x for x in leads if x["id"] not in seen]


def mark_surfaced(ids: list[str], *, root: Path | None = None) -> int:
    """把这些线索标记为"已上报"（状态落盘）。返回标记条数。"""
    if not ids:
        return 0
    state = _load_state(root)
    now = time.time()
    for i in ids:
        state[i] = now
    _save_state(state, root)
    return len(ids)


def summarize(leads: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """按指标合并计数（第②层：渠道级合并）—— 便于一行说清一个指标。"""
    counts: dict[str, int] = {}
    for x in leads:
        counts[x["indicator"]] = counts.get(x["indicator"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


__all__ = [
    "LOOKBACK_HOURS",
    "MAX_PER_RUN",
    "MAX_STATE_ENTRIES",
    "STATE_NAME",
    "filter_new",
    "lead_id",
    "mark_surfaced",
    "read_leads",
    "summarize",
]
