"""原文倾向的**抽取结果存储**（JSONL，按 `content_hash` 索引）。

## 为什么抽取要落库，而不是在接口里现算

本地模型单条实测 ~770ms。情报流一页 60 条，若在 `/intel/feed` 里逐条跑，
**会加 46 秒延迟** —— 接口直接从"秒回"退化成"超时"。

所以拆成两段，与既有设计一致（见 `intel.py` 模块 docstring 的
"为什么接口不做 LLM 分析"）：

    抽取（慢）  定时任务，2 小时一次，落这里
    读取（快）  接口只查这份文件，O(1) 按 hash 命中

## 为什么是 JSONL 而不是数据库表

  · 这份数据的读写模式极简：**按 `content_hash` 追加、按 hash 查**
  · 情报条目是**天级生命周期**（过期就不再看），不需要事务、不需要 join
  · 落 JSONL 可以 `tail -f` 直接看，排障比查表快
  · 保留策略简单：超过 N 天整体重写一次即可

## 存储的字段**只有倾向本身**，不含来源标识

`content_hash` 是内容指纹（不含来源），`tone`/`phrases`/`codes` 都来自
原文。**没有任何来源字段** —— 这份文件即使被读到也不泄漏渠道。

## ⚠️ 加字段必须**向后兼容**（2026-09-25 第二轮）

第二轮往里加了 `summary` / `events` / `bullish` / `bearish`
（原文里明写的利好/利空行业与个股 + 关键事件）。

**已经落库的 176 行没有这些键**（那是第一轮的格式）。所以：

  · 读取侧一律 `hit.get("summary") or ""`、`hit.get("bullish") or {}` ——
    **不许** `hit["summary"]`；缺键是**正常状态**（老行），不是损坏
  · 写入侧走 `build_row` 的白名单，不整份透传 —— 见那个函数的说明
  · 不需要回填/迁移：旧行缺的就是"那轮没抽过"，接口少显示一块，
    下一轮抽到同一条（`content_hash` 变了或 force 重抽）自然补上。
    为几天的数据写迁移脚本，成本比收益高。

第三轮（2026-09-25）同理：方向桶里多了 `boards`（概念板块名 + 主线挖掘的
板块代码），老行没有这个键 —— 读取侧由 `_side_view` 统一补成空列表。

第四轮（2026-09-25）加了**分段抽取的来源说明**：`segments` / `calls` /
`skipped_segments`。它们是"这个结果是怎么来的"（内部过程），不是"原文讲了什么"：

    segments          本条实际处理的段数（= 模型调用次数，短文本为 1）
    calls             本条**实际发起**的模型调用次数（短文本恒为 0）
    skipped_segments  因段数上限跳过的段区间（空串 = 一段没跳）

⚠️ 老行同样没有这三个键，读取侧给**保守默认值**（1 段 / 0 次调用 / 没跳过），
而不是 `0 段` —— `segments=0` 会让界面显示"这条没有段"，
而事实是"这条是老格式，我们不知道它切了几段"。默认值必须选那个
**不会误导人**的：至少一段。
"""


from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 存储位置（与 `data/intel/` 下的游标同级，已被 .gitignore 覆盖）
_STORE_DIR: Final = Path("data") / "intel"
_STORE_FILE: Final = "tone_results.jsonl"

#: 保留天数。情报条目的生命周期是天级 —— 超过这个天数的倾向结果
#: 不会再被任何请求命中（池子只取最近 500 条）。
RETAIN_DAYS: Final = 14

