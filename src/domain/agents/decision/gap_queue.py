"""数据缺口队列（跨轮次补取）—— A17 发现缺口 → A19 补取 → 回填索引。

## 为什么需要它（用户 2026-09-28 提出）

> 「先实现 A17 发现缺口 → A19 补取」

**现状的两个断点**：

```
已通：A01 采某个指标拿不到 → _try_self_heal() → A19 生成连接器   ✅
未通：A17 在 data_gaps 里说"缺 X" → ??? → A19 自动补             ❌
```

**为什么不能当场补**：A17 跑在链路末端，它的结论**已经要交付了**。
当场触发 A19（LLM 生成连接器 + 沙箱验证，秒级到十几秒）只会让用户多等，
而且补到的数据**本轮用不上**（A17 的 prompt 已经发出去了）。

**所以这是跨轮次能力**：
    本轮：A17 报缺口 → **入队**
    盘后：队列消费者 → A19 补取 → 落库 → 回填索引
    下轮：该指标变成"DB 命中"，不再报缺口

## 设计要点

| 要点 | 做法 | 为什么 |
|---|---|---|
| **落盘** | `data/gap_queue.jsonl` | 进程内存队列重启即失效（AGENTS.md 告警闸门纪律同源） |
| **幂等** | 同 `(indicator, reason)` 去重，带 TTL | 同一缺口反复报会淹没队列 |
| **状态机** | `pending → resolving → resolved / failed / skipped` | 可观测、可重试、可人工介入 |
| **退避** | 失败 N 次后 `skipped` | 已知不可达的指标（CME）不该每次重试 |
| **不阻断** | 入队失败只记日志 | 缺口登记是**增值**功能，坏了不该拖垮主链路 |

## 与 `capabilities.py` 的关系

A17 的 `data_gaps` 若带了 `status`，本模块据此决定**要不要入队**：

| status | 是否入队 | 理由 |
|---|---|---|
| `available_not_included` | ✅ 入队 | 系统里有，只是本次没纳入 → A19 可以直接补 |
| `source_terminated` | ❌ 不入队 | 源已停披，补也补不到 |
| `unavailable` | ❌ 不入队 | 环境限制（如 CME 不可达），补不到 |
| `fetchable`（A17 判定的"可下载"） | ✅ 入队 | 用户提到的"编程 agent 帮下载缺少的数据" |
| 无 status（纯字符串，兼容旧格式） | ✅ 入队 | 保守：宁可多试一次 |
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel  # noqa: E402

#: 队列文件（相对项目根）
QUEUE_REL = store_rel("gap_queue")

#: 同一缺口在此时长内不重复入队（秒）。24 小时 = 一天最多报一次
DEDUP_TTL_S = 24 * 3600.0

#: 失败多少次后放弃（转 skipped）
MAX_ATTEMPTS = 3

#: 「源停更 / 没登记更新周期」的重试节奏：**每月一次**。
#:
#: 用户 2026-09-30 口径：「**所有过时数据或没登记更新周期的数据都触发月频更新一次**」。
#: 为什么不能按日重试：源停更（如 社融 的商务部镜像停在 202604）**每天试都是白试**，
#: 而 `MAX_ATTEMPTS=3` 会在第 3 天把它标成 `skipped` **永久放弃** —— 源一旦恢复
#: 就再也没人去取。月频重试是"不放弃、也不浪费"的那个点。
SOURCE_LAG_RETRY_S = 30 * 24 * 3600

#: 状态白名单
STATUSES = ("pending", "resolving", "resolved", "failed", "skipped")

#: 不入队的 status（见模块 docstring 的表）
NO_ENQUEUE_STATUS = frozenset({"source_terminated", "unavailable"})

# ============ ★ 缺口路由：每类缺口的**唯一出口** ============
#
# 2026-09-29 实测缺陷（维护审计探针发现的）：`gap_queue` 的**唯一消费者**是
# `gap_drain` 作业 → `DataGapResolverAgent`（A19，**LLM 生成连接器 + 沙箱验证**）。
# 但队列里躺着两类完全不同的东西：
#   * A17 报的 **散文缺口**（实测 35 条 pending 全是这一类）：
#     「北向资金日度净买额（2024-08-19起交易所停止披露），外资实时流向不可得。」
#     A19 拿不到**可判定的指标名** ⇒ 必然失败，却每次都真的花一次 LLM 调用
#     （实测这 35 条 attempts=0：**一条都没被成功补过**，全是白烧）。
#   * 维护审计报的**指标缺口**（`stock_close:600036` 这类）：源与连接器都在，
#     只是**批采作业**没跑到 ⇒ 出口是 `catalog_*` 作业，不是 LLM 写连接器。
#
# 所以"入队"不等于"该给 A19"。路由按**注入的判据**决定（不在这里 import
# registry/连接器 —— 保持本模块可离线单测）：
ROUTE_CATALOG = "catalog"      # 取数侧没问题 ⇒ 出口 = 既有 `catalog_*` 批采/按需取数
ROUTE_RESOLVER = "resolver"    # 登记在册但没有生产者（或压根没登记）⇒ A19 的本职
ROUTE_PROSE = "prose"          # 非指标形态（散文）⇒ A19 无从下手，**只登记不烧钱**

ROUTES = (ROUTE_CATALOG, ROUTE_RESOLVER, ROUTE_PROSE)

#: 出现这些字符就**不是**指标 id（句子成分/空白/全角括号）。
#:
#: ⚠️ 注意它只用于**排除冒号形态的散文**，不是"有没有标点 ⇒ 是不是 id"的判据 ——
#: 实测反例：`美国联邦基金利率及目标区间（上游显示为?%）` **一个标点都没有**，
#: 却是散文（它靠"没有冒号且登记表查不到"被排除）。
_SENTENCE_CHARS = "，。；！？、（）《》“”‘’ \t\r\n"


@dataclass(frozen=True)
class GapRoute:
    """一条缺口的去向判定。"""

    route: str
    indicator: str          # 从文本里认出的指标 id（认不出则为空）
    reason: str             # 人话理由（进日志）


def looks_like_indicator_id(text: str, *, is_registered: Any = None) -> bool:
    """文本是不是**可判定的指标 id**（不是散文）。

    判据只有两条（都机器可读，**不靠标点/长度这类会漂移的启发式**）：

      1. `X:Y` 形态 —— 冒号分隔，右段是 `600036` 这类 6 位代码或 `UNRATE`
         这类全大写序列号（`fred:UNRATE` / `净息差:600036` / `stock_close:600036`）；
      2. 或者**登记表里查得到**（`CPI` / `社融` / `us_unemployment` 这类具体 id）。

    实测为什么不能用"有没有标点"当判据：`美国联邦基金利率及目标区间（上游显示为?%）`
    **没有标点**（会被误判成 id），而 `限售解禁规模与家数。` 有句号。
    """
    t = str(text or "").strip()
    if not t or len(t) > 60:
        return False
    # `X:Y` 形态 —— 允许**多段**冒号：实测真实 id 里有 `ind:sw_third_dividend_yield:all`
    # （`ind:` 族前缀 + 指标名 + 成员），只认一段会把它们误判成散文。
    parts = t.split(":")
    if (len(parts) >= 2 and all(parts)
            and not any(ch in p for p in parts for ch in _SENTENCE_CHARS)):
        tail = parts[-1]
        if (tail.isdigit() and len(tail) == 6) or (
                tail.isascii()
                and tail.replace("_", "").replace("-", "").isalnum()):
            return True
    if is_registered is not None:
        try:
            return bool(is_registered(t))
        except Exception:  # noqa: BLE001 判据坏了不该把队列打挂
            return False
    return False


def route_gap(text: str, *, is_registered: Any = None,
              has_connector: Any = None) -> GapRoute:
    """一条缺口 → 唯一出口。

    Args:
        text: 队列里的 `indicator` 字段（可能是散文，见模块注释）
        is_registered: `(indicator) -> bool`，登记表里查得到吗（含 `{code}` 模板）
        has_connector: `(indicator) -> bool`，有连接器认它吗
    """
    raw = str(text or "").strip()
    if not looks_like_indicator_id(raw, is_registered=is_registered):
        return GapRoute(ROUTE_PROSE, "",
                        "非指标形态（散文缺口）：A19 需要可判定的指标名，"
                        "送进去只会白花一次 LLM 调用")
    hit_connector = False
    if has_connector is not None:
        try:
            hit_connector = bool(has_connector(raw))
        except Exception:  # noqa: BLE001
            hit_connector = False
    registered = False
    if is_registered is not None:
        try:
            registered = bool(is_registered(raw))
        except Exception:  # noqa: BLE001
            registered = False
    if hit_connector:
        return GapRoute(
            ROUTE_CATALOG, raw,
            "有连接器认它 ⇒ 出口是既有 `catalog_*` 批采/按需取数"
            + ("" if registered else "（但**没登记**：登记面缺口，不需要写连接器）"))
    if registered:
        return GapRoute(ROUTE_RESOLVER, raw,
                        "已登记但没有任何连接器认它 ⇒ 缺生产者，A19 的本职")
    return GapRoute(ROUTE_RESOLVER, raw, "未登记且无连接器认它 ⇒ 需新登记/新连接器")


def route_entries(entries: Any, *, is_registered: Any = None,
                  has_connector: Any = None) -> dict[str, list[Any]]:
    """把一批 `GapEntry` 按出口分组（返回 `{route: [entry, ...]}`）。"""
    out: dict[str, list[Any]] = {r: [] for r in ROUTES}
    for entry in entries:
        verdict = route_gap(getattr(entry, "indicator", ""),
                            is_registered=is_registered,
                            has_connector=has_connector)
        out[verdict.route].append(entry)
    return out


@dataclass
class GapEntry:
    """一条缺口记录。"""

    indicator: str
    reason: str = ""
    status: str = "pending"
    source: str = "A17_recommend"   # 谁报的
    first_seen_ms: int = 0
    last_try_ms: int = 0
    attempts: int = 0
    result: str = ""
    trace_id: str = ""
    #: 下一次**允许**尝试的时刻（ms）。0 = 立即可试。
    #:
    #: 用来表达"月频兜底"：源停更/没登记周期的缺口试过一次后推到 30 天后，
    #: 既不每天白试，也不会像 `skipped` 那样**永久放弃**（源恢复后下个月会再试）。
    #: 老队列文件里没有这个字段 ⇒ `GapEntry(**raw)` 用默认 0 兼容。
    next_try_ms: int = 0

    def key(self) -> tuple[str, str]:
        return (self.indicator, self.reason[:80])


class GapQueue:
    """缺口队列（JSONL 落盘，进程内缓存）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        # ★★ 2026-09-29 修一个**静默写错地方**的缺陷（维护审计的探针发现的）：
        #   原实现 `Path(__file__).resolve().parents[3]` —— 从
        #   `src/domain/agents/decision/gap_queue.py` 往上数三层是 **`src/`**，
        #   于是队列落在 **`src/data/gap_queue.jsonl`**（实测该文件已存在，
        #   里面躺着 **44 条真实缺口**：A17 报的、A19 补失败的、被清理的假缺口）。
        #   三重后果：
        #     ① `configs/data_stores.yaml` 登记的 `data/gap_queue.jsonl`
        #        **是个幻影**（没有任何代码用它）——"同一件事写在 N 处"的又一次现形；
        #     ② 队列写在**源码树里**：清理/换机器/只读挂载都会静默丢队列；
        #     ③ `src/` 下多出一个数据目录，没人知道该不该备份。
        #   修法：用**登记表的 canonical 常量** `PROJECT_ROOT`（`data_stores.py`），
        #   不再自己数 `parents[N]`（数错一层不报错，只是写歪）。
        self._root = Path(root) if root else PROJECT_ROOT
        self._path = self._root / QUEUE_REL
        self._entries: dict[tuple[str, str], GapEntry] = {}
        self._loaded = False

    # ---------- 读写 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                e = GapEntry(**raw)
                self._entries[e.key()] = e
            except TypeError:
                continue

    def _persist(self) -> None:
        """全量重写（队列规模小；比 append 重但保证状态一致）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(json.dumps(asdict(e), ensure_ascii=False)
                      for e in self._entries.values()) + "\n",
            encoding="utf-8")
        tmp.replace(self._path)

    # ---------- 入队 ----------

    def enqueue(
        self, indicator: str, *, reason: str = "", status: str = "",
        source: str = "A17_recommend", trace_id: str = "",
    ) -> bool:
        """入队一条缺口。返回 True = 新入队；False = 去重/被拒。

        判据（按顺序）：
          1. 指标名为空 → 拒
          2. status 在 `NO_ENQUEUE_STATUS` → 拒（补也补不到）
          3. 已有同 key 且未超 DEDUP_TTL → 拒（去重）
          4. 已达 MAX_ATTEMPTS 且失败 → 拒（放弃）
        """
        ind = str(indicator or "").strip()
        if not ind:
            return False
        if status in NO_ENQUEUE_STATUS:
            logger.debug("缺口不入队（status=%s）：%s", status, ind)
            return False

        self._load()
        now = int(time.time() * 1000)
        key = (ind, reason[:80])

        existing = self._entries.get(key)
        if existing is not None:
            # 已解决：不再入队（除非数据又被清了）
            if existing.status == "resolved":
                return False
            # 放弃的：不再入队
            if existing.status == "skipped":
                return False
            # ★ 去重窗口：基准是 **first_seen_ms**，不是 last_try_ms。
            #   踩过的坑（被测试抓到）：新入队时 `last_try_ms=0`，
            #   用 `(now - 0) < TTL` 判会**恒为 False** → 去重完全失效。
            #   first_seen_ms 在入队时就写了，是稳定的去重基准。
            if existing.first_seen_ms and \
                    (now - existing.first_seen_ms) < DEDUP_TTL_S * 1000:
                return False

        self._entries[key] = GapEntry(
            indicator=ind, reason=reason[:200], status="pending",
            source=source, first_seen_ms=now, attempts=0, trace_id=trace_id,
        )
        try:
            self._persist()
        except OSError as exc:
            logger.warning("缺口队列落盘失败（不影响主链路）: %s", exc)
        logger.info("数据缺口入队：%s（%s）", ind, reason[:60])
        return True

    # ---------- 消费 ----------

    def pending(self) -> list[GapEntry]:
        """待处理（status=pending、未超尝试上限、且**已到重试时刻**）。"""
        self._load()
        now = int(time.time() * 1000)
        return [e for e in self._entries.values()
                if e.status == "pending" and e.attempts < MAX_ATTEMPTS
                and int(getattr(e, "next_try_ms", 0) or 0) <= now]

    def pending_resolvable(self, *, is_registered: Any = None,
                           has_connector: Any = None) -> list[GapEntry]:
        """待处理里**该送 A19** 的那些（散文与"批采能补"的排除在外）。

        ★ 不传判据时**与 `pending()` 逐条一致** —— 向后兼容（既有调用点/测试
        不受影响）。这是本项目定过的纪律：新参数不传时行为不变。
        """
        items = self.pending()
        if is_registered is None and has_connector is None:
            return items
        groups = route_entries(items, is_registered=is_registered,
                               has_connector=has_connector)
        return groups[ROUTE_RESOLVER]

    def routing(self, *, is_registered: Any = None,
                has_connector: Any = None) -> dict[str, int]:
        """各出口的条数（进日志，让人一眼看出"钱花在哪一类"）。"""
        groups = route_entries(self.pending(), is_registered=is_registered,
                               has_connector=has_connector)
        return {r: len(v) for r, v in groups.items()}

    def mark(self, entry: GapEntry, status: str, *, result: str = "") -> None:
        """更新状态并落盘。"""
        self._load()
        e = self._entries.get(entry.key())
        if e is None:
            return
        if status not in STATUSES:
            logger.warning("未知缺口状态 %s（按 failed 记）", status)
            status = "failed"
        e.status = status
        e.last_try_ms = int(time.time() * 1000)
        e.result = result[:300]
        if status in ("failed",):
            e.attempts += 1
            if e.attempts >= MAX_ATTEMPTS:
                e.status = "skipped"
                logger.info("缺口 %s 已达尝试上限，转 skipped", e.indicator)
        elif status == "resolving":
            e.attempts += 1
        try:
            self._persist()
        except OSError as exc:
            logger.warning("缺口队列落盘失败: %s", exc)

    def retry_later(self, entry: GapEntry, seconds: int,
                    *, result: str = "") -> None:
        """把这条推到 `seconds` 秒后再试（**不放弃**）。

        与 `mark(..., "skipped")` 的分工：
          * `skipped` = **永久放弃**（试满 `MAX_ATTEMPTS` 次仍失败）；
          * `retry_later` = "现在试没用，下个月再说" —— 源停更/没登记周期的
            缺口走这条。**为什么不能靠 skipped**：源一旦恢复（如商务部镜像
            重新更新），被 skipped 的缺口**再没有任何路径回到 pending**。
        """
        self._load()
        e = self._entries.get(entry.key())
        if e is None:
            return
        e.status = "pending"
        e.attempts = 0                     # 月频重试**不消耗**尝试次数
        e.next_try_ms = int((time.time() + max(0, int(seconds))) * 1000)
        if result:
            e.result = result[:300]
        try:
            self._persist()
        except OSError as exc:
            logger.warning("缺口队列落盘失败: %s", exc)

    # ---------- 观测 ----------
    def stats(self) -> dict[str, int]:
        self._load()
        out = {s: 0 for s in STATUSES}
        for e in self._entries.values():
            out[e.status] = out.get(e.status, 0) + 1
        out["total"] = len(self._entries)
        return out


# ============ 单例 ============

_QUEUE: GapQueue | None = None


def get_gap_queue(root: str | Path | None = None) -> GapQueue:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = GapQueue(root)
    return _QUEUE


def reset_gap_queue_for_test() -> None:
    global _QUEUE
    _QUEUE = None


# ============ 消费执行器 ============

async def drain_catalog_gaps(
    fetcher: Any, *, max_items: int = 20, is_registered: Any = None,
    has_connector: Any = None, retry_after_s: int = SOURCE_LAG_RETRY_S,
) -> dict[str, int]:
    """消费队列里**取数侧能补**的那些（`catalog` 路由）：真取一次，**不烧 LLM**。

    ## 为什么必须有这一跳

    路由只解决了"不要把钱烧在散文上"，**没解决"那谁来补"** ——
    被路由判成 `catalog` 的缺口（"有连接器认它"）原先在两边都不落地：
    A19 不收（对，不该让 LLM 写连接器），批采作业也不认识它（它只在
    `catalog_*` 的名单里，而名单来自**登记表**，索引里自动登记的那些不在其中）。
    本函数补上那一跳。

    ## 三态（每态都有机器可读的落点）

      * 取到**新期** ⇒ `resolved`（并回填索引）；
      * 取到了、但最新期**与库内相同** ⇒ `failed` + **月频重试**，
        `result` 写明「源停更：取到的最新期 X 与库内相同」—— 这是
        "不是我们没采、是源就给到这一期"的**机器证据**；
      * 取不到/抛错 ⇒ `failed` + **月频重试**（不是永久放弃）。

    Args:
        fetcher: `async (indicator) -> dict`，返回
            `{"ok": bool, "before": str, "after": str, "reason": str}`；
            取数实现由调用方注入（生产中就是既有批采入口），
            本模块**不自己连库、不自己联网**（与 `derive.py` 同一条纪律）。
        max_items: 单轮上限（一轮 700 条会把盘后作业跑成几小时）。
        retry_after_s: 失败后的重试间隔（默认月频）。
    """
    q = get_gap_queue()
    groups = route_entries(q.pending(), is_registered=is_registered,
                           has_connector=has_connector)
    items = groups[ROUTE_CATALOG][:max_items]
    backlog = len(groups[ROUTE_CATALOG])
    out = {"resolved": 0, "failed": 0, "queued": len(items), "backlog": backlog}
    if not items:
        return out

    for entry in items:
        q.mark(entry, "resolving")
        try:
            res = await fetcher(entry.indicator)
        except Exception as exc:  # noqa: BLE001 单条失败不阻断其余
            q.mark(entry, "failed", result=f"{type(exc).__name__}: {exc}")
            q.retry_later(entry, retry_after_s,
                          result=f"{type(exc).__name__}: {exc}")
            out["failed"] += 1
            continue
        before = str((res or {}).get("before") or "")
        after = str((res or {}).get("after") or "")
        if (res or {}).get("ok") and after and after > before:
            # 索引回填由 `fetcher` 负责（它拿着 catalog 仓储）——本模块不碰库。
            q.mark(entry, "resolved", result=f"补到新期 {after}（原 {before or '-'}）")
            out["resolved"] += 1
        elif (res or {}).get("ok"):
            why = (f"源停更：取到的最新期 {after or '-'} 与库内 "
                   f"{before or '-'} 相同，源自己没有再新的")
            q.mark(entry, "failed", result=why)
            q.retry_later(entry, retry_after_s, result=why)
            out["failed"] += 1
        else:
            why = str((res or {}).get("reason") or "取不到")
            q.mark(entry, "failed", result=why)
            q.retry_later(entry, retry_after_s, result=why)
            out["failed"] += 1
    if backlog > len(items):
        logger.info("取数侧补采：本轮补 %d 条，**队列里还有 %d 条**"
                    "（按频率每月重试，不会永久放弃）", len(items), backlog)
    return out


async def drain_gaps(
    resolver: Any, *, max_items: int = 5, repo: Any = None,
    data_repo: Any = None, is_registered: Any = None,
    has_connector: Any = None,
) -> dict[str, int]:
    """消费队列：逐条调 A19 补取 → 成功则回填索引。

    Args:
        resolver: `DataGapResolverAgent` 实例（有 `resolve()` 方法）
        max_items: 单轮最多处理几条（防止盘后作业跑太久）
        repo: catalog 仓储（回填索引用）；None 则不回填
        is_registered / has_connector: 路由判据（见 `route_gap`）。
            传了就**只把"该 A19 干"的送进去**；`prose` 与 `catalog` 两类
            不进 A19（前者它无从下手、后者批采已经管）。不传则与旧行为一致。

    Returns: `{"resolved": n, "failed": m, "queued": k, "by_route": {...}}`
    """
    #: `data_repo` 目前**刻意未使用**（保留给"补取成功后直接写事实表"的演进路径）。
    #: ⚠️ 原先这行 `del` 写在 for 循环**里面** ⇒ 处理到第 2 条时
    #: `UnboundLocalError`（实测：单条用例永远发现不了，两条就炸）。
    del data_repo
    q = get_gap_queue()
    items = q.pending_resolvable(is_registered=is_registered,
                                has_connector=has_connector)
    routing = q.routing(is_registered=is_registered,
                        has_connector=has_connector)
    if routing.get(ROUTE_PROSE) or routing.get(ROUTE_CATALOG):
        # ★ 出声：不被 A19 处理的那两类必须可见，否则"队列还有 30 条"
        #   会被读成"补取没干活"（本项目为"静默降级"付过代价）。
        logger.info("缺口路由：%s（A19 只处理 resolver=%d 条；prose=散文缺口、"
                    "catalog=批采的活，都不烧 LLM）",
                    routing, len(items))
    items = items[:max_items]
    out = {"resolved": 0, "failed": 0, "queued": len(items),
           "by_route": dict(routing)}
    if not items:
        return out

    for entry in items:
        q.mark(entry, "resolving")
        try:
            result = await resolver.resolve(entry.indicator, entry.reason)
        except Exception as exc:  # noqa: BLE001 单条失败不阻断其余
            q.mark(entry, "failed", result=f"{type(exc).__name__}: {exc}")
            out["failed"] += 1
            continue

        if getattr(result, "success", False):
            # ★ 回填索引：否则下次仍判 stale（本项目实测过"落库但索引没更"）
            try:
                if repo is not None:
                    await repo.refresh_stats_from_facts([entry.indicator])
            except Exception as exc:  # noqa: BLE001
                logger.debug("缺口回填索引失败（忽略）: %s", exc)
            q.mark(entry, "resolved", result="A19 补取成功")
            out["resolved"] += 1
        else:
            err = getattr(result, "error", "") or "未知原因"
            q.mark(entry, "failed", result=str(err))
            out["failed"] += 1
    return out


__all__ = [
    "DEDUP_TTL_S",
    "MAX_ATTEMPTS",
    "NO_ENQUEUE_STATUS",
    "ROUTES",
    "ROUTE_CATALOG",
    "ROUTE_PROSE",
    "ROUTE_RESOLVER",
    "SOURCE_LAG_RETRY_S",
    "STATUSES",
    "GapEntry",
    "GapQueue",
    "GapRoute",
    "drain_catalog_gaps",
    "drain_gaps",
    "get_gap_queue",
    "looks_like_indicator_id",
    "reset_gap_queue_for_test",
    "route_entries",
    "route_gap",
]
