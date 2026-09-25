"""知识星球**增量**拉取 —— 按时间水位线，停机后可回填。

## 用户口径（2026-09-25）

> 每条都是时间戳，可以通过时间戳对接确认，上次获取到 A 时间，
> 这次就从当前时间到 A 时间来获取信息。但是太多的信息会导致本地大模型
> 分析得很慢，这个要注意设定个上限或者缩短获取信息周期，
> 让本地模型少量多次的去获取，建议 2 小时一次。
> 还要考虑下半夜关闭电脑，再次开机后自动启动这些服务。

## 方案：水位线 + 翻页回填 + 硬上限

    水位线 watermark = 已处理过的**最新** create_time

    正常：拉第一页 → 只保留 create_time > watermark 的
    停机后：第一页可能全比 watermark 新（中间断了）→ 用 `end_time` 往回翻页

⚠️ **`end_time` 是含边界的**（实测返回 `<=` 该时间的那条）。
所以：
  · 过滤用**严格大于** watermark，否则每次重复处理边界那条；
  · 翻页时 `end_time` 取当前页**最早**那条的时间，下一次自然会把它自己
    再带回来一次 —— 靠上面的严格过滤去掉。

## 为什么必须有上限（用户明确提醒）

一次性灌几百条进本地 qwen3:8b，会让分析阶段卡到超时，
而且卡住之后**整批都拿不到结果**（不是慢，是全废）。
所以三处设硬上限，宁可分多次、并如实记 `degraded`：
  · `MAX_FETCH_PER_RUN` 单次拉取条数
  · `MAX_PAGES` 回填翻页数（超过则记缺口，**不静默丢**）
  · `MAX_ANALYZE_PER_RUN` 单次送模型条数（供调用方取用）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 水位线文件（在 data/credentials 同级，已被 .gitignore 覆盖）
CURSOR_DIR = Path("data") / "intel"
CURSOR_FILE = "zsxq_cursor.json"

#: 单次拉取上限。防"开机后一次拉几百条"。
MAX_FETCH_PER_RUN = 30

#: 回填翻页上限（每页 30 条 → 最多 150 条 ≈ 停机一整天）。
MAX_PAGES = 5

#: 单次送本地模型分析的上限。**这是防卡死的关键**：
#: 本地 qwen3:8b 实测 ~30s/批，条数不控会把整批拖超时。
MAX_ANALYZE_PER_RUN = 20

#: 单页请求条数
PAGE_SIZE = 30


@dataclass
class IncrementalResult:
    """一次增量拉取的结果。`degraded` 为真时**必须**在界面上标注缺口。"""

    topics: list[Any]
    watermark: str          # 本次之后的新水位线
    pages_used: int = 1
    new_count: int = 0
    truncated: bool = False     # 命中翻页上限，仍有更早内容未取
    gap_note: str = ""          # 缺口说明（可进界面的"数据不完整"提示）


def _cursor_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / CURSOR_DIR / CURSOR_FILE


def load_watermark(root: Path | None = None) -> str:
    """读水位线。**读不到返回空串**（调用方按"首次运行"处理）。"""
    p = _cursor_path(root)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("watermark") or "")


def save_watermark(watermark: str, *, root: Path | None = None,
                   note: str = "") -> None:
    """保存水位线（原子写）。"""
    if not watermark:
        return
    p = _cursor_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "watermark": watermark,
        "updated_at": datetime.now(timezone.utc).astimezone()
        .isoformat(timespec="seconds"),
        "note": note,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)


def _parse_ts(text: str) -> datetime | None:
    """解析知识星球的时间戳（形如 `2026-09-25T11:45:43.285+0800`）。

    注意末尾是 `+0800`（无冒号），`fromisoformat` 在 3.10 及以下不接受，
    所以先归一化。解析失败返回 `None`（不猜）。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    # +0800 → +08:00
    if len(raw) >= 5 and (raw[-5] in "+-") and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    for parser in (datetime.fromisoformat,):
        try:
            dt = parser(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_incremental(*, root: Path | None = None,
                      max_fetch: int = MAX_FETCH_PER_RUN,
                      max_pages: int = MAX_PAGES) -> IncrementalResult:
    """增量拉取。**同步**（调用方 `to_thread`）。

    只在"水位线为空"时拉一整页当作首次基线；
    之后只返回严格新于水位线的内容。
    """
    from src.infrastructure.connectors.zsxq_source import fetch_topics

    watermark = load_watermark(root)
    wm_dt = _parse_ts(watermark) if watermark else None

    collected: list[Any] = []
    pages = 0
    truncated = False
    end_time = ""

    while pages < max_pages and len(collected) < max_fetch:
        pages += 1
        page = fetch_topics(limit=PAGE_SIZE, end_time=end_time or None)
        if not page:
            break

        fresh = []
        for t in page:
            ts = _parse_ts(t.created_at)
            if ts is None:
                # 时间戳解析失败：保守地**当作新的**收下，并在缺口里说明。
                # 宁可多处理一条，也不要静默丢内容（本项目"不猜"口径：
                # 不知道新旧时，倾向于不丢）。
                fresh.append(t)
                continue
            if wm_dt is None or ts > wm_dt:
                fresh.append(t)
        collected.extend(fresh)

        # 翻页：取本页最早时间；下一轮 end_time 用它（含边界，
        # 该条会再回来一次，但会被上面的严格过滤剔除）
        times = [t.created_at for t in page if t.created_at]
        if not times:
            break
        earliest = min(times)
        if end_time and earliest >= end_time:
            # 没往前推进（服务端忽略 end_time）→ 停止，避免死循环
            logger.warning("增量翻页未推进（end_time 可能未被支持），提前结束")
            truncated = True
            break
        end_time = earliest

        # 本页如果全是老内容，说明已经追上水位线，可以停
        if not fresh:
            break

    # ── 截断判定 ──
    # 首版这里写的是 `pages >= max_pages and len(collected) >= max_fetch`，
    # 漏了一种常见情形：**第一页就装满上限**（行情密集时段 30 条约 20 分钟
    # 就满），此时 pages=1 < max_pages，于是 truncated=False ——
    # 界面上不会提示"数据不完整"，而实际已丢弃更早内容。
    #
    # 正确判据：只要**收够了上限**或**翻满页数**，就说明还有没取完的。
    if collected and (len(collected) >= max_fetch or pages >= max_pages):
        truncated = True

    collected = collected[:max_fetch]

    # 新水位线 = 本次见过的**最新**时间（没取到就用旧的）
    newest = watermark
    for t in collected:
        ts = _parse_ts(getattr(t, "created_at", "") or "")
        if ts is None:
            continue
        if not newest or ts > (_parse_ts(newest) or ts):
            newest = t.created_at

    gap = ""
    if truncated:
        gap = (f"回填命中上限（{pages} 页 / {len(collected)} 条），"
               f"仍有更早内容未取；下次运行会继续往前追")

    return IncrementalResult(
        topics=collected,
        watermark=newest,
        pages_used=pages,
        new_count=len(collected),
        truncated=truncated,
        gap_note=gap,
    )


__all__ = [
    "CURSOR_FILE",
    "MAX_ANALYZE_PER_RUN",
    "MAX_FETCH_PER_RUN",
    "MAX_PAGES",
    "PAGE_SIZE",
    "IncrementalResult",
    "fetch_incremental",
    "load_watermark",
    "save_watermark",
]
