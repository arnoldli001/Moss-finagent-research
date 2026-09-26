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
#:
#: ## 30 → 100（用户口径 2026-09-26："拉高上限 到100条"）
#:
#: 实测这个群约 **90 条/天**，而 30 条只覆盖 **7.4 小时**（13:21~20:47）。
#: 页面上因此只有"最近几小时"的券商作文，用户看到的是"原来的信息丢那里去了"。
#: 提到 100 条 ≈ 覆盖一整天（4 页 × 30 = 120，取 100）。
#:
#: ⚠️ 代价是**每轮 4 次上游调用**（原 1 次）。这条链路长在 `build_feed` 上，
#: 而它有 `FEED_CACHE_TTL`（60 秒）与单飞保护，所以正常浏览不会放大请求；
#: 但"反复手动刷新"会真的打 4 倍。上游限频的表现是**返回空页**，
#: 而空页那条路已经单独加固过（`upstream_empty`：重试 + 如实报缺口，
#: 不再冒充"最近 3 天无更新"）。
#:
#: 与 `item_store` 的分工：上限决定"这一轮新取到多少"，留存决定"页面上能看到
#: 多少"。两者都要，缺一个都会重新出现"刷几次内容不一样"。
MAX_FETCH_PER_RUN = 100

#: 翻页上限 = **单轮上游调用次数的硬上限**（含空页重试）。
#:
#: 5 → 8（2026-09-26）：单轮上限提到 100 条后，一轮至少要翻 4 页
#: （`MAX_FETCH_PER_RUN / PAGE_SIZE` 向上取整），而空页复核也会计入这个预算
#: —— 上限留 5 的话"4 页 + 一次重试"就顶格了，再多一次抖动这一轮就只能收工。
#:
#: ⚠️ 它必须 ≥ `MAX_FETCH_PER_RUN / PAGE_SIZE`，否则单轮上限永远够不到 ——
#: 那种"改了一个常量但被另一个更小的常量卡住"的形态没有任何报错。
MAX_PAGES = 8

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
    watermark: str          # 本次之后的新水位线（已处理区间的**最老**边界）
    pages_used: int = 1
    new_count: int = 0
    truncated: bool = False     # 命中翻页上限，仍有更早内容未取
    gap_note: str = ""          # 缺口说明（可进界面的"数据不完整"提示）
    #: 本次之后的新高水位线（已取到的最新时间戳）。调用方要把它写回游标，
    #: 否则下一轮会把同一批最新内容再取一遍。
    newest: str = ""
    #: 因单轮上限而**放弃**的条数（保最新、丢最旧）。>0 时 `gap_note` 会说明。
    skipped: int = 0
    #: **本次"最新那一页"没取到**（数据源返回空，重试后仍为空）。
    #:
    #: 这一位是为一个会**整块抹掉界面内容**的缺陷加的观测点，见阶段 A 里那段
    #: 长注释。置位时 `topics` 为空、`truncated` 为真，调用方必须**如实报缺口**
    #: ——不能让它退化成"券商作文最近 3 天无更新"这种与事实相反的说法。
    upstream_empty: bool = False


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


def load_newest(root: Path | None = None) -> str:
    """读**高水位线**（已处理过的最新时间戳）。读不到返回空串。

    ## 为什么要有第二个游标（2026-09-25 报障后补）

    只有一个"最老边界"水位线时，每轮都得从"现在"往回走，于是**同一批最新内容
    会被每轮重复取一遍**：游标不动、`new_count` 永远一样、本地小模型反复分析
    同 30 条。而这个群**产量极高** —— 实测 30 条只覆盖 1 小时 45 分钟，
    12 天的空档是数千条，靠每轮 30 条的回填根本追不平。

    `newest` 管的是"**每轮只取真正的新内容**"（`ts > newest` 才算新）；
    `watermark` 管的是"历史回填到哪儿了"。两者分工不同，缺一不可。
    """
    p = _cursor_path(root)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("newest") or "")


