"""知识星球条目的**留存**（JSONL，按 `content_hash` 索引，**最多 3 天**）。

> 用户口径（2026-09-26）：
>
>   "拉高上限 到100条且落库最多保留3天。"

## 为什么必须落库（不落库时"内容会自己消失"）

情报流是**每次请求现拼**的（`build_feed` 现拉六个源 + 知识星球增量），
而知识星球那条链路每轮只取"最新 N 条帖子"。

于是窗口一滑，一条帖子就从页面上消失了 —— **不是被删了，是没被取**。
实测（2026-09-26 凌晨）：那个群当天一共 88 条帖子，而单轮只取最新 30 条
（覆盖 13:21~20:47），**00:37~13:21 那 58 条一条都取不到**；用户的原话是
"原来的信息丢那里去了"。

留存把每轮取到的条目按 `content_hash` 存下来，下一轮再**并回**流水线：

    窗口   决定"这一轮新取到什么"
    留存   决定"页面上能看到什么"

两者都留着，就不再出现"刷几次内容不一样"。

## 保留期**必须**与情报流时效窗口一致

`service.FEED_WINDOW_DAYS` = 3 天：窗口之外的条目**本来就不会出现在任何
请求里**，留着只是占空间。所以这里按 `published_at` 剪枝（不是按入库时间
`at` —— 一条 4 天前发布的帖子今天取到，明天也仍然在窗口外）。

⚠️ 这里**重复**了那个数字（同 `body_store.RETAIN_DAYS` 的处理方式）：
`service` 是聚合层，本模块是存储层，import 过来会让"窗口调了、存储跟着变"
这种**需要有人看懂再决定**的改动变成一次静默生效。测试里钉住两者一致。

## 与 `body_store` 的分工

    body_store（item_bodies.jsonl）  单条**全文**，按 hash 取，给「查看原文」用
    本模块（zsxq_items.jsonl）       条目的**可重建快照**（标题/摘要/可信度…），
                                     给"并回情报流"用

两者**同一份 3 天留存**、同一个目录，但键相同、用途不同。合并时如果
`body_store` 里还有这条的全文，会顺手补上 `extract_text` —— 否则
"留存回来的条目"喂给本地模型时只看得到 260 字的展示摘要（抽取质量下降）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 存储位置（与 `body_store` / `tone_store` 同级，已被 .gitignore 覆盖）
_STORE_DIR: Final = Path("data") / "intel"
_STORE_FILE: Final = "zsxq_items.jsonl"

#: 保留天数。**必须等于情报流的时效窗口**（`service.FEED_WINDOW_DAYS`）。
RETAIN_DAYS: Final = 3

#: 硬上限（防"某个坏源一天灌几十万条"把文件与 prune 拖垮）。
#: 实测该群约 90 条/天 → 3 天约 300 条，给 5000 是数量级余量。
MAX_ROWS: Final = 5000
MAX_BYTES: Final = 8 * 1024 * 1024

#: 内存缓存（进程内）。键 = `content_hash`，值 = 行 dict。
#:
#: 为什么要有它：`build_feed` 每轮都要"存一批 + 取一批"，而 `save_many`
#: 还要靠它判断"这条已经存过"（见模块 docstring）。没有缓存的话每次请求
#: 都要把整份文件读一遍并解析。
_CACHE: dict[str, dict[str, Any]] = {}
_LOADED = False

#: 上一次真正执行 `prune` 的时刻（单调钟，秒）。节流理由同
#: `body_store._PRUNE_EVERY_SECONDS`：prune 是"整份读出来 + 重写"，
#: 而它长在**请求路径**上（`build_feed` 每轮都会 persist 一批）。
_PRUNE_EVERY_SECONDS: Final = 600.0
_LAST_PRUNE: float = 0.0

#: 一份条目快照里**允许落库**的键（白名单）。
#:
#: 送进来的是 `build_feed` 里的 `to_public()` 产物，它身上还挂着
#: `extract_text` / `market_terms` / `highlights` 等**派生或内部**字段。
#: 整份透传等于把它们一并写进文件，将来任何一个内部键都会被它带出去 ——
#: 与 `to_public()` 同一条纪律：新键默认不存，要存必须显式加一行。
#:
#: ⚠️ **不存 `kind_label`**：那是展示名，改名必须能作用于老数据
#: （"研究笔记 → 券商作文"改过一次），所以重建时按当前 `KIND_LABELS` 现算。
_ROW_KEYS: Final[tuple[str, ...]] = (
    "title", "summary", "published_at", "kind", "source_alias",
    "platform", "codes", "industry", "agency", "rating_origin",
    #: 可信度是**当时算出来的分数**（来源轴 + 内容轴），重建时不重算 ——
    #: 来源分级表改过之后，重算会让"同一条老内容"的分数在页面上跳动。
    "credibility",
)


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load(*, root: Path | None = None, force: bool = False) -> dict[str, dict]:
    """载入全部行到内存缓存（首次调用时读文件）。

    文件不存在 / 读坏了都**返回空字典**而不是抛错：留存是"锦上添花"的数据，
    它读不到时条目照常展示（只是页面上只剩这一轮取到的那一批），
    不该让整个情报流挂掉。单行坏了跳过、不放弃整个文件。
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
                    continue
                h = str(obj.get("content_hash") or "")
                if h:
                    _CACHE[h] = obj      # 后写的覆盖先写的（同 hash 重存过）
        except OSError as exc:
            logger.warning("条目留存读取失败（按空处理）：%s", type(exc).__name__)
    _LOADED = True
    return _CACHE


