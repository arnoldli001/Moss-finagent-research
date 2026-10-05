"""指标元数据注册表（Indicator Registry）。

加载 `configs/indicators.yaml` → `IndicatorMeta` 列表，提供：
  - 按 id 查元数据（精确匹配 + 模板匹配，如 `stock_close:{code}`）
  - 按 category / frequency 分组
  - freshness 判定（fresh / stale / missing）

★ 设计原则：
  1. 模板 id（带 `{code}` 占位符）按前缀匹配；具体 id（带真实代码）按精确匹配。
     例：`stock_close:300308` 先精确查 → 命中 `stock_close:{code}` 模板 → 用模板元数据。
  2. freshness 计算：`now - max(period_date, fetch_time) > freshness_hours` 视为 stale。
  3. 没有 catalog 条目的指标 fallback 到"实时"语义（freshness=0，always stale）。
     这是"按需快速失败"原则：未登记的指标必须显式登记才能享受 SmartFetcher 优化。

## 文件组织
  - 配置：`configs/indicators.yaml`
  - 注册表：本模块（`IndicatorRegistry`）
  - DB 索引表：`src/infrastructure/catalog/catalog_repo.py`（运行时快查 + 上次更新时间）
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# 频率白名单
FREQUENCIES: frozenset[str] = frozenset({
    "realtime", "intraday", "daily", "weekly", "monthly", "quarterly", "yearly",
})


@dataclass(frozen=True)
class IndicatorMeta:
    """单个指标的元数据（catalog YAML 的 Python 化）。"""

    indicator: str
    category: str
    frequency: str
    freshness_hours: float
    primary_source: str
    source_url: str
    enabled: bool
    ttl_days: int
    units: str = ""
    notes: str = ""
    # ★ 存储位置（2026-09-28 新增）：回答"这个指标的数据到底在哪"
    storage_backend: str = "sqlite:fact_data_points"
    storage_location: str = ""
    storage_note: str = ""
    #: ★★ 聚合展开（2026-09-28 第十三轮）：请求该指标时**实际要取**的子指标。
    #:
    #: 为什么需要（实测暴露的隐蔽缺陷）：
    #:   `mkt:cybkcb:turnover:all` 是一个**聚合请求** —— 连接器收到它会返回
    #:   3 条**子指标**（`turnover:cyb` / `turnover:kcb` / `turnover:kcb_all`），
    #:   **"all" 这个名字本身永远不会被写进事实表**。
    #:   于是索引表里它的 row_count 恒为 0 → SmartFetcher 每次判 stale →
    #:   **每次请求都联网**（而这些子指标的数据其实早就躺在库里）。
    #:
    #: 修法：声明展开关系。SmartFetcher 取数时把它展开成子指标，
    #: 索引回填时按子指标聚合计数。
    expands_to: tuple[str, ...] = ()

    def is_template(self) -> bool:
        """是否含 `{xxx}` 占位符（具体代码由 caller 填入）。"""
        return "{" in self.indicator

    def is_aggregate(self) -> bool:
        """是否是需要展开的聚合指标。"""
        return bool(self.expands_to)

    @property
    def backend_kind(self) -> str:
        """后端类型：sqlite / file / mysql / postgres。"""
        return self.storage_backend.split(":", 1)[0]

    @property
    def backend_name(self) -> str:
        """后端标识：表名或路径。"""
        parts = self.storage_backend.split(":", 1)
        return parts[1] if len(parts) > 1 else ""

    def location_text(self) -> str:
        """人话的"数据在哪"（给运维/审计用）。"""
        loc = self.storage_location or self.backend_name
        return f"{self.storage_backend}" + (f" [{loc}]" if loc else "")


@dataclass(frozen=True)
class FreshnessState:
    """运行时新鲜度状态（catalog 条目 + 数据点最新期）。"""

    meta: IndicatorMeta
    last_period_date: str | None     # 最新数据点的 period_date
    last_fetch_time_ms: int           # 最新入库的 fetch_time（epoch ms）
    row_count: int

    def is_fresh(self, *, now_ms: int | None = None) -> bool:
        """DB 内数据是否仍在 freshness_hours 内。

        判断基准 = max(last_period_date, last_fetch_time)。
          - period_date 是数据本身的"时间标签"（CPI 哪月发布）；
          - fetch_time 是"我们拉入库"的时刻（更反映"我们多新有这数据"）。
          - 取较大者，避免 stale 数据被误判 fresh。

        ## ★ 2026-09-28 修正：交易时段感知（实测暴露的缺陷）

        原实现只看 `age <= freshness_hours`，对 `realtime` 指标（0.5h）意味着
        **收盘后、周末、节假日照样判 stale** —— 但市场根本没开，数据不会变。

        实测后果：用户问"今天没开盘，库里明明有最新数据（period_date=今天）"，
        SmartFetcher 仍走网络 → `mkt:cybkcb:spot_summary` 白等 13.1s。

        修正：对 `realtime` / `intraday` 指标，**非盘中**时若数据日期等于
        最近交易日，就一直算 fresh（直到下个交易时段开始）。
        """
        if not self.meta.enabled:
            return False
        if self.row_count == 0:
            return False
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        age_ms = self._age_ms(now_ms)
        if age_ms < 0:
            return True  # 数据点在"未来"，视为 fresh（容忍时钟漂移）
        if age_ms <= self.meta.freshness_hours * 3600 * 1000:
            return True
        # ★ 交易时段感知：行情类指标在非盘中不会变
        return self._fresh_by_session_freeze(now_ms)

    def _fresh_by_session_freeze(self, now_ms: int) -> bool:
        """非盘中时，行情类指标只要数据日期是最近交易日就算 fresh。

        只对 `realtime` / `intraday` 生效 —— `daily` 及以上频率的指标
        （如日线估值、月度 CPI）本来就靠 freshness_hours 判定，
        它们的"新一期"有明确的发布节奏，不适合用交易时段推断。
        """
        from src.infrastructure.catalog.market_calendar import (
            LIVE_FREQUENCIES,
            last_trading_day,
            session_frozen,
        )

        if self.meta.frequency not in LIVE_FREQUENCIES:
            return False
        if not self.last_period_date:
            return False
        # 当前是否非盘中（收盘后 / 周末 / 节假日）
        now_dt = datetime.fromtimestamp(now_ms / 1000)
        if not session_frozen(now_dt):
            return False  # 盘中 → 老老实实按 freshness_hours 判
        ltd = last_trading_day(now_dt)
        if ltd is None:
            return False  # 找不到交易日 → 保守判 stale
        # 数据日期 == 最近交易日 → 冻结期内的最新数据
        return str(self.last_period_date)[:10] == ltd.isoformat()

    def _age_ms(self, now_ms: int) -> int:
        # 优先 fetch_time（反映"我们多新拉过"）
        return now_ms - self.last_fetch_time_ms


#: 通配占位符匹配：`{code}` / `{ts_code}` / `{symbol}` 等
_WILDCARD_RE = re.compile(r"\{[^}]+\}")


def _split_for_index(indicator: str) -> list[str]:
    """把指标 id 切成"段"用于 O(1) 索引。

    规则：按 `:` 切分；每段里的 `{xxx}` 通配符统一替换为哨兵 `\\x00`。

        输入                          → 段
        "CPI"                        → ["CPI"]
        "stock_close:300308"         → ["stock_close", "300308"]
        "stock_close:{code}"         → ["stock_close", "\\x00"]
        "ind:sw_third_pe_ttm:all"    → ["ind", "sw_third_pe_ttm", "all"]
        "ind:penetration:新能源汽车"  → ["ind", "penetration", "新能源汽车"]

    为什么要保留段数：`stock_close:{code}` 只该匹配 2 段、不该匹配
    `stock_close:300308:extra`（3 段）。用 (段数, 段元组) 当 key 天然保证。
    """
    parts = str(indicator).split(":")
    return [_WILDCARD_RE.sub("\x00", p) for p in parts]


class IndicatorRegistry:
    """Indicator Catalog 注册表（进程内单例 + YAML 懒加载）。

    ## 匹配算法（2026-09-28 重构：正则 → 分段哈希，O(n) → O(1)）

    旧实现：对每个查询遍历全部模板跑 `re.fullmatch`，复杂度 O(模板数)。
    上千条指标（宏观+行业+期货+个股模板）时这是纯字符串扫描。

    新实现：**把指标 id 按 `:` 切成段，段内通配替换为哨兵，用
    `(段数, 段元组)` 当哈希键**：

        "stock_close:300308"  → key = (2, ("stock_close", "300308"))
        模板 "stock_close:{code}" → key = (2, ("stock_close", "\\x00"))

    查表分两步：
      1. **精确命中**：`self._by_key.get(real_key)` —— O(1)
      2. **模板命中**：对每一段，把该段**依次**替换成哨兵再查
         —— 最坏 O(段数)，而段数通常 ≤3

    例：查 `stock_close:300308` 未精确命中时，生成候选键
      (2, ("stock_close", "\x00"))   ← 第 2 段通配  ✅ 命中模板
      (2, ("\x00", "300308"))        ← 第 1 段通配
    两次 dict 查找即得，**与模板总数无关**。
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(
            config_path or os.environ.get(
                "MOSS_INDICATOR_CATALOG",
                "configs/indicators.yaml",
            ))
        self._by_id: dict[str, IndicatorMeta] = {}
        self._templates: list[IndicatorMeta] = []
        #: ★ 核心索引：`(段数, 段元组) → IndicatorMeta`（精确 + 模板共用）
        self._by_key: dict[tuple[int, tuple[str, ...]], IndicatorMeta] = {}
        #: ★ n-gram 模糊索引（懒建；只在精确 miss 时才用）
        self._fuzzy_index: dict[str, Counter] | None = None
        #: fuzzy 兜底命中次数（观测用 —— 持续增长说明上游命名系统性不一致）
        self._fuzzy_hits: int = 0
        self._loaded_at: float = 0.0
        # 缓存 TTL（秒）：YAML 改动后最多过 30s 重载；生产可设 0
        self._cache_ttl = float(os.environ.get("MOSS_CATALOG_CACHE_TTL", "30"))

    def _ensure_loaded(self) -> None:
        if self._by_id and (time.time() - self._loaded_at) < self._cache_ttl:
            return
        self._load()

    def _load(self) -> None:
        if not self._config_path.exists():
            self._by_id = {}
            self._templates = []
            self._by_key = {}
            self._loaded_at = time.time()
            return
        with open(self._config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        items = (raw or {}).get("indicators") or []
        by_id: dict[str, IndicatorMeta] = {}
        templates: list[IndicatorMeta] = []
        by_key: dict[tuple[int, tuple[str, ...]], IndicatorMeta] = {}
        for raw_item in items:
            meta = self._parse_item(raw_item)
            if meta is None:
                continue
            key = (len(_split_for_index(meta.indicator)),
                   tuple(_split_for_index(meta.indicator)))
            if meta.is_template():
                templates.append(meta)
                # 模板进索引（先到先得，避免后到的覆盖先到的）
                by_key.setdefault(key, meta)
            else:
                # 重复 id 不覆盖（保留先出现的）
                if meta.indicator not in by_id:
                    by_id[meta.indicator] = meta
                    by_key.setdefault(key, meta)
        self._by_id = by_id
        self._templates = templates
        self._by_key = by_key
        self._loaded_at = time.time()

    @staticmethod
    def _parse_item(raw: dict[str, Any]) -> IndicatorMeta | None:
        try:
            iid = str(raw["id"]).strip()
            if not iid:
                return None
            freq = str(raw.get("frequency", "daily")).lower()
            if freq not in FREQUENCIES:
                # 容忍：未知频度当 daily
                freq = "daily"
            return IndicatorMeta(
                indicator=iid,
                category=str(raw.get("category", "other")),
                frequency=freq,
                freshness_hours=float(raw.get("freshness_hours", 24)),
                primary_source=str(raw.get("primary_source", "")),
                source_url=str(raw.get("source_url", "")),
                enabled=bool(raw.get("enabled", True)),
                ttl_days=int(raw.get("ttl_days", 365)),
                units=str(raw.get("units", "")),
                notes=str(raw.get("notes", "")),
                storage_backend=str(
                    raw.get("storage_backend", "sqlite:fact_data_points")),
                storage_location=str(raw.get("storage_location", "")),
                storage_note=str(raw.get("storage_note", "")),
                expands_to=tuple(
                    str(x) for x in (raw.get("expands_to") or []) if x),
            )
        except (KeyError, ValueError, TypeError) as exc:
            # YAML 解析错误：跳过该条（不阻断整个 catalog）
            return None

    def reload(self) -> None:
        """强制重载（配置变更后调用；测试也用）。"""
        self._loaded_at = 0.0
        self._ensure_loaded()

    # ============ 查询接口 ============

    def get(self, indicator: str) -> IndicatorMeta | None:
        """按指标 id 查元数据（精确 → 单段通配 → 多段通配）。

        复杂度：精确 O(1)；通配最坏 O(段数)（段数通常 ≤3），
        **与模板总数无关**（旧实现是 O(模板数) 正则扫描）。
        """
        self._ensure_loaded()
        if not indicator:
            return None
        # 1) 精确命中（最快路径，覆盖 90%+ 场景）
        meta = self._by_id.get(indicator)
        if meta is not None:
            return meta

        segs = _split_for_index(indicator)
        width = len(segs)

        # 2) 通配命中：逐段替换成哨兵再查（单段通配）
        for i in range(width):
            if segs[i] == "\x00":
                continue  # 查询本身已是哨兵段（不应发生）
            cand = list(segs)
            cand[i] = "\x00"
            meta = self._by_key.get((width, tuple(cand)))
            if meta is not None:
                return self._materialize(meta, indicator)

        # 3) 多段通配（如 `{a}:{b}` 两段全通配）—— 枚举组合，段数小所以可接受
        #    这里只做「连续段整体通配」这一种常见形态，避免组合爆炸
        if width >= 2:
            all_wc = tuple("\x00" for _ in range(width))
            meta = self._by_key.get((width, all_wc))
            if meta is not None:
                return self._materialize(meta, indicator)

        return None

    def get_by_base(self, indicator: str) -> IndicatorMeta | None:
        """按「基名」查元数据：`商誉占净资产比:600036` → 登记在册的 `商誉占净资产比`。

        ## 为什么需要它（2026-09-30，实测 11 条 `freq_mismatch` 的根因）

        一批指标在 YAML 里**只能登记基名**（`notes` 写着"模板：实际指标带 6 位
        代码后缀"，如 `商誉占净资产比` / `货币资金占总资产比` / `有息负债占总资产比`），
        而落库的是**带后缀的具体 id**。`get()` 的精确与通配都要求**段数相同**
        （`商誉占净资产比` 是 1 段、`商誉占净资产比:000001` 是 2 段）⇒ 查不到
        ⇒ 索引把它当"自动登记"给了兜底 `daily/24h`
        ⇒ 维护审计用**日频**宽限（3 天）去判**季频**数据（92 天）⇒ 报
        `freq_mismatch`（实测 3+5+3=11 条）。
        而且它们只有 1 期数据 ⇒ `_infer_frequencies_sync` 按纪律"证据不足不猜"
        （`n < 2`）不修 ⇒ **永远修不好**。本方法让**基名的登记口径对具体 id 生效**。

        ## 判据（刻意严格）

        只认「已登记的 id 是本 id 的**前缀**，且紧随其后就是 `:`」——**最长者胜**。
        不许模糊/相似匹配：`PE(TTM)` 与 `PE(TTM):同比` 这类必须由更长者胜出，
        否则会把"同比"口径的周期套到水平值上。

        ⚠️ **2026-10-01 修一个越界缺陷（`CHG-0157`）**：原实现写的是
        `indicator.startswith(base) and indicator[len(base)] == ":"` ——
        当**查询串本身正好等于某个已登记 id**（`"us_cpi_yoy"`：前面那句
        `":" not in indicator` 拦不住它，因为 id 里本来就没有冒号）
        或 `base` 比 `indicator` 还长时，`indicator[len(base)]` **下标越界抛
        `IndexError`**。实测由跨通道判决的探针炸出来（
        `scripts/_audit_cross_channel_tolerance.py`）。
        它的危害不是崩一次，而是**调用方只能 try/except 吞掉** ⇒
        "查不到容忍度"与"查的时候炸了"在下游长得一模一样，
        功能会**静默失效**（"判据接在没人走的路上"的又一形态）。
        修法用 `startswith(base + ":")`：前缀 + 边界一次表达，不可能越界。
        """
        self._ensure_loaded()
        if not indicator or ":" not in indicator:
            return None
        best: IndicatorMeta | None = None
        best_len = -1
        for base, meta in self._by_id.items():
            if not base or len(base) <= best_len:
                continue
            if indicator.startswith(f"{base}:"):
                best, best_len = meta, len(base)
        return best

    @staticmethod
    def _materialize(meta: IndicatorMeta, indicator: str) -> IndicatorMeta:
        """命中模板后，把模板元数据的具体 indicator 换成查询值。"""
        return IndicatorMeta(
            indicator=indicator,
            category=meta.category,
            frequency=meta.frequency,
            freshness_hours=meta.freshness_hours,
            primary_source=meta.primary_source,
            source_url=meta.source_url,
            enabled=meta.enabled,
            ttl_days=meta.ttl_days,
            units=meta.units,
            notes=meta.notes,
            storage_backend=meta.storage_backend,
            storage_location=meta.storage_location,
            storage_note=meta.storage_note,
        )

    # ---------- ★ 分层查找：exact-first, fuzzy-on-miss ----------

    def resolve(
        self, indicator: str, *, fuzzy: bool = True, threshold: float = 0.55,
    ) -> tuple[IndicatorMeta | None, str]:
        """分层解析指标名，返回 `(meta|None, 命中方式)`。

        ## 为什么分层（实测数据，`scripts/bench_lookup_methods.py`）

        | 方法 | 耗时/次（50 条 catalog） | 覆盖 |
        |---|---:|---|
        | 精确哈希 | **0.32 μs** | 绝大多数 |
        | n-gram 全表 | **126 μs** | 多救 67% 的 miss |

        **n-gram 比精确查找慢 398×**（769 条真实 catalog 上约 1.9ms，
        即约 6000×）。它是"更慢但**能容错**的查找"，**不是**"更快的查找"。

        所以正确做法不是二选一，而是**分层**：

            第一层 精确（含模板展开）→ 命中即返回（0.32μs）
            第二层 n-gram 兜底        → 只在 miss 时跑（126μs，但 miss 是少数）

        实测收益：变体查询的 miss 中 **67% 被 n-gram 救回**，而这些查询
        原本会走"未登记 → 按 daily/24h 兜底 → 每次都联网"（最差路径）。

        Returns:
            (meta, 命中方式)，命中方式 ∈ {"exact", "template", "fuzzy", "miss"}
        """
        self._ensure_loaded()
        if not indicator:
            return None, "miss"

        # 第一层：精确 + 模板（O(1)，主路径）
        exact = self._by_id.get(indicator)
        if exact is not None:
            return exact, "exact"
        meta = self.get(indicator)
        if meta is not None:
            # get() 已经做了模板展开；能到这里说明是模板命中
            return meta, "template"

        # 第二层：n-gram 兜底（只在 miss 时付代价）
        if not fuzzy:
            return None, "miss"
        hit = self._fuzzy_search(indicator, threshold)
        if hit is None:
            return None, "miss"
        return self._materialize(hit, indicator), "fuzzy"

    def _ensure_fuzzy_index(self) -> None:
        """懒建 n-gram 索引（一次性；只包含精确条目的指标名）。

        为什么把模板排除在外：模板名带 `{code}` 占位符，它的 gram
        会污染相似度（如 `stock_close:{code}` 与 `stock_close:300308`
        相似度虚高，但语义上后者该走模板路径而不是 fuzzy）。
        """
        if self._fuzzy_index is not None:
            return
        from collections import Counter

        idx: dict[str, Counter] = {}
        for name in self._by_id:
            t = name.lower()
            n = 3
            idx[name] = (Counter(t[i:i + n] for i in range(len(t) - n + 1))
                         if len(t) >= n else Counter([t]) if t else Counter())
        self._fuzzy_index = idx

    def _fuzzy_search(self, query: str, threshold: float) -> IndicatorMeta | None:
        """n-gram 余弦相似度取最优（`cache.py::_SemanticIndex` 的同款算法）。"""
        import math
        from collections import Counter

        self._ensure_fuzzy_index()
        if not self._fuzzy_index:
            return None
        t = query.lower()
        n = 3
        qv = (Counter(t[i:i + n] for i in range(len(t) - n + 1))
              if len(t) >= n else Counter([t]) if t else Counter())
        if not qv:
            return None
        qn = math.sqrt(sum(c * c for c in qv.values()))

        best_name, best_score = None, threshold
        for name, vec in self._fuzzy_index.items():
            dot = sum(c * vec.get(g, 0) for g, c in qv.items())
            if dot <= 0:
                continue
            vn = math.sqrt(sum(c * c for c in vec.values()))
            if not vn:
                continue
            score = dot / (qn * vn)
            if score >= best_score:
                best_name, best_score = name, score
        if best_name is None:
            return None
        self._fuzzy_hits += 1
        logger.info(
            "指标名 fuzzy 命中：%r → %r（相似度 %.2f）—— "
            "精确查找 miss，若不走 fuzzy 会退化为「未登记 → 每次联网」",
            query, best_name, best_score)
        return self._by_id.get(best_name)

    def stats(self) -> dict[str, int]:
        """查找统计（供 /health 观测 fuzzy 兜底被触发的频率）。

        为什么要观测：`_fuzzy_hits` 持续增长说明**上游给的指标名与登记名
        系统性不一致** —— 那要修的是上游（planner / YAML），
        而不是让 fuzzy 一直兜底（它是 398× 慢的路径）。
        """
        self._ensure_loaded()
        return {
            "exact_entries": len(self._by_id),
            "templates": len(self._templates),
            "fuzzy_hits": self._fuzzy_hits,
            "fuzzy_index_size": len(self._fuzzy_index or {}),
        }

    # ---------- 存储位置查询（回答"这个指标的数据在哪"）----------

    def locate(self, indicator: str) -> dict[str, str] | None:
        """返回指标的物理存储位置（None = 未登记）。

        给运维/审计用：一次调用回答"这个数据在哪张表/哪个文件"。
        """
        meta = self.get(indicator)
        if meta is None:
            return None
        return {
            "indicator": meta.indicator,
            "storage_backend": meta.storage_backend,
            "backend_kind": meta.backend_kind,
            "backend_name": meta.backend_name,
            "storage_location": meta.storage_location,
            "storage_note": meta.storage_note,
            "frequency": meta.frequency,
            "freshness_hours": str(meta.freshness_hours),
        }

    def by_backend(self, kind: str) -> list[IndicatorMeta]:
        """按后端类型过滤（如 'file' → 所有落盘指标）。"""
        return [m for m in self.all() if m.backend_kind == kind]

    def all(self) -> list[IndicatorMeta]:
        """全部元数据（精确条目 + 模板）。"""
        self._ensure_loaded()
        return list(self._by_id.values()) + list(self._templates)

    def by_frequency(self, freq: str) -> list[IndicatorMeta]:
        """按 frequency 过滤（如 monthly / daily）。"""
        return [m for m in self.all() if m.frequency == freq]

    def enabled_ids(self) -> list[str]:
        """仅启用的精确 id（模板不计入，由 caller 展开）。"""
        return [m.indicator for m in self.all() if m.enabled]


# ============ 进程内单例 ============

_REGISTRY: IndicatorRegistry | None = None


def get_registry() -> IndicatorRegistry:
    """获取注册表单例（懒加载）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = IndicatorRegistry()
    return _REGISTRY


def reset_registry_for_test() -> None:
    """清空单例（**仅测试用**）。"""
    global _REGISTRY
    _REGISTRY = None