def save_watermark(watermark: str, *, root: Path | None = None,
                   note: str = "", newest: str = "") -> None:
    """保存游标（原子写）。`newest` 传空则**保留原值**（不会被清掉）。"""
    if not watermark and not newest:
        return
    p = _cursor_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    previous: dict = {}
    if p.exists():
        try:
            previous = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            previous = {}
    payload = {
        "watermark": watermark or str(previous.get("watermark") or ""),
        "newest": newest or str(previous.get("newest") or ""),
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
    newest = load_newest(root)       # 高水位线：已取到哪儿了（跨轮去重靠它）

    collected: list[Any] = []
    seen: set[str] = set()
    pages = 0
    truncated = False
    skipped = 0                      # 因为上限而**放弃**的条数（如实报出来）
    caught_up = False                # 阶段 A 是否已经追到"最新"（无空档）
    # ⚠️ 分界用 `watermark`（已回填的下界），**不能用 `newest`**。
    #
    # 这里踩过一次（2026-09-25，用户报障"前端看不到 1 条知识星球信息"）：
    # 我一度把分界设成 `newest`，于是"上一轮已取过的最新 30 条"这一轮被跳过 ——
    # 但**情报流是每次请求现拼的、不落库**，上一轮取到的内容并没有存在任何地方，
    # 跳过等于让它**永远不会出现在页面上**。`counts` 里 research_note 直接归零。
    #
    # 高水位线只对"采集→落库"的管线成立；这条链路是"采集→直接返回给调用方"，
    # 所以每轮**必须重新取最新那一段**（幂等、便宜，靠 content_hash 去重）。
    fresh_floor = watermark

    # ══════════════════════════════════════════════════════════════
    # 阶段 A：先把「现在 → 水位线」这段补上
    # ══════════════════════════════════════════════════════════════
    #
    # ⚠️ 这一段是原实现**整个漏掉**的，也是用户报障的根因（2026-09-25）：
    #
    # 原实现直接以水位线为起点往回取（`end_time=水位线`），于是**水位线之后
    # （更新）的内容永远不会被请求**。实测：
    #
    #     服务端首屏（不传 end_time）= 2026-09-25T13:22  ← 与网站一致，最新在前
    #     水位线                     = 2026-09-13T11:28
    #     `end_time=水位线`           = 0 条（只往回看）
    #
    # 也就是 12 天的新内容从未被取过，情报流里只剩十几天前的旧研报。
    # 用户原话："你抓取信息时没有先按时间排序，正常只要打开界面，
    # 信息第一条就是最新事件，越往下翻越是历史日期信息。"
    #
    # 根因是**把不变式当成了事实**：原 docstring 写"`[水位线, 现在]` 区间内的
    # 内容都已处理完"，但"水位线存在"并不能推出这句话成立 —— 只要服务停过
    # 一段时间、或水位线被写过一次，后面的内容就再也没人去取。
    # 不变式必须**每轮真的从"现在"往回走一遍**来验证，而不是假定。
    #: 本次"最新那一页"是否没取到（定义在分支**外面**：下面 `if fresh_floor:`
    #: 的 else 支路也要能读到它 —— 第一次运行时游标为空，走的是 else 支路）。
    upstream_empty = False
    if fresh_floor:
        back: str | None = None      # 空 = 从最新开始（等价于网站首屏）
        while pages < max_pages and len(collected) < max_fetch:
            pages += 1
            page = fetch_topics(limit=PAGE_SIZE, end_time=back)

            # ── 空页复核：**每一页都要做**，不只是第一页 ──
            #
            # 实测（2026-09-26，单轮上限提到 100 之后）：一轮要翻 4 页，而
            # 上游空页约 1/6 —— 于是"一轮里至少撞到一次空页"的概率约
            # 1-(5/6)^4 ≈ **52%**。撞上时的后果是**本轮提前收工**，
            # 剩下的新鲜内容这一轮取不到（它下一轮才会出现），
            # 而多出来的额度会被阶段 B 用 16 天前的回填内容填满
            # （那些内容落在时效窗口之外，白取一趟）。
            #
            # ⚠️ 重试**计入 `pages`**：不这么做的话"翻页上限"就管不住
            # 真实的上游调用次数（5 页 × 最多 3 次 = 15 次）。计入之后
            # 一轮的总调用次数**硬上限就是 `max_pages`**。
            if not page:
                for _ in range(_EMPTY_RETRY):
                    if pages >= max_pages:
                        break
                    pages += 1
                    page = fetch_topics(limit=PAGE_SIZE, end_time=back)
                    if page:
                        logger.info("增量第 %d 页为空（数据源抖动）后重试成功",
                                    pages)
                        break

            if not page:
                if back is None:
                    # ★★ 第一页空 **≠** 追平 —— 漏了这一句会让整个来源从页面上消失。
                    #
                    # 数据源会**偶发返回空页**（约 1/6，阶段 B 里早就记过这条实测：
                    # 同一个 `end_time` 连续调 6 次，try4 返回 0 条、try5 又 30 条）。
                    # 阶段 A 空页的后果最重，因为它会被当成"已经追到最新"：
                    #
                    #     第一页空 → caught_up=True → 立刻转去**回填水位线那一段**
                    #     （当时水位线在 16 天前的 09-09）→ 那 30 条全部落在
                    #     `FEED_WINDOW_DAYS`（3 天）时效窗口之外被丢弃
                    #     → 页面上「券商作文」**整块消失**，只剩快讯；
                    #     而缺口文案写着"券商作文最近 3 天无更新（已归档 N 条过期内容）"
                    #     —— 与事实正好相反（真实原因是"最新那一页这次没拿到"）。
                    #
                    # 用户的感受就是"刷几次，券商作文时有时无"，而这条链路上没有
                    # 任何一处会报错。所以重试到底仍为空时：**如实报缺口**，
                    # 并且**不去回填**（回填那一段必然在窗口外，取回来也只会被丢掉）。
                    upstream_empty = True
                else:
                    caught_up = True     # 翻到**更早的一页**是空的 = 真的到底了
                break
            for t in page:
                ts = str(getattr(t, "created_at", "") or "")
                if not ts:
                    continue
                if ts <= fresh_floor:
                    # 已处理过（或落在已回填区间）。**这一页剩下的都比它旧**，
                    # 不用继续翻 —— 新内容已经在手上了。
                    caught_up = True
                    break
                h = str(getattr(t, "content_hash", "") or "")
                if h:
                    if h in seen:
                        continue
                    seen.add(h)
                if len(collected) >= max_fetch:
                    # 到上限了，本页剩下的（更旧的）只能放弃 —— 记下来如实报。
                    # 这里**故意**不再往前翻：宁可丢最旧的几条，也要保证
                    # "最新的一定收到"，这正是用户要的语义。
                    skipped += 1
                    continue
                collected.append(t)
            tail = min((str(getattr(t, "created_at", "") or "")
                        for t in page if getattr(t, "created_at", None)),
                       default="")
            if caught_up or not tail:
                break
            if tail <= fresh_floor:
                caught_up = True
                break
            back = tail
            if len(page) < PAGE_SIZE:
                caught_up = True
                break
    else:
        # 首次运行（游标为空）：从最新往回取一页即可，`newest` 会把它固定住
        page = fetch_topics(limit=PAGE_SIZE, end_time=None)
        pages = 1
        for t in page:
            h = str(getattr(t, "content_hash", "") or "")
            if h:
                seen.add(h)
            collected.append(t)
        caught_up = True

    # 空档没补完（翻页/条数上限）：如实记缺口，下次继续。
    # **不**把水位线前移 —— 那会把没取到的那段标成"已处理"，永久丢内容。
    gap_unfilled = bool(fresh_floor) and not caught_up
    if gap_unfilled:
        truncated = True

    # ══════════════════════════════════════════════════════════════
    # 阶段 B：历史回填（从水位线继续往回扩，扩展"已处理区间"的下界）
    # ══════════════════════════════════════════════════════════════
    #
    # 只在**空档已补齐**时才做：空档都没补完就继续往下挖，
    # 只会让"已处理区间"里留一个洞（而水位线是要往前推的）。
    back = watermark or ""           # 连续回取的起点
    while (watermark and caught_up and not upstream_empty
           and pages < max_pages and len(collected) < max_fetch):
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

    # ★ "最新那一页没取到"必须也置 `truncated`：否则调用方**一个缺口都不报**，
    #   用户看到的是内容凭空少了一块 —— 静默消失是本项目明令禁止的形态。
    if upstream_empty:
        truncated = True

    collected = collected[:max_fetch]

    # ── 新水位线 = 已处理区间的**最老**边界 ──
    #
    # ⚠️ 只能**往更老走，绝不前移** —— 原实现写的是 `new_watermark = oldest`
    # （这批最老的一条），在"这批全部新于水位线"时会**把水位线往后推**：
    #
    #     水位线 09-13 → 阶段 A 补到今天 → oldest = 09-25
    #     → 水位线变成 09-25 → [09-13, 09-25] 被标成"已处理"
    #     → 而它**从来没被取过** → 永久静默丢 12 天内容
    #
    # 阶段 A 上线后"这批全比水位线新"变成**常态**，所以这个 bug 必然会被触发。
    # 正确写法就是取两者更老的那个：区间只会**扩大**，不会跳过任何一段。
    new_watermark = watermark
    if collected:
        oldest = min((t.created_at for t in collected if t.created_at),
                     default="")
        if oldest:
            new_watermark = min(oldest, watermark) if watermark else oldest

    # ── 高水位线 = 本次取到的**最新**那条（跨轮去重靠它，必须前移）──
    new_newest = newest
    if collected:
        latest = max((t.created_at for t in collected if t.created_at),
                     default="")
        if latest and latest > new_newest:
            new_newest = latest

    gap = ""
    if skipped > 0:
        # 上限导致**放弃**了最旧的几条。这是有意的取舍（保最新），但不能不说。
        gap = (f"本次新增超过单轮上限（{max_fetch} 条），"
               f"已放弃最旧的 {skipped} 条未取 —— 这些内容不会再补"
               f"（该群产量高于轮询频率，建议提高 MAX_FETCH_PER_RUN 或缩短轮询间隔）")
    if upstream_empty:
        # 措辞必须与"最近 3 天无更新"**分得开**：那是"看过了、确实没有新的"，
        # 而这里是"这一趟没拿到"。把后者说成前者，用户会以为这个来源停了。
        gap = (f"本次没取到最新的券商作文（数据源返回空，已自动重试 "
               f"{_EMPTY_RETRY} 次）—— 内容没有被删，稍后会自动再试")
    elif gap_unfilled:
        gap = ("水位线之后仍有未取内容（本次命中翻页/条数上限），"
               "下次运行继续补齐 —— 这段时间的新内容下次才会出现")
    elif truncated and not gap:
        gap = (f"回填命中上限（{pages} 页 / {len(collected)} 条），"
               f"仍有更早内容未取；下次运行会继续往前追")

    return IncrementalResult(
        topics=collected,
        watermark=new_watermark,
        pages_used=pages,
        new_count=len(collected),
        truncated=truncated,
        gap_note=gap,
        newest=new_newest,
        skipped=skipped,
        upstream_empty=upstream_empty,
    )


__all__ = [
    "CURSOR_FILE",
    "MAX_ANALYZE_PER_RUN",
    "MAX_FETCH_PER_RUN",
    "MAX_PAGES",
    "PAGE_SIZE",
    "IncrementalResult",
    "fetch_incremental",
    "load_newest",
    "load_watermark",
    "save_watermark",
]