#: 内存缓存（进程内）。键 = `content_hash`，值 = 结果 dict。
#:
#: 为什么要它：接口每次请求都要按 hash 查 60 次，每次都读文件太浪费。
#: 缓存在**进程启动时**一次载入，之后只追加 —— 文件是追加写的，
#: 所以缓存不会与文件不一致（除非有别的进程在写，那种情况下
#: `invalidate()` 会被调度任务调用）。
_CACHE: dict[str, dict[str, Any]] = {}
_LOADED = False


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load(*, root: Path | None = None, force: bool = False) -> dict[str, dict]:
    """载入全部结果到内存缓存（首次调用时读文件）。

    文件不存在 / 读坏了都**返回空字典**而不是抛错 ——
    倾向是"锦上添花"的数据，它缺失时情报流应当照常工作（用规则层兜底），
    不该让整个页面挂掉。
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
                    # 后写的覆盖先写的（同一 hash 可能被重抽过）
                    _CACHE[h] = obj
        except OSError as exc:
            logger.warning("倾向结果读取失败（按空处理）：%s", type(exc).__name__)
    _LOADED = True
    return _CACHE


def get(content_hash: str, *, root: Path | None = None) -> dict[str, Any] | None:
    """按内容指纹取一条倾向结果。**没抽过返回 `None`**（不编造）。

    ⚠️ 老行（第一轮格式）**没有** `summary`/`events`/`bullish`/`bearish`
    四个键，调用方必须用 `view()` 取字段 —— 直接下标会 KeyError。
    """
    if not content_hash:
        return None
    return load(root=root).get(content_hash)


def view(hit: dict[str, Any] | None) -> dict[str, Any]:
    """把一条存储行摊成**读取侧的稳定形状**（缺键给空值，不抛）。

    ## 为什么需要它（而不是让每个读取方自己 `.get(k) or []`）

    这份存储里**同时存在两种格式的行**：第一轮落的（只有 tone/phrases/
    codes/…）与第二轮落的（多了 summary/events/bullish/bearish）。
    老行缺键是**正常状态**，不是损坏 —— 但"缺键"与"空值"在代码里
    长得一样，于是每加一个字段，每个读取点都要记得写一次 `or []`。
    漏一处就是线上 KeyError（把整个情报流打挂），而它只在
    "用户翻到一条几天前的老数据"时才复现。

    集中在这里摊平，读取方拿到的形状永远一样：
    `hit = view(tone_store.get(h))`。

    `None` 入参（没抽过）也返回同一个形状，且 `has_tone=False` ——
    "没抽过"与"抽过但未定"在存储里是两种状态，但在**读取侧的形状**
    上两者都只能给"没有倾向字段"，界面靠 `has_tone` 区分
    （见 `service.build_feed`）。
    """
    h = hit or {}
    return {
        "tone": str(h.get("tone") or "未定"),
        "has_tone": bool(h.get("has_tone")),
        "neutral": bool(h.get("neutral")),
        "phrases": list(h.get("phrases") or []),
        "codes": list(h.get("codes") or []),
        "confidence": h.get("confidence"),
        "source": str(h.get("source") or ""),
        "explain": str(h.get("explain") or ""),
        # ── 第二轮字段：老行没有 → 空值（界面显示"这块没有"）──
        "summary": str(h.get("summary") or ""),
        "events": list(h.get("events") or []),
        "bullish": _side_view(h.get("bullish")),
        "bearish": _side_view(h.get("bearish")),
        # ── 第四轮字段：分段来源说明（老行没有 → 保守默认值）──
        # ⚠️ 默认值**不能**是 0 段：那会被读成"这条没抽过"。
        #    老行（第一~三轮）都是单次调用，所以"至少 1 段"才是事实。
        "segments": int(h.get("segments") or 1),
        "calls": int(h.get("calls") or 0),
        "skipped_segments": str(h.get("skipped_segments") or ""),
        # ── 第五轮字段：券商名 / 分析师名（模型给的，**只作审计**）──
        # ⚠️ 老行没有这两个键 → 空列表（正常状态，不是损坏）。
        #    ⚠️ 它们**不上屏**：界面上的"机构：X / 分析师：X"由
        #    `alert_rules` 的确定性扫描保证（见 `tone.validate_people`）。
        #    这里透传是为了让"模型到底认出了谁"可回看（扩名单时的依据）。
        "brokers": [str(x) for x in (h.get("brokers") or []) if str(x)],
        "analysts": [str(x) for x in (h.get("analysts") or []) if str(x)],
    }


def _side_view(side: Any) -> dict[str, Any]:
    """方向桶的读取侧形状（老行 / 坏行一律给空桶）。"""
    s = side if isinstance(side, dict) else {}
    stocks: list[dict[str, Any]] = []
    for raw in (s.get("stocks") or []):
        if not isinstance(raw, dict):
            continue
        stocks.append({
            "name": str(raw.get("name") or ""),
            "code": str(raw.get("code") or ""),
            "count": int(raw.get("count") or 0),
        })
    # 第三轮字段：与 `industries` 同一批板块 + 主线挖掘的板块代码。
    # ⚠️ 必须在这里透传：读取方拿到的是本函数的**投影**，漏一个键的表现是
    # "抽到了、落库了、接口永远看不到"，不会有任何报错（同 `to_public` 那条纪律）。
    boards: list[dict[str, Any]] = []
    for raw in (s.get("boards") or []):
        if not isinstance(raw, dict):
            continue
        boards.append({
            "name": str(raw.get("name") or ""),
            "code": str(raw.get("code") or ""),
        })
    return {
        "industries": [str(x) for x in (s.get("industries") or []) if str(x)],
        "stocks": stocks,
        "boards": boards,
        "count": int(s.get("count") or (len(s.get("industries") or []) + len(stocks))),
    }


#: 一份结果里**允许落库**的键（白名单）。
#:
#: ## 为什么是白名单，而不是 `**r` 整份透传
#:
#: 第一轮写的是 `{"content_hash": h, "at": ..., **r}` —— 调用方给什么就存什么。
#: 那个写法在这份数据上有个具体危害：`tone_job` 的中间结果里带着
#: `rejected`（被拦掉的幻觉原文）。**被拦掉的东西不该进持久层** ——
#: 这份文件是排障时要 `tail -f` 直接看的（模块 docstring 里写了这条优点），
#: 幻觉文本躺在里面会被下一个排障的人当成真实抽取结果。
#:
#: 白名单还有一个好处：将来 `ToneResult` 加字段时，
#: **默认不进存储**，必须在这里显式加一行 —— 与 `to_public()` 同一条纪律。
_ROW_KEYS: Final[tuple[str, ...]] = (
    "tone", "has_tone", "neutral", "phrases", "codes",
    "confidence", "source", "explain", "summary",
    # ── 第二轮 ──
    # ⚠️ `bullish` / `bearish` 是**整份**放行的，所以第三轮在方向桶里
    # 新增的 `boards`（板块名 + 主线挖掘板块代码）跟着它们一起落库，
    # 这里不需要（也不该）再列一遍 —— 但**读取侧要显式透传**，
    # 见 `_side_view`：那边是投影，漏键不会报错、只会静默少一块数据。
    "events", "bullish", "bearish",
    # ── 第四轮：分段抽取的来源说明 ──
    # ⚠️ 这三项**必须显式列**（白名单不列就是"算了但用户/排障永远看不到"，
    # 与 `to_public()` 漏字段同一种失败，且没有任何报错）。
    # 它们不是模型输出，是 `tone_job` 自己算出来的整数/字符串 ——
    # 没有幻觉风险，但仍然逐个决定，不整份透传。
    "segments", "calls", "skipped_segments",
    # ── 第五轮：券商名 / 分析师名（模型给的，审计用）──
    # ⚠️ 同样**必须显式列**：白名单不列 = "模型认出了孙潇雅，但存储里没有"，
    # 而表现只是"提示词要了、模型给了、审计时查不到"，没有任何报错。
    "brokers", "analysts",
)


def build_row(result: dict[str, Any]) -> dict[str, Any]:
    """把一份抽取结果**按白名单**整理成可落库的行（含 `content_hash`/`at`）。

    缺键的**不补默认值** —— 读取侧本来就按"缺键 = 那轮没抽过"处理
    （见模块 docstring 的向后兼容要求）。
    """
    return {k: result[k] for k in _ROW_KEYS if k in result}


def save_many(results: list[dict[str, Any]], *,
              root: Path | None = None) -> int:
    """追加写入一批结果，返回写入条数。

    每条至少要有 `content_hash` 与 `tone`，否则跳过 ——
    没有 hash 的结果永远查不回来，写进去只是垃圾。
    """
    rows = []
    for r in results:
        h = str(r.get("content_hash") or "")
        if not h or not r.get("tone"):
            continue
        rows.append({"content_hash": h, "at": _now(), **build_row(r)})
    if not rows:
        return 0

    p = store_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for r in rows:
        _CACHE[r["content_hash"]] = r
    return len(rows)


def prune(*, root: Path | None = None, retain_days: int = RETAIN_DAYS) -> int:
    """删掉过期行（按 `at` 字段），返回删除条数。

    为什么按**整体重写**而不是原地删：JSONL 无法原地删除，
    而这份文件量级很小（实测每天几千行），重写成本可忽略。
    """
    p = store_path(root)
    if not p.exists():
        return 0
    cutoff = (datetime.now(timezone.utc).astimezone()
              - timedelta(days=max(1, retain_days)))
    kept: list[str] = []
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
            removed += 1
            continue
        try:
            at = datetime.fromisoformat(str(obj.get("at") or ""))
        except ValueError:
            kept.append(s)        # 时间戳坏了保守保留（宁可多留）
            continue
        if at >= cutoff:
            kept.append(s)
        else:
            removed += 1
    if removed:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                       encoding="utf-8")
        tmp.replace(p)
        load(root=root, force=True)
    return removed


def stats(*, root: Path | None = None) -> dict[str, Any]:
    """存储概况（供管理员页与排障用）。"""
    data = load(root=root)
    tones: dict[str, int] = {}
    for v in data.values():
        t = str(v.get("tone") or "未定")
        tones[t] = tones.get(t, 0) + 1
    p = store_path(root)
    return {
        "count": len(data),
        "tones": tones,
        "path": str(p),
        "bytes": p.stat().st_size if p.exists() else 0,
    }


__all__ = [
    "RETAIN_DAYS",
    "build_row",
    "get",
    "load",
    "prune",
    "save_many",
    "stats",
    "store_path",
    "view",
]
