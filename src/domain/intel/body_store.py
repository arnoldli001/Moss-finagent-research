"""条目**全文**的持久化存储（JSONL，按 `content_hash` 索引）。

## 为什么需要这份存储（用户口径 2026-10-01）

> "点击可以看全文。"

展示契约把 `summary` 截到 260 字（`intel_sources.SUMMARY_MAX_BY_KIND`）——
移动端一条 3400 字的笔记会占满十屏。但券商作文的价值恰恰在**后半段**
（"综上，推荐 XX，目标价…"），所以"截断展示"必须配一条"按需取全文"的路。

## 为什么不在接口里现算（与 `tone_store` 同一条纪律）

`build_feed` 是**每次请求现拼、不落库**的，而全文只存在于采集那一次的
`extract_text` 里（进程内字段，`IntelFeed.to_public()` 会剥掉它）。
接口要拿全文，只能**事先存下来**：

    采集 + 抽取 → 本存储（按 `content_hash`）
    读取（`GET /intel/item/{hash}`）→ 只查文件，O(1) 命中，**零模型调用**

⚠️ 这条链路上**没有**任何模型调用：存的是清洗后的原文，读的是文件。

## ★ 落库点有两个：定时任务 **和** `build_feed`（2026-10-01 第六轮）

原来只有 `_intel_tone_extract`（2 小时一次）写这里。用户报障
「点击看全文 → 全文读取失败」时，`item_bodies.jsonl` **根本不存在** ——
而那条链路上"没存过"与"过期了"在接口看来是同一件事（都是 404），
所以用户看到的是"这个功能坏了"。

现在的做法是**两处都写**，它们不冲突（同一个 `save_many` 短路 + 原子写）：

    定时任务   每 2 小时一次，兜住"没人打开页面时也要有全文"
    build_feed 每次请求，兜住"定时任务没跑/刚跑过之后新来的条目"

⚠️ 为什么这**不违反**"请求路径不调模型"：存全文是**一次文件追加**，
判据是 `content_hash`（内容指纹，算它不花钱），与模型无关。
真正被禁止的是"在请求里做语义抽取"（本地 8B ~770ms/条）。
实测成本见 `_PRUNE_EVERY_SECONDS` 的注释与本模块的"幂等"说明。

⚠️ **重复调用不能涨文件**：`save_many` 按 `content_hash` 短路，
而 `content_hash` 是内容指纹 ⇒ 同一个 hash 的文本必然相同。
所以"同一批 500 条每次请求都送来"这个最坏情况，落库成本是
**一次字典查找 × 500**（构建行之前就短路，见 `_new_hash`）。

## 存什么 / 不存什么

存（白名单，见 `build_row`）：

    content_hash / at / title / text / published_at / kind

**不存 `kind_label`** —— 标签是展示层的事（`KIND_LABELS`），存下来就会
在改名时留下历史值。用户刚刚把 `research_note` 的展示名从"研究笔记"
改成"券商作文"：若标签随行落库，老数据会永远显示旧名字，而新老数据
混在一页里，看起来像改名没生效。**倾向也不存** —— 它在 `tone_store` 里，
读取侧按同一个 `content_hash` join（见 `api/routes/intel.py`）。

**不存任何来源标识**：没有 `source_alias` / `platform` / `source_name` /
URL。这份文件即使被读到也不泄漏渠道（与 `tone_store` 同一条纪律）。

## 文本从哪来：`extract_text` 优先，退回展示摘要

  · 知识星球那条路有**清洗后的全文**（`service.extraction_input`），用它；
  · 其余来源（快讯/研报/政策）**上游只给到那么长**，`summary` 就是全部正文 ——
    存它，界面上点开看到的就是"我们手里有的全部内容"。

## 有界：时间 + 条数 + 字节数，三道闸门

    时间   `RETAIN_DAYS` = 情报流的时效窗口（`service.FEED_WINDOW_DAYS`，3 天）
    条数   `MAX_ROWS`
    字节   `MAX_BYTES`

只留时间闸门是不够的：某个坏掉的源可能一天塞进几十万条，
而 prune 是"整份重写"，文件涨到几百 MB 后每次重写都会把接口拖慢。
所以另两道闸门在**同一趟 prune** 里生效（见 `prune`）。

⚠️ **重复保存要按 `content_hash` 短路**（`save_many`）：这条链路每 2 小时
跑一次，同一批条目会被反复送来；append-only 不短路的话，一天 12 轮 ×
几百条就是几千行重复，文件与 prune 成本都白涨十倍。
`content_hash` 是**内容指纹** ⇒ 同一个 hash 的文本必然相同，短路是安全的。

## 向后兼容

老行（或将来缺键的行）读取侧一律 `hit.get(...)`，缺键给空值（`view`）。
**不许**直接下标 —— 与 `tone_store` 的"两种格式的行"同一个坑。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 存储位置（与 `tone_store` 同级，已被 .gitignore 覆盖）
_STORE_DIR: Final = Path("data") / "intel"
_STORE_FILE: Final = "item_bodies.jsonl"

#: 保留天数。**必须等于情报流的时效窗口**（`service.FEED_WINDOW_DAYS`）：
#: 窗口外的条目不会再出现在任何请求里，留着只是占空间。
#:
#: ⚠️ 这里**重复**了那个数字，是刻意的（同 `alert_rules.SUMMARY_CLIP` 的
#: 处理方式）：`service` 是聚合层，本模块是存储层，import 过来会让
#: "窗口调了、存储跟着变"这种**需要有人看懂再决定**的改动变成一次静默生效。
#: 所以加一条测试钉住两者一致（`test_intel_label_and_fulltext.py`）。
RETAIN_DAYS: Final = 3

#: 硬上限（防"某个坏源一天灌几十万条"把文件与 prune 拖垮）。
MAX_ROWS: Final = 20000
MAX_BYTES: Final = 32 * 1024 * 1024

#: 单条全文的字符上限。实测最长笔记 3452 字；给 20000 是**防病态输入**
#: （上游偶发把整页 HTML 塞进正文），不是正常的截断路径。
#:
#: ⚠️ 截断时**不**加省略标记：`extraction_text` 的契约是"不改写正文"
#: （`phrases` 要逐字可核对）。这里只是不存超出部分，正文本身不动。
MAX_TEXT_CHARS: Final = 20000

#: 内存缓存（进程内）。键 = `content_hash`，值 = 行 dict。
#:
#: 为什么要有它：接口每次点击都要按 hash 查一次，而 `save_many` 也要靠它
#: 判断"这条已经存过"（见模块 docstring）。
_CACHE: dict[str, dict[str, Any]] = {}
_LOADED = False

#: 上一次真正执行 `prune` 的时刻（单调钟，秒）。
#:
#: ## 为什么需要它（2026-10-01，第六轮：落库搬进请求路径）
#:
#: `build_feed` 现在**每次请求**都会调 `persist`（见 `service.build_feed`
#: 的"全文落库"一段）。而 `prune` 是"整份文件读出来 + 重写" —— 文件到
#: 上限（`MAX_BYTES` 32 MB）时一次就是几十毫秒，而请求路径上每 5 分钟
#: （`FEED_CACHE_TTL`）就会走一遍。没有这个节流阀，"加一个顺手的清理"
#: 会变成"接口随文件增长而变慢"，而那种慢在压测里看不出来（空文件时不慢）。
#:
#: 实测（本机 SSD，3663 条 / 1.45 MB）：读+解析 12 ms，整份重写 9 ms。
#: 文件涨到 32 MB 时按比例约 260 ms —— 那已经是一次可感知的卡顿。
_PRUNE_EVERY_SECONDS: Final = 600.0
_LAST_PRUNE: float = 0.0


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load(*, root: Path | None = None, force: bool = False) -> dict[str, dict]:
    """载入全部行到内存缓存（首次调用时读文件）。

    文件不存在 / 读坏了都**返回空字典**而不是抛错 —— 全文是"锦上添花"的
    数据：它读不到时条目照常展示（只是点开提示"全文暂不可用"），
    不该让整个情报流挂掉。
    """
    global _LOADED
    if _LOADED and not force:
        return _CACHE
    _CACHE.clear()
    p = store_path(root)
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue          # 单行坏了跳过，不放弃整个文件
                h = str(obj.get("content_hash") or "")
                if h:
                    _CACHE[h] = obj   # 后写的覆盖先写的（同 hash 重存过）
        except OSError as exc:
            logger.warning("全文存储读取失败（按空处理）：%s", type(exc).__name__)
    _LOADED = True
    return _CACHE


def get(content_hash: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """按内容指纹取一条全文。**没有就返回 `None`**（不编、不回退到摘要）。"""
    if not content_hash:
        return None
    return load(root=root).get(content_hash)


def view(hit: dict[str, Any] | None) -> dict[str, Any]:
    """把一条存储行摊成**读取侧的稳定形状**（缺键给空值，不抛）。

    与 `tone_store.view` 同一个理由：这份存储里可能出现缺键的行
    （老格式 / 别处写坏的），而"缺键"与"空值"在代码里长得一样。
    集中在这里摊平，读取方拿到的形状永远一样。
    `None` 入参（没存过）也返回同一个形状 —— 调用方靠它判"有没有"。
    """
    h = hit or {}
    return {
        "content_hash": str(h.get("content_hash") or ""),
        "title": str(h.get("title") or ""),
        "text": str(h.get("text") or ""),
        "published_at": str(h.get("published_at") or ""),
        #: 内部 kind（`research_note` 等）。标签**不存**，由调用方用
        #: `KIND_LABELS` 现算 —— 否则改名改不动老数据（见模块 docstring）。
        "kind": str(h.get("kind") or ""),
    }


#: 一份全文里**允许落库**的键（白名单）。
#:
#: ## 为什么是白名单，而不是 `**item` 整份透传
#:
#: 送进来的是 `build_feed` 的 item —— 它身上挂着 `extract_text` /
#: `market_terms` / `credibility` / `tone`（十几键）等一大堆东西。
#: 整份透传等于把**内部字段**（包括全文抽取入参本身）原样写进一份
#: "会被接口按 hash 读出来"的文件里，将来任何一个内部键都会被它带出去。
#: 白名单下新键默认不存，必须显式加一行 —— 与 `to_public()` 同一条纪律。
_ROW_KEYS: Final[tuple[str, ...]] = (
    "title", "text", "published_at", "kind",
)


def build_row(item: dict[str, Any]) -> dict[str, Any] | None:
    """一条 feed item → 可落库的行。**没有指纹或没有正文时返回 `None`**。

    `None` 不是错误：收容组（`is_group`）是服务端合成的条目，它没有
    `content_hash`（空串）也没有原文，跳过它就是正确行为。

    ⚠️ **调用前先按指纹短路**（见 `persist`）：本函数会做一次
    `strip()` + 一次 20000 字切片，在请求路径上对"已经存过的 500 条"
    白做一遍就是几百次无用字符串操作。
    """
    h = str(item.get("content_hash") or "")
    if not h:
        return None
    # `extract_text`（清洗后的全文）优先；没有就退回展示摘要 ——
    # 对绝大数来源来说，摘要就是上游给的**全部**正文（见模块 docstring）。
    text = str(item.get("extract_text") or item.get("summary") or "").strip()
    if not text:
        return None
    row = {k: item[k] for k in _ROW_KEYS if item.get(k) is not None}
    row["text"] = text[:MAX_TEXT_CHARS]
    return {"content_hash": h, "at": _now(), **row}


def save_many(rows: list[dict[str, Any]], *,
              root: Path | None = None) -> int:
    """追加写入一批行，返回**实际写入**条数（已存过的按 hash 短路）。

    短路见模块 docstring：这条链路每 2 小时把同一批条目再送一次，
    不短路的话文件一天涨十倍，而内容一个字都没变。
    """
    data = load(root=root)
    out: list[dict[str, Any]] = []
    for r in rows:
        h = str(r.get("content_hash") or "")
        text = str(r.get("text") or "")
        if not h or not text:
            continue                     # 没有 hash 查不回来，没有正文没意义
        if h in data:
            continue                     # 同指纹 = 同内容，不重复写
        out.append(r)
    if not out:
        return 0

    p = store_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for r in out:
        _CACHE[str(r["content_hash"])] = r
    return len(out)


def persist(items: list[dict[str, Any]], *,
            root: Path | None = None,
            prune_now: bool = True) -> dict[str, int]:
    """把一批 feed item 的全文存下来并顺带清理过期行，返回统计。

    单独一个入口是给调度任务用的（`src/scheduler/jobs.py`）：它需要在
    **同一步**里完成"存 + 清"，否则调用方很容易只写不清理，
    而"只写不清"的表现要几天后磁盘涨满才看得出来。

    ## `prune_now=False`：请求路径那一档（2026-10-01 第六轮）

    `build_feed` 现在每次请求都调它。默认的"每次都整份重写一遍"在空文件上
    看不出来，但文件涨到 `MAX_BYTES` 时是每次请求几十到几百毫秒
    （见 `_PRUNE_EVERY_SECONDS` 的实测数字）。所以请求路径传 `False`，
    由下面两道**廉价**判据决定要不要真的清：

      ① 有没有过期行 / 超上限 —— 拿内存缓存扫一遍，通常 O(1)（缓存很小）
      ② 距上次清理够不够 `_PRUNE_EVERY_SECONDS`

    两道都过才清。⚠️ **不是"请求路径永不清"**：只写不清会让文件无限涨、
    而"文件涨满"的表现是"接口越来越慢"，查不出是哪来的数据
    （与调度任务那边同一条纪律）。
    """
    rows = [r for r in (build_row(it) for it in items
                        if _new_hash(it)) if r]
    written = save_many(rows, root=root)
    if not prune_now and not _prune_due(root=root):
        return {"written": written, "pruned": 0, "considered": len(items)}
    pruned = prune(root=root)
    return {"written": written, "pruned": pruned, "considered": len(items)}


def _new_hash(item: dict[str, Any]) -> bool:
    """这条 item 的指纹**还没存过**吗（`build_row` 之前的廉价前置闸）。

    ⚠️ 它必须与 `build_row` 判"指纹为空就跳过"完全一致 —— 不一致的表现是
    "没有指纹的条目被送去 build_row 又返回 None"，只是白花一点时间；
    反过来（这里放过、那边也放过）才会出问题，而那种情况不存在。
    """
    h = str(item.get("content_hash") or "")
    return bool(h) and h not in load()


def _prune_due(*, root: Path | None = None) -> bool:
    """现在**该不该**真的跑一趟 `prune`（廉价判据，不读文件）。

    判据见 `_PRUNE_EVERY_SECONDS`：要么有东西真的过期/超限，要么距上次
    清理已超过节流窗口。两个条件都用到缓存与两个计数器，成本可忽略。

    ⚠️ 阈值**在函数体里读模块常量**，不写成 `def` 默认值：写成默认值会在
    **导入时**把数字钉死，此后改 `RETAIN_DAYS` / `MAX_BYTES`（调参、单测、
    `monkeypatch`）对它毫无影响 —— 与 `tone.segment_text` 的
    `max_segments` 那次实测踩到的坑是同一个（"上限改了但没生效，没有任何报错"）。

    ⚠️ 时间戳**读不懂的行按"过期"处理**（宁可多清一次，也不要因为读不懂
    时间就把可能已经该删的行永远留着）—— 与 `prune` 里"读不懂就保守保留"
    是**相反**的取舍，因为这里的代价只是"多跑一次清理"，不是"误删数据"。
    """
    global _LAST_PRUNE
    now = time.monotonic()
    if _LAST_PRUNE and now - _LAST_PRUNE < _PRUNE_EVERY_SECONDS:
        # 节流窗口内：只有**确实有事**才破例（下面按缓存扫一遍）
        cutoff = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=max(1, RETAIN_DAYS)))
        overdue = 0
        for row in _CACHE.values():
            try:
                at = datetime.fromisoformat(str(row.get("at") or ""))
            except ValueError:
                overdue += 1
                continue
            if at < cutoff:
                overdue += 1
        if overdue:
            _LAST_PRUNE = now
            return True
        if len(_CACHE) > MAX_ROWS:
            _LAST_PRUNE = now
            return True
        if sum(len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
               for r in _CACHE.values()) > MAX_BYTES:
            _LAST_PRUNE = now
            return True
        return False
    _LAST_PRUNE = now
    return True


def prune(*, root: Path | None = None, retain_days: int = RETAIN_DAYS,
          max_rows: int = MAX_ROWS, max_bytes: int = MAX_BYTES) -> int:
    """删掉过期行（按 `at`）+ 执行硬上限，返回删除条数。

    为什么按**整体重写**而不是原地删：JSONL 无法原地删除，而这份文件
    量级可控（3 天窗口 + 条数上限），重写成本可忽略。
    写入走 `tmp` + `replace`（**原子替换**）：直接覆写时，进程在写到一半
    被杀会留下半份文件 —— 而"半份 JSONL"的表现是后面所有行都读不出来，
    看起来像"全文功能突然全坏了"。
    """
    p = store_path(root)
    if not p.exists():
        return 0
    cutoff = (datetime.now(timezone.utc).astimezone()
              - timedelta(days=max(1, retain_days)))
    kept: list[tuple[str, str]] = []      # (原始行, at)
    removed = 0
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            removed += 1                  # 坏行直接丢（它已经读不出来了）
            continue
        try:
            at = datetime.fromisoformat(str(obj.get("at") or ""))
        except ValueError:
            # 时间戳坏了**保守保留**（宁可多留一条，也不要因为读不懂时间
            # 就把可能还新的内容删掉）。它在下面的硬上限里按"最旧"处理。
            kept.append((s, ""))
            continue
        if at >= cutoff:
            kept.append((s, at.isoformat()))
        else:
            removed += 1

    # ── 硬上限：从最旧的开始丢（没有时间戳的按最旧处理）──
    def _bytes(rows: list[tuple[str, str]]) -> int:
        return sum(len(s.encode("utf-8")) for s, _ in rows)

    if len(kept) > max_rows or _bytes(kept) > max_bytes:
        kept.sort(key=lambda kv: kv[1])   # 空 at 排最前 = 最先被丢
        while kept and (len(kept) > max_rows or _bytes(kept) > max_bytes):
            kept.pop(0)
            removed += 1
        if removed:
            logger.warning("全文存储超过硬上限（%d 行 / %d 字节），"
                           "已按最旧优先清理 %d 条",
                           max_rows, max_bytes, removed)

    if removed:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("\n".join(s for s, _ in kept) + ("\n" if kept else ""),
                       encoding="utf-8")
        tmp.replace(p)
        load(root=root, force=True)
    return removed


def stats(*, root: Path | None = None) -> dict[str, Any]:
    """存储概况（供管理员页与排障用）。"""
    data = load(root=root)
    p = store_path(root)
    return {
        "count": len(data),
        "path": str(p),
        "bytes": p.stat().st_size if p.exists() else 0,
        "retain_days": RETAIN_DAYS,
        "max_rows": MAX_ROWS,
        "max_bytes": MAX_BYTES,
    }


__all__ = [
    "MAX_BYTES",
    "MAX_ROWS",
    "MAX_TEXT_CHARS",
    "RETAIN_DAYS",
    "build_row",
    "get",
    "load",
    "persist",
    "prune",
    "save_many",
    "stats",
    "store_path",
    "view",
]