def build_row(item: dict[str, Any]) -> dict[str, Any] | None:
    """一条 feed item → 可落库的行。**没有指纹时返回 `None`**。

    `None` 不是错误：收容组（`is_group`）是服务端合成的条目、没有指纹，
    跳过它就是正确行为。
    """
    h = str(item.get("content_hash") or "")
    if not h:
        return None
    row = {k: item[k] for k in _ROW_KEYS if item.get(k) is not None}
    return {"content_hash": h, "at": _now(), **row}


def save_many(rows: list[dict[str, Any]], *,
              root: Path | None = None) -> int:
    """追加写入一批行，返回**实际写入**条数（已存过的按 hash 短路）。

    短路很要紧：这条链路每轮都把同一批"最新 N 条"再送一次，
    不短路的话文件一天涨十倍，而内容一个字都没变。
    """
    data = load(root=root)
    out: list[dict[str, Any]] = []
    for r in rows:
        h = str(r.get("content_hash") or "")
        if not h or h in data:
            continue
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
            root: Path | None = None) -> dict[str, int]:
    """把一批条目存下来并**按需**清理过期行，返回统计。

    ⚠️ 它长在请求路径上（`build_feed` 每轮都会调），所以 prune **不是每次都跑**
    —— 判据见 `_prune_due()`：要么确实有过期行/超上限，要么距上次清理
    超过节流窗口。与 `body_store.persist` 同一套取舍。

    ⚠️ 整段都不抛：留存失败不该让这一页打不出来（与"全文落库失败"同级）。
    """
    try:
        rows = [r for r in (build_row(it) for it in items) if r]
        written = save_many(rows, root=root)
        pruned = prune(root=root) if _prune_due(root=root) else 0
        return {"written": written, "pruned": pruned, "considered": len(items)}
    except Exception as exc:  # noqa: BLE001 存储失败绝不冒泡到请求
        logger.warning("条目留存失败（忽略本轮）：%s", type(exc).__name__)
        return {"written": 0, "pruned": 0, "considered": len(items)}


