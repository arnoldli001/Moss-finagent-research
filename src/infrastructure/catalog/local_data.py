"""本地数据执行器：**意图直达列** + 空结果诊断 + 跨库只读。

## 为什么需要它（用户 2026-09-28 第三次报障）

用户三轮追问同一件事："数据在库里/在数据源上，为什么取不到"。
逐轮排查后根因是**同一个**：

    系统的"我能拿到什么"由**人工维护的契约列表**决定
    （`configs/indicators.yaml` → `INDICATOR_CATALOG` → 白名单 → 连接器 `_CODE_PREFIXES`），
    而**库里实际有什么**从来没有一条可执行的取数路径。

实测现场（2026-09-28）：`quant_daily_basic` 含 `dv_ratio`(股息率) /
`total_mv` / `turnover_rate` —— 投研链路**零命中**（SmartFetcher 与数据仓储里
这张表出现 0 次）。于是用户看到的结论是「招商银行缺基本面与股息率数据」。

> ⚠️ **行数一律现算，禁止写进注释**（`CHG-0059`）：这里原先写的那串数字
> 是 `data/dev/moss_dev.db` 里**化石副本**的行数，
> 而权威副本在**共享行情仓**（`data/quant/warehouse.db`）——
> 那串数字被抄到 6 处，并据此误判成"只存在于 dev 库 → 生产上永远取不到"。

> **修订（CHG-0066）**：扫描范围改由 `configs/data_stores.yaml`
> （`data_stores.all_stores()`）决定，不再由本模块或 `ColumnIndex`
> 自己维护"扫哪些库"—— 这正是原来"行情仓被名字 + 体积双重跳过"的根因。

**本模块把顺序倒过来**：先问库"你有什么"（`ColumnIndex` 反向索引），
再按意图直接取 —— **不要求先在 `indicators.yaml` 登记新指标**。

## 与既有链路的关系（互补，不替代）

    SmartFetcher       走 indicator_catalog → fact_data_points（**契约驱动**）
    本模块             走 ColumnIndex 反向索引 → 任意库任意列（**资产驱动**）

两者是"或"的关系：契约里有就走契约链（有新鲜度判定、有联网兜底），
契约里没有但**库里确实有**，就走这里。**先本地，再联网**。

## 空结果必须诊断（用户专家建议的第 3 条）

只说"没找到"会让排查方向随机。本模块给出**12 个机器可读错误码**，
其中 `NOT_APPLICABLE_FOR_ENTITY` 是本轮实测**新增**的：

    流动比率:600036 → 列存在、14 期数据也在，
    但**银行资产负债不划分流动/非流动** → 该实体此列恒空

它不是"列不存在"（会去改映射表，白改），也不是"没数据"（会去补数据源，白补）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# 诊断错误码（机器可读 —— Agent 据此决定下一步，而不是猜）
# ============================================================

class DiagCode:
    """空结果的**原因码**。每个码对应一个明确的下一步动作。"""

    CONN_FAIL = "CONN_FAIL"
    """数据源/库连不上。下一步：查连接与健康检查，**不是**"数据没有"。"""

    NO_TABLE = "NO_TABLE"
    """表不存在。下一步：确认库/表名，或该数据集尚未落库。"""

    NO_COLUMN = "NO_COLUMN"
    """字段不存在。下一步：**换列名/同义词**（不是换数据源）。"""

    ENTITY_UNMAPPED = "ENTITY_UNMAPPED"
    """实体未映射（"招商银行"→600036 失败）。下一步：查实体字典/名称表。"""

    TIME_OUT_OF_RANGE = "TIME_OUT_OF_RANGE"
    """时间超出覆盖范围。下一步：**放宽时间**或改用历史区间。"""

    FREQ_MISMATCH = "FREQ_MISMATCH"
    """频率不匹配（要日频、只有季频）。下一步：上卷/下钻频率。"""

    UNIT_MISMATCH = "UNIT_MISMATCH"
    """单位不一致（元/万元、%与小数）。下一步：单位换算后再比。"""

    DIM_MISMATCH = "DIM_MISMATCH"
    """维度不匹配（要行业级、只有个股级）。下一步：换维度或聚合。"""

    NO_PERMISSION = "NO_PERMISSION"
    """无权限（只读账号/RLS 拦截）。下一步：**报权限问题，不是缺数据**。"""

    NOT_APPLICABLE_FOR_ENTITY = "NOT_APPLICABLE_FOR_ENTITY"
    """★ 本轮的**关键新增**：列在、表在、有数据，但**该实体不适用此口径**。

    实测：`流动比率` 对银行恒空（银行资产负债不划分流动/非流动）。
    归成 NO_COLUMN 会去改映射表（白改），归成 NO_DATA 会去补源（白补）。
    下一步：**换适用于该类实体的口径**（银行用资产负债率/产权比率）。
    """

    STALE_BEYOND_TOLERANCE = "STALE_BEYOND_TOLERANCE"
    """有数据但过旧（超出容忍）。下一步：补采或降级说明"这是历史值"。"""

    NO_DATA = "NO_DATA"
    """确认无数据（表在、列在、实体在、时间在，就是没记录）。
    这是**唯一的真缺口**，可以进缺口队列。"""

    # ---------- ★ 环境维度（2026-09-28 第二十四轮补，见 PRD §18.3 A6）----------
    #
    # 为什么必须与环境维度分开：多环境（dev/test/pilot/prod）各有自己的库，
    # **"本环境没有" ≠ "没有数据"**。不区分的话，"dev 没同步过来"会被误报成
    # "库里没有这个字段" —— 排查方向完全相反（去补数据源 vs 去跑同步）。

    ENV_NOT_COVERED = "ENV_NOT_COVERED"
    """该指标在**任何环境**的库里都找不到 → 覆盖缺口（不是环境差异）。"""

    PROD_ONLY = "PROD_ONLY"
    """只有生产库有（如历史回填数据）→ 当前环境查不到是**预期**行为。
    下一步：去生产环境查，或等同步。**不要**报成数据缺失。"""

    DEV_ONLY = "DEV_ONLY"
    """只有 dev 库有（如尚未发布的实验数据）→ 生产查不到是**预期**行为。
    下一步：确认该指标是否已发布；未发布就别在生产依赖它。"""

    DEV_SYNC_DELAY = "DEV_SYNC_DELAY"
    """dev 有、但当前环境的最新期间明显落后 → **同步延迟**，不是没有。
    下一步：跑同步；或如实标注"数据截止到 X"。"""

    PROD_PERMISSION_DENIED = "PROD_PERMISSION_DENIED"
    """生产库拒绝访问（只读账号/RLS 拦截）→ **权限问题**，不是没有数据。"""


#: 错误码 → 一句"下一步做什么"（给 Agent 读，避免它自由发挥）
DIAG_NEXT_STEP: dict[str, str] = {
    DiagCode.CONN_FAIL: "检查数据源连接/健康状态；不要报告为『数据不存在』",
    DiagCode.NO_TABLE: "确认库与表名；或该数据集尚未落库（需补采）",
    DiagCode.NO_COLUMN: "换列名或使用同义词重试；**不要**换数据源",
    DiagCode.ENTITY_UNMAPPED: "用实体字典/名称表解析实体代码后重试",
    DiagCode.TIME_OUT_OF_RANGE: "放宽时间范围，或改用覆盖期内的历史区间",
    DiagCode.FREQ_MISMATCH: "调整频率（季→年上卷 / 日→月聚合）后重试",
    DiagCode.UNIT_MISMATCH: "统一单位（元/万元、%与小数）后重试",
    DiagCode.DIM_MISMATCH: "换维度或先聚合到目标维度",
    DiagCode.NO_PERMISSION: "报告权限问题（**不要**报成数据缺失）",
    DiagCode.NOT_APPLICABLE_FOR_ENTITY: (
        "该口径不适用于此类实体（如银行无流动比率）→ 换适用口径，"
        "并说明原因，**不要**报成数据缺失"),
    DiagCode.STALE_BEYOND_TOLERANCE: "补采；或如实标注『历史值，非当前值』",
    DiagCode.NO_DATA: "真缺口：可入缺口队列，由补采链路处理",
    # —— 环境维度（见 DiagCode 里那一段的说明）——
    DiagCode.ENV_NOT_COVERED: (
        "任何环境的库里都没有该指标 → 这是覆盖缺口，按真缺口处置（可入队）"),
    DiagCode.PROD_ONLY: (
        "只有生产库有 → 去生产环境查，或等同步；**不要**报成数据缺失"),
    DiagCode.DEV_ONLY: (
        "只有 dev 库有（尚未发布）→ 确认发布状态；**不要**在生产依赖它"),
    DiagCode.DEV_SYNC_DELAY: (
        "当前环境落后于 dev → 跑同步；或如实标注数据截止期间"),
    DiagCode.PROD_PERMISSION_DENIED: (
        "生产库拒绝访问 → 报告权限问题，**不要**报成数据缺失"),
}

#: 只有这个码**才配**进缺口队列（其余是"我们的问题"，补采也补不到）。
ENQUEUEABLE_CODES: frozenset[str] = frozenset({DiagCode.NO_DATA,
                                               DiagCode.NO_TABLE})


@dataclass
class Diag:
    """一次空结果的诊断结论。"""

    code: str
    detail: str = ""
    candidates: list[str] = field(default_factory=list)

    @property
    def next_step(self) -> str:
        return DIAG_NEXT_STEP.get(self.code, "（未知码）")

    def render(self) -> str:
        parts = [f"[{self.code}] {self.detail}".strip()]
        if self.candidates:
            parts.append(f"候选：{', '.join(self.candidates[:5])}")
        parts.append(f"下一步：{self.next_step}")
        return "；".join(parts)


@dataclass
class MetricSeries:
    """取数结果（成功或带诊断的失败）。"""

    metric: str
    entity: str = ""
    points: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    dataset_id: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    diag: Diag | None = None

    @property
    def ok(self) -> bool:
        return bool(self.points) and self.diag is None

    @property
    def row_count(self) -> int:
        return len(self.points)

    def summary(self, limit: int = 10) -> str:
        if self.ok:
            head = self.points[-limit:][::-1]
            lines = [
                f"- {p.get('period')}: {p.get('value')}"
                for p in head
            ]
            return (f"本地命中 {self.row_count} 条（{self.dataset_id}）\n"
                    + "\n".join(lines))
        return self.diag.render() if self.diag else "无数据"


# ============================================================
# 执行器
# ============================================================

def _alias_match_span(alias: str, query: str) -> int:
    """别名在查询串里**实际匹配了多长**（最长匹配打分，0 = 不匹配）。

    为什么要打分而不是"别名长的优先"：别名『股息率ttm』**比**查询串
    『股息率』还长，按长度排序会把它排在前面 —— 那正是错误方向
    （实测踩过：`股息率` 被解析成 `dv_ttm`）。

    正确判据是**匹配跨度**：
        查询『股息率』    → 『股息率』跨度 3 > 『股息率ttm』跨度 0  → 选 dv_ratio ✅
        查询『股息率ttm』 → 『股息率ttm』跨度 6 > 『股息率』跨度 3  → 选 dv_ttm   ✅
    """
    if not alias or not query:
        return 0
    if alias == query:
        return len(query) + 1000          # 完全相等，最强
    if alias in query:
        return len(alias)                 # 别名是查询的子串
    if query in alias:
        return len(query)                 # 查询是别名的子串
    return 0


#: 列名 → 标准指标名的**兜底映射**。
#:
#: ## 主判据是 `synonym_dict`（B4，154 条指标别名 + 260 条实体别名）
#:
#: 这张表只在 `synonym_dict` 不可用时兜底（它未装/导入失败）——
#: 本项目已登记过"同一判断只允许一份实现"，所以**不重复维护同义词**：
#: 这里的 12 条是 `synonym_dict` 的最小影子集，只为"字典模块坏掉时链路不瘫"。
_METRIC_ALIASES_FALLBACK: dict[str, tuple[str, ...]] = {
    "股息率": ("dv_ratio", "dividend_yield", "股息率", "dv_ttm"),
    "股息率ttm": ("dv_ttm", "dv_ratio"),
    "总市值": ("total_mv", "市值"),
    "流通市值": ("circ_mv", "流通市值"),
    "换手率": ("turnover_rate", "turnover_rate_f", "换手率"),
    "量比": ("volume_ratio",),
    "市销率": ("ps_ttm", "ps"),
    "市盈率": ("pe_ttm", "pe"),
    "市净率": ("pb",),
    "收盘价": ("close", "close_basic", "收盘"),
    "成交量": ("volume", "vol"),
    "成交额": ("amount",),
}

#: ⚠️ 实体列 / 时间列候选**不在本模块定义**（`CHG-0066` 后续，2026-10-01 删除）。
#:
#: 这里原先有 `_ENTITY_COLS`（6 项）与 `_TIME_COLS`（9 项）两份拷贝，
#: 注释写着「与 `column_index.*` 同源」—— 实测**逐字不同**（各少 1~2 项），
#: 而且它们**从未被任何代码读取**（全仓只有定义行）。
#: 结果是：一份不会生效、却看起来在生效的判据，加一句不成立的"同源"注释。
#: 唯一事实源是 `column_index.ENTITY_COLUMN_CANDIDATES` /
#: `column_index.TIME_COLUMN_CANDIDATES`（本模块实际用的实体列/时间列
#: 来自 `TableColumns.entity_column` / `.time_column`，那是按常量扫出来的）。
#: 判据：`tests/unit/test_local_data_paths.py::..._have_single_source`。

#: 默认陈旧容忍（天）。超过就报 `STALE_BEYOND_TOLERANCE`，但仍**返回数据**
#: （带 `stale_days`）—— 丢掉一个真实的历史值比标出它更糟。
DEFAULT_STALE_DAYS: int = 400


class LocalDataExecutor:
    """把"意图"落到"本地库里的某一列"并取数。

    用法：
        ex = LocalDataExecutor(index=ColumnIndex().build())
        res = ex.metric_series("股息率", entity="600036")
        if res.ok: ... else: print(res.diag.render())
    """

    def __init__(self, index: Any | None = None, *,
                 stale_days: int = DEFAULT_STALE_DAYS,
                 max_points: int = 400) -> None:
        self._index = index
        self._stale_days = stale_days
        self._max_points = max_points

    # ---------- L2：跨库只读查询面（PRD §16.3「读统一、写归属」）----------

    @staticmethod
    def query_across(stores: list[str], sql: str,
                     params: list[Any] | None = None) -> list[dict[str, Any]]:
        """**一份只读连接**跨多个存储查询，库名直接写在 SQL 里。

        用法（板块映射 × 行情仓估值 —— 这在 `CHG-0066` 之前做不到）：

            LocalDataExecutor.query_across(
                ["app_db", "warehouse"],
                "SELECT b.code, m.sector_id, b.dv_ratio "
                "FROM warehouse.quant_daily_basic b "
                "JOIN main.map_quant_sector_stock m ON m.code = b.code "
                "WHERE b.trade_date = ?", ["20260928"])

        ## 为什么写路径**不**走这里

        本函数返回的是 `mode=ro` + `PRAGMA query_only=1` 的连接，
        且 SQLite **没有跨库事务** —— 一次写两个库只能半提交。
        所以口径是**读统一（这里）、写归属（`data_stores.writable_here`）**，
        写路径必须恰好落到一个库，否则"谁改了这个数"答不出来
        （数据溯源规范要求每个数据点可归属到来源与操作者）。
        """
        from src.infrastructure.catalog.data_stores import open_readonly

        conn, _aliases = open_readonly(*stores)
        try:
            cur = conn.execute(sql, params or [])
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    @staticmethod
    def available_stores() -> list[dict[str, Any]]:
        """当前环境的存储清单 + 写权限报告（给人看与给 Agent 看的是同一份）。"""
        from src.infrastructure.catalog.data_stores import describe

        return describe()["stores"]

    # ---------- 索引（惰性，避免每次请求都扫库）----------

    def _idx(self) -> Any:
        if self._index is None:
            from src.infrastructure.catalog.column_index import (
                get_column_index,
            )

            self._index = get_column_index()
        return self._index

    def _refresh_index(self) -> bool:
        """让列级索引**重建一次**（应对库在运行期间被改动）。

        ## 为什么需要它（并发协作者实测场景）

        索引是**内存缓存**：`get_column_index()` 进程级单例。
        而库结构会在运行期间变化 —— 实测（2026-09-28）另一个会话在
        清理冗余表，主库表数 47→52、dev 33→38，且随时可能 DROP 掉
        我们缓存过的表。那时"查不到"**不是"没有数据"**，而是
        **我们拿着一张过期的地图**。

        ## 只在"看起来矛盾"时才重建（不无条件重建）

        判据：**缓存说这张表有行，实测却一行都查不到**。
        无条件每次重建的代价是 ~1.7 s（实测），不能放在查询路径上。

        重建失败（返回 False）时**保持旧索引**并照常返回诊断码 ——
        自愈是增强，不是必需（坏了不能把主流程弄崩）。

        ## ⚠️ 作用域纪律（`CHG-0066` 补，实测踩到）

        本函数原先**无条件**把 `self._index` 换成全局单例
        `get_column_index(rebuild=True)` —— 而全局单例扫的是**真实仓库**。
        后果：调用方给了 `project_root=<tmp>` 造的假库树（单测、离线脚本），
        一次自愈就把**作用域悄悄扩大成真实仓库**，于是
        **单测读到了真实行情仓**、用例的通过与否开始取决于本机数据。

        这不是理论风险：`test_local_data_executor.py` 的两个用例
        （"列在但实体无值应判 NOT_APPLICABLE"、"诊断要带候选列"）
        在本轮之前是**靠巧合通过**的 —— 全局单例当时也看不见行情仓，
        所以 `current_ratio` 两边都查不到；行情仓一进扫描范围，它们立刻变红。

        所以：**给了显式索引就只重建它自己**，绝不换成全局单例。
        """
        try:
            if self._index is not None:
                # 显式索引 = 调用方限定的作用域。就地重建，**不越界**。
                self._index.build()
                return True
            from src.infrastructure.catalog.column_index import (
                get_column_index,
            )

            self._index = get_column_index(rebuild=True)
            return True
        except Exception as exc:  # noqa: BLE001 自愈失败不影响诊断输出
            logger.warning("列级索引重建失败（沿用旧索引）：%s", exc)
            return False

    # ---------- 解析：意图 → (表, 列) ----------

    def _alias_candidates(self, metric: str) -> list[str]:
        """指标名的候选**列名**（同义词优先，其次原文）。

        ## 主判据走 `synonym_dict`（B4）

        顺序：
          ① `synonym_dict.resolve_metric()` —— 154 条指标别名，**最长匹配跨度**
             排序（『股息率TTM』→ `dv_ttm` 在前，『股息率』→ `dv_ratio` 在前）
          ② 原文兜底（`out.append(metric.strip())`）—— 保留，因为"库里的列名
             就是用户说的那个词"很常见
          ③ `synonym_dict` 不可用时退回内置的 `_METRIC_ALIASES_FALLBACK`

        ## ⚠️ 与 `synonym_dict.resolve_metric` 的一处**刻意语义差异**

        它认不出时返回 `[]`（"我没认出这个词"），而这里**保留原文兜底** ——
        因为本函数的调用方是"拿到候选去索引里查列名"，
        而库里的列名**可能就叫这个名字**。两者语义不同，不是不一致：
          · `resolve_metric` 回答"这个词对应哪个标准口径"
          · `_alias_candidates` 回答"拿哪些字符串去索引里试"
        """
        key = (metric or "").strip()
        out: list[str] = []
        try:
            from src.infrastructure.catalog.synonym_dict import resolve_metric

            out.extend(resolve_metric(key))
        except Exception:  # noqa: BLE001 字典不可用时退回内置影子集
            logger.debug("synonym_dict 不可用，改用内置别名兜底：%s", key)
            low = key.lower()
            scored = [(_alias_match_span(a, low), a)
                      for a in _METRIC_ALIASES_FALLBACK]
            scored = [(s, a) for s, a in scored if s > 0]
            scored.sort(key=lambda x: (-x[0], x[1]))
            for _span, alias in scored:
                out.extend(_METRIC_ALIASES_FALLBACK[alias])
        if key:
            out.append(key)          # ② 原文兜底（见 docstring 的语义说明）
        seen: set[str] = set()
        return [c for c in out if c and not (c.lower() in seen
                                             or seen.add(c.lower()))]

    def resolve(self, metric: str, *, entity: str = "") -> tuple[Any, str, list[str]]:
        """返回 `(TableColumns | None, 命中的列名, 候选列名列表)`。

        ## ★ 选库必须走 `ColumnIndex.best_for_column`（单一入口）

        第一版这里自己写了一套"优先带实体列的"挑选逻辑，而
        `ColumnIndex.dataset_registry()` 里另有一套排序 —— **两套排序必然漂移**
        （本项目已登记过同类缺陷："同一个判断只允许一份实现"）。

        现在选库只有一处：`best_for_column` 按
        **①有没有数据 → ②最新期次优先（期次不可得则退回既有顺序）→
        ③库权威层级 → ④行数** 排序。

        ⚠️ 同一列常常**有多条路径**（实测去重后 119 个列）。本函数只回答
        "选哪张"，"还有哪几条、为什么选它"由 `_path_visibility()` 从**同一次排序**
        取出来挂进 `plan` —— 不是第二套排序，也不会退化成扫全库。

        ⚠️ 为什么"有没有数据"要排在"库权威"**前面**（实测教训）：
        `quant_daily_basic.dv_ratio` 只在 **dev 库**有 1184 万行，
        主库与试点库**连这张表都没有**。若按"主库优先"选，
        会选中一张空表 → 用户又看到"缺数据"。**先看有没有，再看谁权威。**
        """
        idx = self._idx()
        candidates = self._alias_candidates(metric)
        for col in candidates:
            table = idx.best_for_column(col)
            if table is not None:
                return table, col, candidates
        return None, "", candidates

    # ---------- 执行 ----------

    def _path_visibility(self, column: str) -> dict[str, Any]:
        """「这一列有几条路径、选了哪条、为什么」—— 挂进 `plan` 与日志。

        ## 为什么必须有（实测空白）

        同一列被多张表提供是**常态**而非例外：2026-10-01 实测（按 `asset_id`
        去重后）**119 个列**有多条路径（`code` 47 张、`trade_date` 37 张、
        `created_at` 23 张…），而选库只有一个入口、只返回一张表 ——
        "还有别的路径、为什么不是那张"在返回结果里**一个字都没有**。
        期次/口径不同的两张表看起来一样可用：这正是"看起来很有据"的错误温床。

        ## 挂在哪（沿用既有设施，不新造一套）

        · **机器可读** → `MetricSeries.plan["path_reason"]`（`PathReason.*`，
          与 `DiagCode` 同一风格：每个码对应一个明确结论）
        · **人读** → `plan["paths"]` / `plan["path_why"]` / `plan["path_candidates"]`
          与 `logger.info`（多于一条路径才记，避免刷日志）
        · 空结果时的诊断**不改**：`Diag` 回答"为什么没取到"，
          这里回答"取到了，为什么是它" —— 两件事，别混。

        ## 性能与失败纪律

        `column_paths` 只排**这一列**（纯内存，单列 0.0x ms）。
        诊断是增强：拿不到就返回 `{}`（`plan` 里少几个键），**绝不阻断取数**。
        """
        try:
            paths = self._idx().column_paths(column)
        except Exception as exc:  # noqa: BLE001 诊断失败不影响取数
            logger.debug("路径可见性不可用（不影响取数）：%s", exc)
            return {}
        if not paths:
            return {}
        top = paths[0]
        if len(paths) > 1:
            logger.info("列 %s 有 %d 条路径；选中 %s（%s）",
                        column, len(paths), top.asset_id, top.why)
        return {
            "paths": len(paths),
            "path_rank": top.rank,
            "path_reason": top.reason,
            "path_why": top.why,
            "path_candidates": [
                f"{p.db}:{p.table}" + (f"@{p.latest_time}" if p.latest_time else "")
                for p in paths[:5]
            ],
        }

    def metric_series(self, metric: str, *, entity: str = "",
                      start: str | None = None, end: str | None = None,
                      limit: int | None = None) -> MetricSeries:
        """按意图取一条指标序列（**本地优先**，不联网）。"""
        table, column, candidates = self.resolve(metric, entity=entity)
        if table is None:
            return MetricSeries(
                metric=metric, entity=entity,
                diag=Diag(DiagCode.NO_COLUMN,
                          detail=f"本地 {self._count_tables()} 张表里没有"
                                 f"匹配『{metric}』的列",
                          candidates=candidates),
                plan={"metric": metric, "entity": entity},
            )

        plan = {
            "metric": metric, "column": column, "entity": entity,
            "db": table.db_name, "table": table.table,
            "time_column": table.time_column,
            "entity_column": table.entity_column,
            "start": start, "end": end,
        }
        # ★ 多路径可见性：几条路径 / 选了哪条 / 为什么（本轮新增，见 _path_visibility）
        plan.update(self._path_visibility(column))
        rows, err = self._fetch(table, column, entity, start, end, limit)
        if err is not None:
            return MetricSeries(metric=metric, entity=entity, plan=plan,
                                dataset_id=table.asset_id, diag=err)
        if not rows:
            # ★ 陈旧引用自愈：索引是**内存缓存**，而库可能在运行期间被改动
            #   （另一次清理/迁移 DROP 了表或列）。此时"查不到"不代表"没有数据"，
            #   而是**我们拿着一张过期的地图**。
            #   判据：缓存说这张表有行，实测却查不到任何行 → 让索引重建一次再试。
            if table.has_data and self._refresh_index():
                fresh = self._idx().best_for_column(column)
                if fresh is not None and fresh.asset_id != table.asset_id:
                    logger.info("索引已刷新：%s 的落点由 %s 改为 %s",
                                column, table.asset_id, fresh.asset_id)
                    retry, err2 = self._fetch(fresh, column, entity,
                                              start, end, limit)
                    if err2 is None and retry:
                        return MetricSeries(
                            metric=metric, entity=entity, points=retry,
                            source=f"{fresh.db_name}:{fresh.table}.{column}",
                            dataset_id=fresh.asset_id,
                            # 重新算一遍路径可见性：落点换了，旧的"为什么"就过期了
                            plan={**plan, "db": fresh.db_name,
                                  "table": fresh.table,
                                  "index_refreshed": True,
                                  **self._path_visibility(column)},
                        )
            return MetricSeries(
                metric=metric, entity=entity, plan=plan,
                dataset_id=table.asset_id,
                diag=self._diagnose_empty(table, column, entity, start, end),
            )

        stale = self._staleness(table.latest_time)
        if stale is not None and stale > self._stale_days:
            # **返回数据 + 标注**，而不是丢弃：历史值也是真值，
            # 丢掉它等于让"这张表停更了"这件事彻底不可见。
            logger.info("%s.%s 最新期间距今 %d 天（超过 %d 天容忍）",
                        table.table, column, stale, self._stale_days)
        return MetricSeries(
            metric=metric, entity=entity, points=rows,
            source=f"{table.db_name}:{table.table}.{column}",
            dataset_id=table.asset_id, plan={**plan, "stale_days": stale},
        )

    def _fetch(self, table: Any, column: str, entity: str,
               start: str | None, end: str | None,
               limit: int | None) -> tuple[list[dict[str, Any]], Diag | None]:
        """执行参数化查询（**只读**打开，恒带 LIMIT）。"""
        db_path = Path(table.db_path)
        if not db_path.exists():
            return [], Diag(DiagCode.CONN_FAIL,
                            detail=f"库文件不存在：{db_path.name}")
        ent_col = table.entity_column
        time_col = table.time_column
        # 显式列出要选的列并记住顺序（比"猜偏移量"直白，也不会有未使用变量）
        selects: list[tuple[str, str]] = []      # (字段名, SQL 表达式)
        if time_col:
            selects.append(("period", f'"{time_col}"'))
        if ent_col:
            selects.append(("entity", f'"{ent_col}"'))
        selects.append(("value", f'"{column}"'))

        where: list[str] = [f'"{column}" IS NOT NULL']
        params: list[Any] = []
        if entity and ent_col:
            where.append(f'CAST("{ent_col}" AS TEXT) = ?')
            params.append(self._norm_entity(entity))
        if start and time_col:
            where.append(f'CAST("{time_col}" AS TEXT) >= ?')
            params.append(_norm_date(start))
        if end and time_col:
            where.append(f'CAST("{time_col}" AS TEXT) <= ?')
            params.append(_norm_date(end))

        sql = (f'SELECT {", ".join(expr for _, expr in selects)} '
               f'FROM "{table.table}" WHERE ' + " AND ".join(where))
        # ★★★ 必须 **倒序取、再正序还**（实测踩过的致命 bug）
        #
        # 第一版写 `ORDER BY 时间 ASC LIMIT 400` —— 那取到的是**最早的 400 条**！
        # 实测：招商银行的 dv_ratio 返回「最新 20071015 = 0.6448」，
        # 而真实的 2023-11-10 值是 5.0464。**差 8 倍，且看起来完全正常。**
        #
        # 对投研这是致命错误：用 2007 年的股息率回答"现在能不能持有"。
        # 正确做法：`ORDER BY 时间 DESC LIMIT N` 取**最新 N 条**，
        # 再在内存里翻回升序（下游按时间序读，语义不变）。
        desc = False
        if time_col:
            sql += f' ORDER BY "{time_col}" DESC'
            desc = True
        sql += f" LIMIT {int(limit or self._max_points)}"
        del desc

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                raw = list(con.execute(sql, params))
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg:
                return [], Diag(
                    DiagCode.NO_TABLE,
                    detail=f"{table.table} 在 {db_path.name} 中已不存在"
                           "（表结构在运行期间被改动/清理过）",
                )
            if "no such column" in msg:
                return [], Diag(
                    DiagCode.NO_COLUMN,
                    detail=f"{table.table}.{column} 已不存在（列在运行期间被改动）",
                )
            if "readonly" in msg or "attempt to write" in msg:
                return [], Diag(DiagCode.NO_PERMISSION, detail=str(exc)[:160])
            if "unable to open" in msg or "disk i/o" in msg:
                return [], Diag(DiagCode.CONN_FAIL,
                                detail=f"库不可打开：{db_path.name}")
            return [], Diag(DiagCode.CONN_FAIL, detail=str(exc)[:160])
        except sqlite3.Error as exc:
            return [], Diag(DiagCode.CONN_FAIL, detail=str(exc)[:160])

        out: list[dict[str, Any]] = []
        for row in raw:
            out.append({name: row[i] for i, (name, _) in enumerate(selects)})
        # 倒序取的，这里翻回升序 —— 下游（含 A17 的展示）按时间序读
        if time_col:
            out.reverse()
        return out, None

    @staticmethod
    def _norm_entity(entity: str) -> str:
        """实体归一：`600036.SH` / `SH600036` / `招商银行` → `600036`。

        ## 为什么要认中文名（这条 query 的必需环节）

        用户问的是「未来半年能否持有高股息的**招商银行**」，而不是 `600036`。
        只认代码的话，实体过滤会查不到任何行 —— 而症状是
        `NOT_APPLICABLE_FOR_ENTITY`（"该实体一条都没有"），
        **看起来像"这只票没有数据"，其实是"名字没被解析成代码"**。

        解析顺序（**都只在本地/缓存内，不联网**）：
          ① 已是 6 位代码（含带后缀写法）→ 直接用
          ② 同义词字典（B4，人工维护，最快）
          ③ `security_resolver.resolve_stock_sync`（名称表：内存 → 新鲜磁盘缓存；
             **过期时才可能联网**，且有超时保护）
        """
        raw = (entity or "").strip()
        if not raw:
            return ""
        # ① 带后缀/前缀的代码写法
        for pattern in (r"^(\d{6})(?:\.\w+)?$", r"^(?:SH|SZ|BJ)(\d{6})$",
                        r"^(\d{6})"):
            m = re.match(pattern, raw, flags=re.IGNORECASE)
            if m:
                return m.group(1)
        # ② 同义词字典（可选依赖：字典没装也不该影响主流程）
        try:
            from src.infrastructure.catalog.synonym_dict import resolve_entity

            hits = resolve_entity(raw)
            if hits:
                return hits[0]
        except Exception:  # noqa: BLE001 字典未就绪时静默降级到名称表
            logger.debug("同义词字典不可用，改用名称表解析实体：%s", raw)
        # ③ 名称表（本地缓存优先）
        try:
            from src.infrastructure.connectors.security_resolver import (
                resolve_stock_sync,
            )

            hit = resolve_stock_sync(raw)
            if hit:
                return hit[0]
        except Exception as exc:  # noqa: BLE001 名称表不可用时保留原文
            logger.debug("证券名称解析失败(%s): %s", raw, exc)
        return raw

    # ---------- 空结果诊断 ----------

    def _diagnose_empty(self, table: Any, column: str, entity: str,
                        start: str | None, end: str | None) -> Diag:
        """**空结果必须分类**（否则排查方向随机）。判据顺序即优先级。"""
        # ① 表里这一列**整体**有没有值？没有 → 该列在我们的数据里是空的
        total = self._count(table, f'"{column}" IS NOT NULL')
        if total == 0:
            return Diag(
                DiagCode.NOT_APPLICABLE_FOR_ENTITY,
                detail=f"{table.table}.{column} 全表无值 —— "
                       "该口径在本地数据源中未被填充（可能是该口径不适用于"
                       "此类实体，如银行无流动比率）",
                candidates=self._neighbor_columns(table, column),
            )

        # ② 实体维度：库里有值，但**这个实体**没有 → 最典型的"口径不适用"
        if entity and table.entity_column:
            n_ent = self._count(table, f'"{column}" IS NOT NULL AND '
                                       f'CAST("{table.entity_column}" AS TEXT) = ?',
                                [self._norm_entity(entity)])
            if n_ent == 0:
                others = self._count(table, "1=1")
                return Diag(
                    DiagCode.NOT_APPLICABLE_FOR_ENTITY,
                    detail=f"{table.table}.{column} 有 {total} 条值，但实体 "
                           f"{entity} **一条都没有**（全表共 {others} 行）—— "
                           "该口径很可能不适用于此类实体",
                    candidates=self._neighbor_columns(table, column),
                )

        # ③ 时间维度：实体有值，但不在请求区间 → 放宽时间即可
        if start or end:
            n_ent = self._count(
                table,
                f'"{column}" IS NOT NULL'
                + (f' AND CAST("{table.entity_column}" AS TEXT) = ?'
                   if (entity and table.entity_column) else ""),
                [self._norm_entity(entity)] if (entity and table.entity_column) else [])
            if n_ent > 0:
                return Diag(
                    DiagCode.TIME_OUT_OF_RANGE,
                    detail=f"实体 {entity or '（全部）'} 的 {column} 有 {n_ent} 条，"
                           f"但都不在 [{start or '-'} ~ {end or '-'}] 内；"
                           f"该表覆盖到 {table.latest_time}",
                )

        # ④ 连"表里有这列的值"都不成立时（上面已覆盖）→ 真缺口
        return Diag(
            DiagCode.NO_DATA,
            detail=f"{table.table}.{column} 存在但该组合无记录",
            candidates=self._neighbor_columns(table, column),
        )

    def _count(self, table: Any, where: str,
               params: list[Any] | None = None) -> int:
        sql = f'SELECT COUNT(*) FROM "{table.table}" WHERE {where}'
        try:
            con = sqlite3.connect(f"file:{table.db_path}?mode=ro", uri=True)
            try:
                return int(con.execute(sql, params or []).fetchone()[0] or 0)
            finally:
                con.close()
        except sqlite3.Error:
            return 0

    @staticmethod
    def _neighbor_columns(table: Any, column: str, top: int = 4) -> list[str]:
        """同表里语义相近的其它列（引导"换口径"而不是"换数据源"）。"""
        try:
            from src.infrastructure.connectors.akshare_connector import (
                _suggest_columns,
            )

            return _suggest_columns(column, list(table.columns), top=top)
        except Exception:  # noqa: BLE001 建议失败不影响诊断
            return [c for c in table.columns[:top] if c != column]

    def _count_tables(self) -> int:
        try:
            return len(self._idx().tables)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _staleness(latest: str) -> int | None:
        """最新期间距今天数（解析不出返回 None）。"""
        from datetime import date

        raw = str(latest or "").strip()
        if not raw:
            return None
        digits = re.sub(r"\D", "", raw)[:8]
        if len(digits) < 8:
            return None
        try:
            y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
            if not (1 <= m <= 12 and 1 <= d <= 31):
                return None
            return (date.today() - date(y, m, d)).days
        except (TypeError, ValueError):
            return None


def _norm_date(raw: str) -> str:
    """`2026-09-01` / `20260901` → 统一的 `20260901`（兼容两种存储口径）。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[:8] if len(digits) >= 8 else str(raw or "")
