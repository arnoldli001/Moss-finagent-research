"""知识星球**增量**拉取 —— 按时间水位线，停机后可回填。

## 用户口径（2026-09-25）

> 每条都是时间戳，可以通过时间戳对接确认，上次获取到 A 时间，
> 这次就从当前时间到 A 时间来获取信息。但是太多的信息会导致本地大模型
> 分析得很慢，这个要注意设定个上限或者缩短获取信息周期，
> 让本地模型少量多次的去获取，建议 2 小时一次。
> 还要考虑下半夜关闭电脑，再次开机后自动启动这些服务。

## 方案：最老边界水位线 + 连续回取 + 硬上限

    watermark = "**已处理区间的最老边界**"，不变式是
                「[watermark, 现在] 区间内的内容都已处理完」

    拉取只有**一个方向**：`end_time=watermark` 连续往回取，
    取到什么就把 watermark 推到这批里最老的那条；
    取空 = 已追平到"现在"。

    为什么不是"处理过的最新一条"：那会把一个时间戳当成两个边界用，
    导致"水位线推过去了、它下面的内容却还没取" —— 而那些内容
    再也不会被请求（静默丢数据，详见 `fetch_incremental` 的 docstring）。

⚠️ **`end_time` 是含边界的**（实测返回 `<=` 该时间的那条），所以每条
边界记录都会在下一轮再回来一次 —— 靠 `content_hash` 去重，
并且**边界那条要预置进去重集**（否则追平后每轮都收那 1 条，永不收敛）。

⚠️ **数据源会偶发返回空页**（实测同一请求 6 次里 1 次空返回）。
空返回必须复核再判定"已追平"，否则水位线会原地不动、内容静静停住。
详见 `fetch_incremental` 里的空结果复核。

⚠️ **不要在"只取到几条"时把水位线推到最新**。首版就是那样丢掉 29 条的。

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

#: 空结果重试次数
#:
#: 数据源**会偶发返回空页**（实测同一请求 6 次里 1 次空），
#: 不重试就会把抖动当"已追平"，水位线原地不动、内容静静停在原地。
_EMPTY_RETRY = 2


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

    ## 水位线语义：**已处理过的最新时间戳**（high-water mark）

    ## 水位线语义：**已处理区间的最老边界**

    不变式：**`[水位线, 现在]` 区间内的内容都已处理完。**

    取法永远只有一个方向 —— `end_time=水位线` **连续地往回取**，
    取到什么就把水位线往前推到这批里最老的那条：

        水位线 T   → 取 (…, T]     → 水位线 = 这批最老 t1
        水位线 t1  → 取 (…, t1]    → 水位线 = 这批最老 t2
        ……一直追到取空（说明已经追平到"现在"）

    ## 为什么不能用"处理过的最新一条"当水位线

    那是首版的写法，有一个**静默丢数据**的 bug：

        水位线 13:15:06 → 第 1 页（无 end_time）30 条：13:15:41 … 11:43:09
        → 过滤 `ts > 水位线` 只剩 1 条（13:15:41）
        → 收 1 条，水位线推到 13:15:41
        → 下一轮第 1 页没有更新的 → `fresh` 空 → break
        → 第 2 页那 29 条**永远不会被取**

    根因是它把"一个时间戳"当成了两个边界（新鲜区的下界 **和** 回填走的上界）。
    "最新一条"只说明**它自己**处理过，不说明它**下面**的内容处理过 ——
    而水位线一旦推过去，下面的内容就再也不会被请求。

    "最老边界"没有这个问题：它天然是连续区间的端点，推进即代表区间扩大。
    """
    from src.infrastructure.connectors.zsxq_source import fetch_topics

    watermark = load_watermark(root)

    collected: list[Any] = []
    seen: set[str] = set()
    pages = 0
    truncated = False
    back = watermark or ""          # 连续回取的起点

    while pages < max_pages and len(collected) < max_fetch:
        pages += 1
        before = len(seen)
        # `end_time` 含边界 → 边界那条每轮都会再回来，靠 content_hash 去重。
        page = fetch_topics(limit=PAGE_SIZE, end_time=back or None)

        # ── 空结果复核（数据源会抖，实测约 1/6 概率）──
        #
        # 同一个 `end_time` 连续调用 6 次：try0~3 各 30 条、**try4 返回 0 条**、
        # try5 又 30 条。把这个抖动当成"已追平"，水位线会停在原地不再推进，
        # 而调用方看到 `new_count=0` 会认为无事发生 —— 又是一种静默停摆。
        #
        # 复核：不带 `end_time` 拿最新一页，若其中存在比水位线**更老**的内容，
        # 说明两侧对不上（水位线之前还有东西没取），那就重试。
        # 最多试 `_EMPTY_RETRY` 次，避免把偶发抖动变成偶发延迟。
        if not page and back:
            probe = fetch_topics(limit=PAGE_SIZE, end_time=None) or []
            oldest_probe = min((t.created_at for t in probe if t.created_at),
                               default="")
            if oldest_probe and oldest_probe < back:
                for _ in range(_EMPTY_RETRY):
                    page = fetch_topics(limit=PAGE_SIZE, end_time=back)
                    if page:
                        logger.info("增量首取为空（数据源抖动）后重试成功")
                        break

        if not page:
            break                   # 取空 = 已追平到"现在"

        # ── 预置"边界那条" ──
        #
        # `end_time` **含边界**，所以往回要时**边界那条自己**总会被返回一次；
        # 而它必然已经处理过（水位线就是它）。必须在这里先放进去重集：
        #
        #   · 不预置 → 追平后每轮都把这 1 条当新内容收下，`new_count` 永远为 1、
        #     水位线不动 —— 表现为**永不收敛**（实测卡在 1 条）。
        #   · 用"页首 == 边界就 break"来代替 → 会误伤正常翻页
        #     （正常翻页时页首本来就可能正好落在边界上，那是健康推进）。
        #
        # 预置只影响**正好等于边界**的那一条，不影响它后面真正的新内容。
        if back:
            for t in page:
                if str(getattr(t, "created_at", "") or "") == back:
                    h = str(getattr(t, "content_hash", "") or "")
                    if h:
                        seen.add(h)
                    break

        for t in page:
            h = str(getattr(t, "content_hash", "") or "")
            if h and h in seen:
                continue
            if h:
                seen.add(h)
            collected.append(t)
            if len(collected) >= max_fetch:
                break

        new_tail = min((t.created_at for t in page if t.created_at), default="")
        if not new_tail:
            # 整页都没有可用时间戳 → 无法推进，停在原地而不是死循环
            logger.warning("增量页无可用时间戳，提前结束")
            truncated = bool(page)
            break

        # ⚠️ 判定"到底了"要用**去重集有没有增长**，不能用 `new_tail >= back`。
        #
        # 追平之后服务端会稳定返回"以边界那条为最新的一小页"，此时页尾正好
        # 等于 `back`，于是 `new_tail >= back` 成立 —— 但那是**正常追平**，
        # 不是"服务端忽略了 end_time"。首版这样判会把"已追平"误报成警告，
        # 并且每轮都返回那 1 条边界记录，**永不收敛**。
        if len(seen) == before:
            break                   # 本页全是在去重集里的内容 = 到底了
        back = new_tail

        # 没满页 = 服务端已给到最老，追平了
        if len(page) < PAGE_SIZE:
            break

    # ── 截断判定 ──
    # 只要**收够了上限**或**翻满页数**，就说明还有更早的没取完。
    # （首版判据 `pages >= max_pages and len >= max_fetch` 漏了
    #  "第一页就装满上限"这种最常见的情形，界面不会提示数据不完整。）
    if collected and (len(collected) >= max_fetch or pages >= max_pages):
        truncated = True

    collected = collected[:max_fetch]

    # ── 新水位线 = 本次取到的**最老**那条 ──
    # 没取到内容就保持原值（"只处理了几条就把水位线推过去"正是首版丢数据的机制）。
    new_watermark = watermark
    if collected:
        oldest = min((t.created_at for t in collected if t.created_at),
                     default="")
        if oldest:
            new_watermark = oldest

    gap = ""
    if truncated:
        gap = (f"回填命中上限（{pages} 页 / {len(collected)} 条），"
               f"仍有更早内容未取；下次运行会继续往前追")

    return IncrementalResult(
        topics=collected,
        watermark=new_watermark,
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