def _prune_due(*, root: Path | None = None) -> bool:
    """现在**该不该**真的跑一趟 `prune`（廉价判据，不读文件）。

    判据：要么缓存里确实有过期行，要么距上次清理已超过节流窗口。
    时间戳读不懂的行按"**没过期**"处理（保守保留）—— 与 `prune` 里的取舍
    一致：宁可多留一条，也不要因为读不懂时间就删掉可能还新的内容。
    """
    global _LAST_PRUNE
    now = time.monotonic()
    if _LAST_PRUNE and now - _LAST_PRUNE < _PRUNE_EVERY_SECONDS:
        cutoff = _cutoff(RETAIN_DAYS)
        for row in _CACHE.values():
            try:
                ts = datetime.fromisoformat(str(row.get("published_at") or ""))
            except ValueError:
                continue                 # 读不懂 = 保守保留，不触发清理
            if ts < cutoff:
                break
        else:
            return False                 # 没有过期行，且还在节流窗口内
    _LAST_PRUNE = now
    return True


def _cutoff(retain_days: int) -> datetime:
    return datetime.now(timezone.utc).astimezone() - timedelta(
        days=max(1, retain_days))


def prune(*, root: Path | None = None, retain_days: int = RETAIN_DAYS,
          max_rows: int = MAX_ROWS, max_bytes: int = MAX_BYTES) -> int:
    """删掉过期行（按 `published_at`）+ 执行硬上限，返回删除条数。

    ## 为什么按 `published_at` 而不是入库时间 `at`

    情报流的时效窗口（`service.FEED_WINDOW_DAYS`）判的是 `published_at`。
    按 `at` 剪枝会留下"4 天前发布、今天才取到"的行 —— 它下一次请求必然
    被窗口丢掉，只是白占地方。

    ## 为什么整份重写而不是原地删

    JSONL 无法原地删除，而这份文件量级可控（3 天 + 条数上限）。写入走
    `tmp` + `replace`（**原子替换**）：直接覆写时进程写到一半被杀会留下
    半份文件 —— 而"半份 JSONL"的表现是后面所有行都读不出来，
    看起来像"留存功能突然全坏了"。
    """
    p = store_path(root)
    if not p.exists():
        return 0
    cutoff = _cutoff(retain_days)
    removed = 0
    kept: list[tuple[str, str]] = []          # (原始行, 排序键)
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
            removed += 1                      # 坏行直接丢（它已经读不出来了）
            continue
        try:
            ts = datetime.fromisoformat(str(obj.get("published_at") or ""))
        except ValueError:
            # 时间戳坏了**保守保留**。它在硬上限里按"最旧"处理 ——
            # 与空串同档，所以极端情况下它会**先**被丢，这是有意的：
            # 读不懂时间的行不可能比读得懂的更值得留。
            kept.append((s, ""))
            continue
        if ts >= cutoff:
            kept.append((s, ts.isoformat()))
        else:
            removed += 1

    def _bytes(rows: list[tuple[str, str]]) -> int:
        return sum(len(s.encode("utf-8")) for s, _ in rows)

    if len(kept) > max_rows or _bytes(kept) > max_bytes:
        kept.sort(key=lambda kv: kv[1])       # 空键排最前 = 最先被丢
        while kept and (len(kept) > max_rows or _bytes(kept) > max_bytes):
            kept.pop(0)
            removed += 1
        logger.warning("条目留存超过硬上限（%d 行 / %d 字节），已按最旧优先清理",
                       max_rows, max_bytes)

    if not removed:
        return 0
    _write_atomic(p, [s for s, _ in kept])
    _CACHE.clear()
    for s in (x for x, _ in kept):
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        h = str(obj.get("content_hash") or "")
        if h:
            _CACHE[h] = obj
    return removed


def _write_atomic(path: Path, lines: list[str]) -> None:
    """整份重写（tmp + 原子替换），失败不留半份文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("条目留存重写失败（保留原文件）：%s", type(exc).__name__)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def reset_cache() -> None:
    """清空内存缓存（测试用；生产没有调用点）。"""
    global _LOADED, _LAST_PRUNE
    _CACHE.clear()
    _LOADED = False
    _LAST_PRUNE = 0.0


__all__ = [
    "MAX_BYTES",
    "MAX_ROWS",
    "RETAIN_DAYS",
    "build_row",
    "load",
    "persist",
    "prune",
    "reset_cache",
    "save_many",
    "store_path",
]
