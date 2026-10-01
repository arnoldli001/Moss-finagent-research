"""平台自有后端数据连接器：把**本平台自己算出来**的四类数据接进采集层。

## 为什么需要它（用户原话，2026-09-29）

> 「连接器或行业agent，个股agent，要考虑连接器接入如下本平台的**板块拥挤度、
>   主线挖掘、个股行情估值打分、投资日历个股解禁**情况的后端数据…
>   个股所属行业 agent：要接入本地数据库的**概念板块拥挤度**数据表…个股**相关度
>   最大的**所属概念板块，其**拥挤度水平**可以给行业 agent 辅助参考分析，
>   **主线挖掘若最近有中高强度的告警信息**，也可以给信息，辅助综合分析…
>   个股 agent：自身**估值水平**…是属于"**估值透支**"、还是"**估值合理偏贵**"、
>   "**上涨空间充足**"，其判断依据的数据，都可以连接到…」

这四类数据**早就在平台上跑着**（前端看板、主线挖掘、做T面板、投资日历），
但它们**一条都没进连接器** —— 于是 Agent 侧完全看不到，用户看到的是
"缺数据"（与 2026-09-28 那条 `fed:policy_range` 报障**同一形状**：
数据在库里，Agent 看不见，且**不报错**）。

## 指标族与口径（`supports()` 与 `get_capabilities()` **都从这张表派生**）

* `估值水位:{code}` —— 估值空间分 ∈[-1,1]（正=上涨空间、负=透支）+ 五档结论；
  档位与中文标签取自 `src/intraday/valuation.py`；
  数据 = 平台采集链（PE/PB 序列）+ 本地行情仓 `quant_daily_basic`。
* `概念拥挤度:{code}` —— **相关度最高**的那个概念板块的 `water_level`（水位 0~1），
  明细随 extra 下发；数据 = `mainline_cache::ml_member_corr` / `ml_stock_theme`
  × `legacy_main::sector_crowding_daily`。
* `主线告警:{code}` —— 窗口内该股所属概念的**中/高强度**
  （`level ∈ {medium, strong}`）主线挖掘告警，每告警一个点；数据 = `app_db::mainline_alert`。
* `个股告警:{code}` —— `affected_stocks_json` 里**点名该 code** 的告警，
  value=方向性分数；数据 = `app_db::fact_alerts`。
* `解禁计划:{code}` —— 窗口内该股的限售解禁计划，value=该解禁日解禁市值合计（元）；
  数据 = `app_db::fact_data_points` 的 `cal:unlock:*`（明细在 **`extra_json.top_stocks`**）。
* `行业拥挤度:{行业名}`（**占位是行业名**）—— 行业名 → **最相近的所属概念板块**
  （`sector_crowding_daily.sector_name` 最长匹配）→ 该板块 `water_level`。
* `板块资金流:{行业名}` —— 板块近期资金流方向（近 N 日主力净额合计 + 方向 + 截面排名），
  数据 = `src.fundflow.provider::FundFlowProvider`。
* `行业轮动:{行业名}` —— 「行业轮动日报」里该行业那一行 + 全局研判（含落伍天数），
  数据 = `src.sector_rotation` 的 `report_YYYYMMDD.json`。

## ★★ 纪律一：`估值水位` 的档位与标签**只有一份实现**

用户在前端看到「估值透支」（永鼎股份 600105）／「估值合理偏贵」（招商银行 600036）
就是 `src/intraday/valuation.py` 给的。**本连接器不定义任何阈值、不写标签表**：

* `compute_headroom_score()` —— 合成估值空间分（分位 / 同业 / 行业三分量）；
* `headroom_bucket()` —— 五档 key（阈值 `0.30 / 0.05 / -0.20` 写在那里）；
* `HEADROOM_LABELS` —— 中文标签（`上涨空间充足/估值合理/估值合理偏贵/估值透支/数据不足`）；
* `ValuationProvider.fetch()` —— **平台权威路径**（个股分位 + 同业中位数 + 巨潮行业中位数），
  带着前端渲染的全部字段。

同一判断写两份的后果不是报错，是**界面说「估值合理偏贵」而 Agent 说「估值透支」**
（同一天、同一只票），两边都"有理有据"。所以本模块**只 import，不重写**。
护栏：`tests/unit/test_platform_data_connector.py` 里有一条
"档位→标签必须来自 `HEADROOM_LABELS`，且档位必须是 `headroom_bucket` 的值域"
的断言（含五个档位各一条的**行为**用例，谁另抄一份立刻红）。

### 两条估值路径（都会把"这次含不含同业/行业对标"写进 extra）

1. **`valuation_provider`（首选，与前端逐字段一致）** —— 需要装配时注入采集链
   （`PlatformDataConnector(backend=...)`，`src/api/runtime.py` 的路由装配处注入）。
   它走项目**既有的** PE/PB 采集链（百度估值近三年序列，实测 600036 = 1110 点），
   与前端读的是**同一份缓存**。
2. **`local_warehouse_series`（退化路径，也会真实发生）** —— 采集链没注入或取不到时，
   直接用共享行情仓 `quant_daily_basic` 的 `pe_ttm/pb`，**仍走同一组判据函数**，
   并在 `extra` 里如实写明：`peer_source`（本路径恒为 `unavailable`）、
   `peer_pe_median=None`、`industry_pe_median=None`、`components_used=["自身历史分位"]`。
   实测该退化路径对**用户截图上的两只票给出了同一个档位**
   （600036 → `stretched` 估值合理偏贵；600105 → `expensive` 估值透支），
   但**样本集不同**（行情仓按交易日 724 点 vs 百度按自然日 1110 点），
   边界附近可能与界面不一致 —— 这条**随数据下发**（`extra.basis`），不许省略。

## ★★ 纪律二：「没量到」与「量到 0」分开（`解禁计划` 是唯一有三态的一族）

1. 窗口内有该股解禁明细 → 产点，`value` = 该日解禁市值合计（元）
   （判据：`extra_json.top_stocks` 里命中该 code）。
2. 窗口内有 `cal:unlock:*` 行、但**没有该 code** → **产一个 `value=0.0` 的点**
   —— 这是**量到 0**（真结论：无解禁计划），`extra.unlock_dates_in_window`
   列出查过的解禁日。
3. 窗口内**一条 `cal:unlock:*` 都没有** → **不产点**（`[]`）—— 这是**没量到**
   （日历没同步），`diagnose()` 给出原因。

其余四族"查不到"一律**不产点**（`[]`），**绝不填 0**，并由 `diagnose()`
回答"为什么这次产不出点"（`no_rows` / `no_crowding` / `no_alerts_in_window` …）。

## ★★ 纪律三：相关度排序用**逐票**的表，不用候选池

实测（2026-09-29，本机）：

* `mainline_cache::ml_member_corr` **111,624 行 / 5,215 只**（`board_code, code, corr, samples,
  start_date, end_date, computed_at`，索引 `idx_ml_member_corr_code`）——
  **以个股为键**的走势相关度；600036 实测 **16 个板块**，按 corr 降序；
* `mainline_cache::ml_stock_theme` **42,404 行 / 4,996 只**（`code, rank, theme,
  business_score, corr, final_score, reason`）—— **以个股为键**的主营相关度，`final_score` 非空；
* 与 `sector_crowding_daily` 的 join：**600036 的 16 个板块 16/16 命中**（同一套 `TI` 编码）。

⚠️ **不要**用 `map_stock_concept`（7,815 行但**只覆盖 105 只**个股）当主力来源，
也不要用 `ml_member_pure`（**以板块为键的候选池**：600036 只有 1 条且 `relevant=0`）。
两者的形状都是"看起来对、其实答非所问"。
名字 join（`ml_stock_theme.theme → sector_crowding_daily.sector_name`）实测 3/4 命中，
**未命中的那条如实保留**（`no_crowding: true` + 原因）—— 悄悄丢掉它会吃掉
"相关度第一但没有拥挤度"这条信息。

## ★★ 纪律四：只读、只走 registry、不写任何库

* 库路径**只**从 `src.infrastructure.catalog.data_stores.resolve_store(<store名>)` 取
  （`src/` 里写路径字面量会被 `test_store_registry.py::test_no_store_path_literals_in_src` 拦下）；
* 连接一律 `sqlite3.connect(f"file:{p}?mode=ro", uri=True)` + `PRAGMA query_only=1`；
* 三个库各司其职（实测口径，见 `configs/data_stores.yaml`）：
  · `legacy_main`（= `data/moss_finagent.db`）—— `sector_crowding_*`（平台拥挤度模块自己的
    `legacy_main` 口径，见 `src/sector_crowding/config.py::_store_rel("legacy_main")`）；
  · `mainline_cache` —— `ml_member_corr` / `ml_stock_theme`；
  · `app_db`（按环境隔离）—— `mainline_alert` / `fact_alerts` / `fact_data_points`；
  · `warehouse` —— `quant_daily_basic`。
  ⚠️ **`app_db` 在 dev/pilot 下是 `data/<env>/moss_<env>.db`，那里没有
  `sector_crowding_*` 也没有 `ml_*`** —— 实测（2026-09-29）：dev 库 36 张表、
  五张拥挤度/映射表**全部不存在**。所以每张表都按**候选存储序列**解析
  （`_TABLE_CANDIDATES`），命中的存储名写进 `extra.store`，**可复核**。

## 资源上限（数字都在代码里，不留在注释里）

进程内缓存 + TTL，键含 code；`reset_cache()` 供测试清空。

* **估值水位（平台路径）**：0 条本连接器的 SQL（PE/PB 走采集链自己的 TTL/DB 缓存）；
  实测 1110 个 PE 点；TTL 6h。
* **估值水位（行情仓路径）**：**1 条语义查询**
  （`code=? AND trade_date<=?` 全历史，索引 `(code,trade_date)`）；
  实测 600036 = 4,981 行 / **806 ms**（冷）；TTL 6h。
* **概念拥挤度**：4 条 —— 相关度 ×2（`code=?`，索引，**0.05 ms**）
  + 拥挤度截面（`IN (...) AND trade_date=(相关子查询)`，走
  `idx_crowding_sector_date`，**218 万行表不扫全表**，66 个板块实测 **23 ms**）
  + 看板板块池（1,226 行）；覆盖 ≤ ~130 个板块；TTL 24h / 6h / 24h。
* **主线告警**：共享上面的相关度缓存 + 1 条（`board_code IN (...)`，
  索引 `(board_code, trade_date)`）；实测 ≤ ~130 个板块的告警行（全库 2,869 行）；TTL 1h。
* **个股告警**：1 条（`LIKE '%<code>%'` 预筛 + **内存里精确校验**，防子串假命中）；
  实测 `fact_alerts` 全表 main 7 / dev 124 / pilot 500 行；TTL 1h。
* **解禁计划**：1 条（**全进程共享**，
  `indicator >= 'cal:unlock:' AND indicator < 'cal:unlock;'`）；
  实测 87~150 行 / **0 ms**；TTL 6h。

⚠️ **`cal:unlock:` 不许用 `LIKE 'cal:unlock:%'`** —— 实测
`EXPLAIN QUERY PLAN` 显示它退化成 `SCAN ... USING COVERING INDEX`（200 万行，**107 ms**），
改成前缀范围扫描后是 `SEARCH ... (indicator>? AND indicator<?)`（**0 ms**）。
⚠️ **`sector_crowding_daily` 不许用"全局最新交易日截面"代替"每个板块自己的最新日"** ——
实测 2,510 个板块里 **1,038 个**自身最新日早于全局最新日，用全局截面会让这 1,038 个
板块"看起来没有拥挤度"（静默丢数据）。

## 已知缺口（诚实登记，不修）

1. **多租户未过滤**：`fact_alerts.tenant_id` 实测恒为 `tenant_001`，本连接器是数据层
   生产者、**不做 RLS**，租户原样带进 `extra.tenant_id` 由上层处置。
2. **`mainline_alert` 的 `weak` 档被过滤掉**（用户要的是"中高强度"），
   `extra.levels_filtered` 明写这一事实；要全档请改 `_MAINLINE_ALERT_LEVELS`。
3. **`解禁计划` 的个股明细没有登记成指标**：`calendar_store` 只登记了
   `cal:unlock:market_cap` / `company_count` / `top_stock_cap` 三条**聚合**指标，
   个股明细装在 `extra_json` 里 ⇒ "登记面"缺口（不是数据面缺口，由本连接器读 `extra_json` 兜住）。
4. **估值退化路径不含同业/行业中位数**（见上），且样本集与百度口径不同（724 vs 1110 点）。
5. **`sector_crowding_daily.water_level` 有大量 NULL**（全表 150,403 行；最新截面 74/2,510）——
   水位未量到的板块**原样下发**（`water_level: null` + `water_level_measured: false`），不填 0。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.intel_limits import UNLOCK_DETAIL_TOP_N
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.catalog import data_stores
from src.infrastructure.connectors.base import BaseConnector
from src.intraday.config import load_intraday_config
from src.intraday.sources import IntradayDataProvider
from src.intraday.valuation import (
    HEADROOM_LABELS,
    ValuationProvider,
    compute_headroom_score,
    headroom_bucket,
    valuation_from_series,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 一、指标族 → 口径（`supports()` / `get_capabilities()` 的唯一来源）
# ============================================================================
_FAMILY_CALIBERS: dict[str, str] = {
    "估值水位": (
        "估值空间分 ∈[-1,1]（正=有上涨空间、负=估值透支）+ 五档结论；"
        "档位/标签/合成分的唯一来源是 `src/intraday/valuation.py`"
    ),
    "概念拥挤度": (
        "该股**相关度最高**的概念板块的 `water_level`（水位 0~1）；"
        "每条记录把「相关度 + 拥挤度」成对下发"
    ),
    "主线告警": (
        "窗口内该股所属概念的中/高强度主线挖掘告警；每告警一点，value=告警分"
    ),
    "个股告警": (
        "`fact_alerts.affected_stocks_json` 点名该 code 的告警；"
        "value=方向性分数（positive→opportunity_score，否则 risk_score，**原样直取**）"
    ),
    "解禁计划": (
        "窗口内该股的限售解禁计划（`fact_data_points.extra_json.top_stocks`）；"
        "value=该解禁日解禁市值合计（元）"
    ),
    # ---- 行业侧三族（按**行业名**占位，不是 6 位代码）----
    "行业拥挤度": (
        "行业名 → **最相近的所属概念板块**（`sector_crowding_daily.sector_name` "
        "最长匹配）→ 该板块 `water_level`；本地匹配不上时不产点，"
        "**由采集链的联网兜底继续去找**（口径 2026-09-29 变更）"
    ),
    "板块资金流": (
        "板块/行业名 → 近 N 日主力净流入合计（元）+ 方向 + 最新截面 + 榜单排名"
    ),
    "行业轮动": (
        "「行业轮动日报」里该行业那一行 + 全局研判（headline/views）；"
        "value=该行业当日涨跌幅（%）"
    ),
}

#: `supports()` 认的前缀全集（**派生**，与 capabilities 同一张表）。
_SUPPORTED_PREFIXES: tuple[str, ...] = tuple(f"{p}:" for p in _FAMILY_CALIBERS)

#: 按**行业名/板块名**占位的族（占位是行业名，不是 6 位代码）。
#:
#: ⚠️ 占位符只能写 `{行业名}` —— `test_contract_consistency.py` 的
#: `_PLACEHOLDER_SAMPLES` 只登记了 `{code, YYYY-MM-DD, 指数名, 行业名, 赛道名,
#: 行业关键词}` 六个，`capabilities` 里出现别的占位符会让
#: `test_connector_capabilities_agree_with_supports` **直接红**
#: （那个文件不在本次可改范围）。所以 `板块资金流` 也写 `{行业名}` ——
#: 板块名与行业名在这三族里本来就是同一个口径（都来自板块榜单/拥挤度表）。
_INDUSTRY_NAME_FAMILIES: tuple[str, ...] = ("行业拥挤度", "板块资金流", "行业轮动")

# ============================================================================
# 二、存储名（**只写 store 名，绝不写路径字面量**）
# ============================================================================
_APP_DB = "app_db"
_LEGACY_MAIN = "legacy_main"
_MAINLINE_CACHE = "mainline_cache"
_WAREHOUSE = "warehouse"

#: 逻辑表 → **候选存储序列**（命中的第一个写进 `extra.store`，可复核）。
#:
#: 为什么不写死一个：实测 `app_db` 是**按环境隔离**的，dev/pilot 下
#: （`data/<env>/moss_<env>.db`）**没有** `sector_crowding_*` / `ml_*`；
#: 而主实例下 `app_db` 与 `legacy_main` 是同一个文件。写死一个必然在某个实例上静默取空。
_TABLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    # 参考数据：留在**共享**库（无用户维度，三个环境读同一份）
    "sector_crowding_daily": (_LEGACY_MAIN, _APP_DB),
    # ★★ 用户配置：`CHG-0143` 起在**本环境应用库**（原先在共享遗留主库）。
    #
    # ⚠️ **候选顺序必须 `app_db` 在前** —— `_table_store()` 取**第一个存在该表**
    # 的候选。旧库里那张 `sector_crowding_list` 不会自动消失，若把
    # `_LEGACY_MAIN` 放前面，迁移会**静默失效**：接口照常返回旧库那份
    # （dev 与 pilot 仍共用一份板块清单），而**没有任何报错**。
    # 判据 `test_crowding_list_prefers_app_db` 钉住这个顺序。
    "sector_crowding_list": (_APP_DB, _LEGACY_MAIN),
    # ★ 2026-09-29：`_load_crowding()` 的 LEFT JOIN 读了这张表（"每个板块的历史最高
    #   平滑拥挤度"），但**声明里原来没有它** —— 于是"这张表在哪"无从解析，
    #   而后缀诊断/字段可达性判据都看不见它。实测由
    #   `test_connector_field_reachability.py::test_no_undeclared_table_is_read` 抓到。
    "sector_crowding_max_ma5": (_LEGACY_MAIN, _APP_DB),
    "ml_member_corr": (_MAINLINE_CACHE,),
    "ml_stock_theme": (_MAINLINE_CACHE,),
    "mainline_alert": (_APP_DB, _LEGACY_MAIN),
    "fact_alerts": (_APP_DB, _LEGACY_MAIN),
    "fact_data_points": (_APP_DB, _LEGACY_MAIN),
    "quant_daily_basic": (_WAREHOUSE,),
}

# ============================================================================
# 三、窗口与上限（**数字写进代码**）
# ============================================================================
#: `ml_stock_theme` 的**溯源列**：这个主营相关分是"谁在什么时候按哪个模板打的"。
#:
#: 实测样例：`model='reasoning'`（LLM **层名**，不是模型 id）、
#: `prompt_sig='member_pure_v1'`（prompt 模板指纹）、
#: `scored_at='2026-09-26T10:58:31+08:00'`、`raw_name='宠物经济'`（归一前的原名）。
#: 原实现只读 `rank/theme/business_score/corr/final_score/reason`，
#: 这 4 列**在库里没人读**（由字段可达性判据 A 现读 `PRAGMA table_info` 抓到）。
#: 为什么值得下发：`final_score` 是**LLM 打的分**，不带出处的分数会被当成客观读数。
_THEME_PROVENANCE_FIELDS: tuple[str, ...] = (
    "raw_name", "model", "prompt_sig", "scored_at")

#: 上表字段的人话口径（随 `extra.theme_provenance_basis` 下发）。
_THEME_PROVENANCE_BASIS = (
    "ml_stock_theme 的逐条溯源：`raw_name`=归一前的原名（如 '宠物经济'）；"
    "`model`=**LLM 层名**（reasoning/light…，不是模型 id）；"
    "`prompt_sig`=prompt 模板指纹（模板一改，旧分与新分不可比）；"
    "`scored_at`=打分时间 ⇒ business_score/final_score 是**当时**的判定，"
    "不是今天重算的。列在本环境缺失时该键**不出现**（不填 0、不填 '未知'）"
)

#: `解禁计划` 的默认窗口：用户原话是"未来一个月"。
_UNLOCK_WINDOW_DAYS = 30
#: 投资日历 JSON 明细**每天只留市值最大的 N 只**（`calendar_store` 的 `[:10]`）。
#: ★ 这个数字是"回退路径的结论强度上限"：它没出现的标的**不能**判成"无解禁"。
#: 单一真值源在 `src/core/intel_limits.py::UNLOCK_DETAIL_TOP_N`（生产者与消费者同源），
#: 这里只做别名 —— 写两份常量必然漂移，而漂移的症状是**结论强度失真**。
_UNLOCK_JSON_TOP_N = UNLOCK_DETAIL_TOP_N
#: 主线/个股告警的默认回看窗口（"最近"）。
_ALERT_WINDOW_DAYS = 90
#: `估值水位` 的**窗口**（见 `_VALUATION_WINDOW_DAYS` 的下一条）与**随点下发的原始字段**。
#:
#: ★ 为什么要下发这些列（用户要求 2026-09-29）：
#: > 「连接器确保数据库里的所有字段（至少相关表都要遍历到）**可达数据库、可匹配获取到**」
#:
#: 实测：`quant_daily_basic` 有 19 列，而原实现只读了 `pe_ttm` / `pb`
#: （`dv_ttm`/`total_mv` 由 `akshare_connector` 与 `mainline` 读）。其余列
#: **在库里、值也在、就是没有一条取数路径读它** —— 由
#: `tests/unit/test_connector_field_reachability.py` 判据 A 从 `PRAGMA table_info`
#: 现读抓到（**不是**手写清单：库里加一列会自动进入判据）。
#: 一并下发还有个直接好处：估值分这个结论**可以被复核**（PS 与 PE 背离、
#: 自由流通盘很小导致换手虚高 —— 这两件事单看 PE/PB 永远看不出来）。
#: ⚠️ 单位是**共享行情仓的存储单位**（Tushare 原始值 ×1e4：万元→元、万股→股），
#: 口径写在 `_VALUATION_RAW_BASIS` 里随 extra 一起给，不让调用方猜。
_VALUATION_RAW_FIELDS: tuple[str, ...] = (
    "close_basic", "ps", "ps_ttm", "turnover_rate", "turnover_rate_f",
    "total_share", "float_share", "free_share", "total_mv", "circ_mv",
    "dv_ratio", "dv_ttm", "volume_ratio",
)

#: 上表每个字段的**人话口径**（列名 → 单位/含义）。
#:
#: 单一真值源：口径只写在这里，`extra.raw_inputs.basis` 直接引用本表。
#: 两条最容易被混用的写清楚了：`turnover_rate` 是**流通**口径、
#: `turnover_rate_f` 是**自由流通**口径，**不是同一个数**（实测同一行 0.9989 vs 1.0074）。
_VALUATION_RAW_BASIS: dict[str, str] = {
    "close_basic": "未复权收盘价（元）—— 复权序列走行情主表，两者不许混用",
    "ps": "市销率（总市值/营业收入）",
    "ps_ttm": "市销率（TTM）",
    "turnover_rate": "换手率（%，**流通股本**口径）",
    "turnover_rate_f": "换手率（%，**自由流通股本**口径）—— 与 turnover_rate 不是同一个数",
    "total_share": "总股本（股）",
    "float_share": "流通股本（股）",
    "free_share": "自由流通股本（股）",
    "total_mv": "总市值（元）",
    "circ_mv": "流通市值（元）",
    "dv_ratio": "股息率（%）",
    "dv_ttm": "股息率（TTM，%）",
    "volume_ratio": "量比",
}

#: `估值水位` 的**平台口径**窗口：采集链取的是百度 `period="近三年"`（`akshare_connector`）。
#: ⚠️ 这不是新阈值，是与平台对齐；改它 = 改口径，必须同时改采集侧。
_VALUATION_WINDOW_DAYS = 365 * 3
#: `概念拥挤度` top N（按相关度排序后的展示条数）。
_RELATED_TOP_N = 10
#: `ml_stock_theme`（主营相关度）top N。
_THEME_TOP_N = 5
#: 主线告警/个股告警一次最多产多少点（超出记 `truncated=True` 并告知）。
_ALERT_MAX_POINTS = 10
#: `主线告警` 只取中/高强度（用户原话"中高强度"）。`weak` 被过滤并在 extra 明写。
_MAINLINE_ALERT_LEVELS: tuple[str, ...] = ("medium", "strong")
#: `板块资金流` 的"近期"窗口（交易日数）。
_FUNDFLOW_WINDOW_DAYS = 5
#: `板块资金流` 历史序列的**等待上限**（秒）—— 上限必须写进代码，不留在注释里。
#:
#: 实测（2026-09-29 本机）：`FundFlowProvider.sector_history()` 在东财
#: `RemoteDisconnected` 时**冷调用 35.6s**（子进程 timeout=60），而板块**截面**
#: （Tushare `moneyflow_ind_dc`）只要 **0.7s 且实测可用**。
#: 所以：截面优先；历史序列最多等 6 秒，超时就用截面当日口径并在 extra 里写明。
_FUNDFLOW_HISTORY_TIMEOUT = 6.0
#: 历史序列判定不可用后的**冷却**（秒）：冷却期内直接走截面口径，不再白等一次。
#:
#: 为什么必须有：`FundFlowProvider` 自己把失败结果缓存 1800s，但那只覆盖
#: "**已经拿到失败结论**"的调用；超时被我们主动放弃时它**没写缓存**，
#: 于是下一次调用会再付一次 6 秒。用一层 15 分钟冷却兜住。
_FUNDFLOW_HISTORY_COOLDOWN = 900
#: `行业轮动` 的热力榜取前 N（复用 `service.pick_heat`，不另写排序）。
_ROTATION_TOP_N = 5

#: 匹配不上时的诊断话术。
#:
#: ⚠️ **口径已变更（2026-09-29，用户原话）**：
#: > 「"找不到就不提示"改成 **找不到就去联网搜索找**」
#:
#: 所以"匹配不上"不再是终点 —— 采集链会把这条指标交给联网兜底
#: （`supervisor._network_lookup_for_collection`，走 `NetworkFallback` 的白名单 +
#: 预算 + 冷却熔断）。本常量只用于**诊断文案**：它说明"本地这一跳为什么没产点"，
#: 而不是"这件事到此为止"。
_NO_MATCH_REASON = "（本地无相近匹配 → 已交联网兜底去找）"

# ============================================================================
# 四、进程内缓存 TTL（秒）
# ============================================================================
_CONCEPT_RELATEDNESS_TTL = 24 * 3600   # ml_member_corr / ml_stock_theme（日频重算）
_CROWDING_TTL = 6 * 3600               # sector_crowding_daily（日频表）
_BOARD_POOL_TTL = 24 * 3600            # sector_crowding_list（板块池，低频变更）
_ALERTS_TTL = 3600                     # mainline_alert / fact_alerts（事件驱动）
_UNLOCK_TTL = 6 * 3600                 # cal:unlock:*（日历，日频）
_VALUATION_TTL = 6 * 3600              # 行情仓 PE/PB 序列（日频）
_TABLE_PROBE_TTL = 24 * 3600           # 表在哪个存储里（不会运行时变）
_ROTATION_TTL = 3600                   # 行业轮动日报（落盘文件，读一次很便宜）
#: `行业轮动` 正文的**上限**（保护上游上下文；截断时置 `body_truncated=True`）。
_ROTATION_BODY_MAX = 1500

# ============================================================================
# 五、通用工具
# ============================================================================


@dataclass
class _CacheSlot:
    at: float
    value: Any


_CACHE: dict[str, _CacheSlot] = {}
_CACHE_LOCK = threading.Lock()


def reset_cache() -> None:
    """清空进程内缓存（测试用）。

    ⚠️ **连板块资金流的失败冷却一起清**：那是"历史序列不可用"的全局状态
    （实测东财被阻断时整机不可用），留着它会让**下一个用例**静默走截面口径 ——
    即"用例之间互相喂状态"的假绿。
    """
    with _CACHE_LOCK:
        _CACHE.clear()
    _FUNDFLOW_HISTORY_STATE.clear()


#: 板块资金流 provider 的**进程内单例**。
#:
#: ⚠️ 每次 fetch 新建一个 `FundFlowProvider` 会让它自带的 TTL 缓存**全部失效**
#: （截面/历史/成分各自带 TTL），于是每次查询都重付一轮网络取数。
_FUNDFLOW_PROVIDER: Any = None

#: 板块资金流**历史序列**的最近一次失败（冷却用）：`{"at": 时间戳, "reason": 原因}`。
_FUNDFLOW_HISTORY_STATE: dict[str, Any] = {}


def _fundflow_provider() -> Any:
    """`FundFlowProvider` 进程内单例（自带 TTL 缓存，见上）。"""
    global _FUNDFLOW_PROVIDER
    with _CACHE_LOCK:
        if _FUNDFLOW_PROVIDER is None:
            from src.fundflow.provider import FundFlowProvider

            _FUNDFLOW_PROVIDER = FundFlowProvider()
        return _FUNDFLOW_PROVIDER


def _cached(key: str, ttl: float, loader: Callable[[], Any]) -> Any:
    """进程内 TTL 缓存。

    ⚠️ **失败不入缓存**：本连接器读的是本地 SQLite，取不到就是配置/环境问题，
    负缓存只会把"修好了但进程还记着旧失败"变成新的排查方向。
    （`ComplianceFinConnector` 的失败冷却针对的是**整表拉 254 个分页**的网络源，
    两者不是一回事。）
    """
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None and now - hit.at < ttl:
        return hit.value
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = _CacheSlot(now, value)
    return value


def _store_path(name: str) -> Path:
    """存储路径的**唯一解析入口**（测试 monkeypatch 本函数即可模拟库缺失）。"""
    return data_stores.resolve_store(name)


def _today() -> str:
    return date.today().isoformat()


def _iso_date(raw: Any) -> str | None:
    """`'20260924'` / `'2026-09-24'` / `datetime` → `'2026-09-24'`。"""
    if raw is None:
        return None
    if isinstance(raw, (datetime, date)):
        return raw.strftime("%Y-%m-%d")
    text = str(raw).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text or None


def _compact_date(iso: str) -> str:
    """`'2026-09-24'` → `'20260924'`（库里 `trade_date` 是紧凑格式）。"""
    return "".join(ch for ch in iso if ch.isdigit())[:8]


def _shift_days(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def _as_of(end_date: str | None) -> str:
    """窗口右端：给定则用给定的（并夹到 today —— 调用方很自然地传"年窗口"）。"""
    today = _today()
    if not end_date:
        return today
    iso = _iso_date(end_date) or today
    return min(iso, today)


def _window(start_date: str | None, end_date: str | None, days: int,
            *, forward: bool = False) -> tuple[str, str]:
    """`(start, end)`。

    * `forward=False`（回看族）：默认 `[end - days, end]`，且 `end` **夹到 today**
      —— 调用方很自然地传"年窗口"（`end_date="2026-12-31"`），不夹就会去问未来区间。
    * `forward=True`（前瞻族，本连接器的 `解禁计划`）：默认 `[today, today + days]`，
      且**不夹到 today** —— "未来一个月有没有解禁"问的就是未来；
      传 `end_date="2030-01-31"` 时必须真的按 2030 年查（否则永远只看得到未来 30 天，
      而 `diagnose` 也永远给不出"该窗口没量到"）。
    """
    today = _today()
    given_end = _iso_date(end_date) if end_date else None
    given_start = _iso_date(start_date) if start_date else None
    if forward:
        end = given_end or _shift_days(today, days)
        start = given_start or today
    else:
        end = min(given_end or today, today)
        start = given_start or _shift_days(end, -days)
    if start > end:
        start, end = end, start
    return start, end


def _loads(raw: Any) -> Any:
    """JSON 列 → 对象；坏 JSON / 空串 → None（**不抛**，由调用方按语义处置）。"""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _digits_code(raw: Any) -> str:
    """任意形态的代码 → 6 位数字串（`'600036'` / `600036.0` / `' 600036 '`）。"""
    text = str(raw or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _table_store(table: str) -> str:
    """这张表在哪个存储里（**按候选序列探一次，24h 内复用**）。"""
    def _probe() -> str:
        candidates = _TABLE_CANDIDATES.get(table) or ()
        for store in candidates:
            path = _store_path(store)
            if not path.exists():
                continue
            with _readonly(store) as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
            if row is not None:
                return store
        raise DataFetchError(
            f"表 {table} 在候选存储 {list(candidates)} 里都不存在"
            f"（平台自有数据连接器**只读**这些登记存储，找不到就明说，不猜路径）")

    return str(_cached(f"table_store:{table}", _TABLE_PROBE_TTL, _probe))


@contextmanager
def _readonly(store: str) -> Iterator[sqlite3.Connection]:
    """只读连接（`mode=ro` + `PRAGMA query_only=1`），**绝不写任何库**。"""
    path = _store_path(store)
    if not path.exists():
        raise DataFetchError(
            f"存储 {store!r} 不存在（{path}）—— 平台自有数据连接器只读登记存储，"
            "库不在就报错，不静默返回空、更不返回 0")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DataFetchError(
            f"存储 {store!r} 打不开（{type(exc).__name__}: {exc}）") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        yield conn
    except sqlite3.Error as exc:
        raise DataFetchError(
            f"存储 {store!r} 查询失败（{type(exc).__name__}: {exc}）") from exc
    finally:
        conn.close()


def _table_columns(table: str) -> frozenset[str]:
    """这张表**实际有的列**（`PRAGMA table_info`，24h 内复用）。

    为什么"多读几个原始字段"也必须过它：共享行情仓的导入列**随环境不同**
    （实测 `quant_daily_basic.dv_ratio` 只在 dev 库有 1184 万行 —— 见
    `infrastructure/catalog/column_index.py` 的登记）。把列名写死在 SELECT 里，
    在没有该列的环境会 `no such column` ⇒ **本来好用的族整族打挂**（"修一个坏一个"）。
    所以分工是：代码写清"**想要**哪些列"，这个函数回答"**真的有**哪些列"。
    """
    def _probe() -> frozenset[str]:
        store = _table_store(table)
        with _readonly(store) as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return frozenset(str(row["name"]) for row in rows)

    return frozenset(_cached(f"table_columns:{table}", _TABLE_PROBE_TTL, _probe))


def _selectable(table: str, wanted: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """`wanted` 拆成 `(表里真有的列, 表里没有的列)`，**两者都保序**。

    前者进 SELECT，后者**随 extra 一起下发** —— 缺列要看得见（"没量到"≠"量到 0"），
    不许静默少下发几个字段。
    """
    present = _table_columns(table)
    return ([column for column in wanted if column in present],
            [column for column in wanted if column not in present])


@dataclass(frozen=True)
class _SeriesPoint:
    """喂给 `valuation_from_series` 的最小载体（它按 `getattr(p,'value')` 读）。"""

    value: float
    period_date: str


def _finite_number(value: Any) -> float | None:
    """任意值 → float；`None`/NaN/非数字 → None（**不是 0**）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result      # NaN != NaN


def _norm_name(text: Any) -> str:
    """板块/行业名的归一化（**只做大小写与空白**，不删词、不做同义词）。

    刻意不做"去后缀/去括号"这类清洗：那会让 `银行(A股)` 与 `银行` 变成同一个键，
    而它们在拥挤度表里是**两个不同的板块**（`700334.TI` / `881155.TI`），
    合并了就再也说不清"这个水位是谁的"。
    """
    return "".join(str(text or "").split()).lower()


#: **跨分类体系的行业同义词**：名录行业名 → 平台板块池名。
#:
#: ## 为什么必须有（用户 2026-09-29 报障）
#:
#: > 「未获取到 `行业轮动:电气设备` 数据 —— 数据库里肯定有，网上也有」
#:
#: 他说得对：**名录**（`quant_stock_basic.industry`，110 个，Tushare 口径）里的
#: `电气设备`，在平台板块池（东财口径，轮动日报 457 个 / 拥挤度表 2510 个）里
#: 叫 **`电力设备`** —— 两套分类体系对同一批公司用了不同的名字。
#: 精确/包含两档都命中不了（`电气设备` 与 `电力设备` 互不为子串）⇒ 不产点。
#:
#: ## 为什么**不做模糊匹配**（实测否决，不是偏好）
#:
#: 本条判据上线前实测过"按字符相似度兜底"（`difflib.SequenceMatcher`）：
#: 名录 110 个行业里相似度 ≥0.5 的有 63 对，而**语义正确的只有少数**：
#:
#:     0.750  电气设备 → **火电设备**   ← 真要的是「电力设备」，模糊匹配给的是这个
#:     0.667  其他商业 → 其他种植业 · 0.500  铁路 → 钢铁 · 0.500  保险 → 环保
#:     0.500  白酒 → 啤酒 · 0.500  水务 → 水泥 · 0.571  日用化工 → 氟化工
#:
#: **错配的板块比"没数据"更危险**：用户会拿到一个"看着有据"的错误板块水位
#: （AGENTS.md：数字看着有据、语义是错的，比缺数据更危险）。
#: 所以只认**逐条复核过的**同义词，并把档位随数据下发（`match_tier="alias"`），
#: 让人一眼看出"这是被映射过的"。
#:
#: ## 过期语义（**不许变成永久豁免**）
#:
#: `tests/unit/test_board_name_alias.py` 断言：① 每个**源名**都必须是名录里
#: 真实存在的行业（名录改名 → 红）；② 每个**目标名**必须出现在当前板块池里
#: （板块池改名/下线 → 红）。两条都是"修好后条目必须被删/改"的过期检查。
_BOARD_NAME_ALIASES: dict[str, str] = {
    # 名录「电气设备」= 东财「电力设备」（同一批公司；实测成员高度重叠）
    "电气设备": "电力设备",
}


def _match_board_name(industry: str, candidates: list[str]) -> tuple[str | None, str]:
    """行业名 → **最相近**的板块名，返回 `(板块名|None, 匹配档位)`。

    档位只有四种，**确定性、可复核**：

      * `exact`     —— 归一化后完全相等（优先，取名字字典序第一条）；
      * `alias`     —— 跨分类体系的**已复核同义词**（`_BOARD_NAME_ALIASES`，
        如 名录`电气设备` → 板块池`电力设备`）；
      * `contains`  —— 互为子串，按「命中长度降序 → 板块名长度升序 → 名字字典序」
        取第一条（即用户说的"**最相近**"：`银行` → `银行(A股)`，而不是
        `货币金融服务指数`）；
      * `none`      —— 找不到 → 本连接器**不产点**；采集链随后交给**联网兜底**
        继续找（口径 2026-09-29 变更：「找不到就去联网搜索找」）。

    ⚠️ 候选池由调用方给（拥挤度族给 `_load_name_index()` 的键、轮动族给日报
    `industries` 的名字），所以两个族的候选池**同源**，不各写一套模糊匹配。
    """
    target = _norm_name(industry)
    if not target:
        return None, "none"
    exact = sorted(name for name in candidates if _norm_name(name) == target)
    if exact:
        return exact[0], "exact"
    # ★ 同义词档：映射目标是**当前候选池里真实存在**的那个名字才用（否则等于
    #   把用户指向一个不存在的板块 —— 那比"匹配不上"更难查）。
    alias = _BOARD_NAME_ALIASES.get(str(industry or "").strip())
    if alias:
        hit = sorted(name for name in candidates if _norm_name(name) == _norm_name(alias))
        if hit:
            return hit[0], "alias"
    hits: list[tuple[int, int, str]] = []
    for name in candidates:
        key = _norm_name(name)
        if not key:
            continue
        if target in key or key in target:
            hits.append((min(len(target), len(key)), -len(key), name))
    if not hits:
        return None, "none"
    hits.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return hits[0][2], "contains"


@dataclass(frozen=True)
class FamilyDiagnosis:
    """为什么这一族这次产不出点（**空 ≠ 0**，空必须有理由）。"""

    family: str
    code: str
    reason: str
    detail: dict[str, Any]


# ============================================================================
# 六、各族取数（供 fetch 与 diagnose 共用，**单一实现**）
# ============================================================================


class PlatformDataConnector(BaseConnector):
    """平台自有后端数据（拥挤度 / 主线 / 估值 / 日历解禁）。

    ⚠️ `估值水位` 的档位与标签**不在本类里定义** —— 唯一来源是
    `src/intraday/valuation.py`（见模块 docstring 纪律一）。
    """

    source_name = "平台自有后端数据"
    source_url = "store://platform"

    def __init__(self, backend: Any = None) -> None:
        #: 项目采集链（`ConnectorRouter`）。注入后 `估值水位` 走**平台权威路径**
        #: （`ValuationProvider`：个股分位 + 同业 + 行业中位数），与前端逐字段一致。
        #: 不注入时退化到本地行情仓序列（仍用同一组判据函数，局限随 extra 下发）。
        self._backend = backend
        self._valuation_provider: ValuationProvider | None = None
        self._valuation_cfg: Any = None
        self._valuation_backend: Any = None

    # ------------------------------------------------------------------ 契约

    def bind_backend(self, backend: Any) -> None:
        """回注采集链。

        为什么是"回注"而不是构造参数：`ConnectorRouter` 必须在**全部路由装配完之后**
        才存在（`src/api/runtime.py`），所以连接器先构造、路由建好后再绑定。
        """
        if backend is not self._backend:
            self._backend = backend
            self._valuation_provider = None
            self._valuation_backend = None

    def get_capabilities(self) -> dict[str, Any]:
        # ★ 占位符**按族给真实值**（单一真值源 = `_INDUSTRY_NAME_FAMILIES`）。
        #   全部声明成 `{code}` 会让护栏在**假前提**下变绿：
        #   `test_contract_consistency._declared_code_prefixes()` 正是从这份
        #   capabilities 里读"`XXX:{code}` 模板根"，而 `supports()` 只做前缀匹配 ——
        #   于是行业三族会被当成"个股类模板根"通过 ③→①，
        #   而 `fetch("行业拥挤度:600036")` 根本不认 6 位代码（见 `fetch()` 的契约校验）。
        #   该文件另有 `_declared_other_prefixes()`（`{行业名}` 那一路）——
        #   两条判据的分工就是为这种区分准备的。
        indicators = [
            f"{prefix}:{{{'行业名' if prefix in _INDUSTRY_NAME_FAMILIES else 'code'}}}"
            for prefix in _FAMILY_CALIBERS
        ]
        return {
            "name": self.source_name,
            "source_type": DataSourceType.FILE.value,
            "indicators": indicators,
            "notes": (
                "平台自己算出来的**八族**后端数据：个股侧五族（估值空间分 / 概念板块拥挤度 / "
                "主线告警 / 个股告警 / 解禁计划，占位 `{code}`）+ 行业侧三族"
                "（行业拥挤度 / 板块资金流 / 行业轮动，占位 `{行业名}`）。"
                "估值档位与标签**复用** `src/intraday/valuation.py`；"
                "板块资金流**复用** `FundFlowProvider`；行业轮动**复用** "
                "`src/sector_rotation`；拥挤度读 `sector_crowding_daily`。"
                "全部**只读**本地登记存储（legacy_main / mainline_cache / app_db / "
                "warehouse）。缺数据一律不产点（`解禁计划` 的「量到 0」例外：窗口内查过"
                "且没有该标的时产 value=0.0 的点，并在 extra 写明依据）；"
                "行业侧三族**本地匹配不上**时不产点，**由采集链的联网兜底继续去找**"
                "（口径 2026-09-29 变更：找不到就去联网搜索找），"
                "但**后缀不是行业名**（6 位代码/空/含冒号）属契约违规，**一律抛错**。"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(_SUPPORTED_PREFIXES)

    def has_related_board(self, industry: str) -> bool:
        """**诊断用**：这个行业在拥挤度板块池里**有没有相近板块**？

        ## ⚠️ 它已经**不是**规划期闸门（口径 2026-09-29 变更）

        旧口径是「**如果找不到就不提示未找到数据**」，当时的落法正是"规划期先问
        本地有没有，没有就**不排**这个指标"（于是采集不跑、界面干净）。
        但那条路**永远不会联网** —— 等于"这个族对用户彻底不存在"。

        现行口径（用户原话）：「**"找不到就不提示"改成 找不到就去联网搜索找**」：
        照排 → 本地/连接器拿不到 → 采集链的**联网兜底**
        （`supervisor._network_lookup_for_collection`）→ 联网也没有才按缺口上报。
        所以本方法**只用于诊断/探针**（"本地这一跳为什么没产点"），
        **不再决定排不排**。

        ## 三条纪律（都写进测试）

        1. **只读 + 走既有缓存**：内部用 `_load_name_index()`（24h TTL），
           **不新增任何取数、不联网**；
        2. **绝不向上抛异常**（调用方可能是规划层，挂了 = 整条任务挂）；
        3. **异常时返回 True（保守放行）**：存储层真的坏了（表缺失/库打不开）
           是真缺口，必须如实上报，不许被这个判定吃掉。
        """
        try:
            name_index = self._load_name_index()
            matched, _tier = _match_board_name(industry, list(name_index))
            return matched is not None
        except Exception as exc:  # noqa: BLE001 判定不许把规划层带崩
            logger.warning(
                "平台自有数据：行业「%s」↔板块池匹配判定失败（保守放行，"
                "真缺口照样如实上报）：%s", industry, exc)
            return True

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        """按族分派。上游异常**一律抛 `DataFetchError`**，不吞成空、更不填 0。"""
        prefix, sep, rest = indicator.partition(":")
        if not sep or prefix not in _FAMILY_CALIBERS:
            raise DataFetchError(
                f"平台自有数据连接器不支持的指标: {indicator!r}"
                f"（支持：{[f'{p}:{{占位}}' for p in _FAMILY_CALIBERS]}）")
        code = rest.strip()

        # 行业侧三族按**行业名/板块名**取数（占位不是 6 位代码）。
        if prefix in _INDUSTRY_NAME_FAMILIES:
            if not code:
                raise DataFetchError(
                    f"{prefix} 需要一个行业名/板块名，收到: {rest!r}"
                    f"（取值模板是 `{prefix}:{{行业名}}`）")
            # ★ 契约校验：后缀**必须是行业名**。6 位数字/含冒号的后缀是**契约违规**
            #   （LLM 可能照旧的 `{code}` 描述写出来），**不许静默返回 `[]`** ——
            #   那会把"契约违规"读成"平台没有这个概念板块"，而两者的下一步动作
            #   完全不同：前者要修调用方；后者是"本地没匹配上"，
            #   **由采集链的联网兜底接着找**（口径 2026-09-29）。
            if code.isdigit() or ":" in code:
                raise DataFetchError(
                    f"{prefix} 的后缀是**行业名/板块名**、不是 6 位代码 —— 收到 "
                    f"{code!r}。取值模板是 `{prefix}:{{行业名}}`"
                    f"（契约违规会如实进 errors，不静默返回空）")
            if prefix == "行业拥挤度":
                points, reason = self._industry_crowding_points(code)
            elif prefix == "板块资金流":
                points, reason = await self._sector_flow_points(code)
            else:
                points, reason = self._rotation_points(code)
            if reason:
                logger.info("平台自有数据：%s:%s 本次不产点 —— %s", prefix, code, reason)
            return points

        if not (len(code) == 6 and code.isdigit()):
            raise DataFetchError(
                f"平台自有数据连接器需要一个 6 位 A 股代码，收到: {code!r}")

        if prefix == "估值水位":
            points, reason = await self._valuation_points(code, start_date, end_date)
        elif prefix == "概念拥挤度":
            points, reason = self._crowding_points(code)
        elif prefix == "主线告警":
            points, reason = self._mainline_alert_points(code, start_date, end_date)
        elif prefix == "个股告警":
            points, reason = self._stock_alert_points(code, start_date, end_date)
        elif prefix == "解禁计划":
            points, reason = self._unlock_points(code, start_date, end_date)
        else:  # pragma: no cover - 上面的分支已穷尽
            raise DataFetchError(f"平台自有数据未实现的分派: {prefix}")

        if reason:
            logger.info("平台自有数据：%s:%s 本次不产点 —— %s", prefix, code, reason)
        return points

    async def diagnose(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> FamilyDiagnosis:
        """回答"这一族这次为什么不产点"（**空 ≠ 0** 的机器可读版本）。

        与 `fetch()` **共用同一批取数函数** —— 不另写一套判据（两套必然漂移）。
        ⚠️ `reason` 只在**真的产不出点**时才写缺口；能产点时写"能产点 ×N"，
        否则调用方会把"有数据"读成"缺数据"（这正是本函数要防的那类误读）。
        """
        prefix, sep, code = indicator.partition(":")
        if not sep or prefix not in _FAMILY_CALIBERS:
            raise DataFetchError(f"不支持的指标: {indicator!r}")
        code = code.strip()

        # ★ 行业侧三族：`reason` 用 `_NO_MATCH_REASON`（说明"本地这一跳为什么没产点"，
        #   并点明"已交联网兜底"——口径 2026-09-29：「找不到就去联网搜索找」）。
        #   能产点时给"能产点 ×N"。
        if prefix in _INDUSTRY_NAME_FAMILIES:
            if prefix == "行业拥挤度":
                points, gap = self._industry_crowding_points(code)
                detail = {"name_index_size": len(self._load_name_index())}
            elif prefix == "板块资金流":
                points, gap = await self._sector_flow_points(code)
                detail = {"window_days": _FUNDFLOW_WINDOW_DAYS}
            else:
                points, gap = self._rotation_points(code)
                detail = self._load_rotation_report()
                detail.pop("payload", None)     # 整份报告太大，diagnose 只留元信息
            return FamilyDiagnosis(
                prefix, code, gap or f"能产点：{len(points)} 条", detail)

        if prefix == "估值水位":
            window_end = _as_of(end_date)
            detail = self._valuation_series(code, window_end, start_date)
            if self._has_valuation_data(detail):
                stats = detail["window_stats"]
                if stats.get("pe_percentile") is None and stats.get("pb_percentile") is None:
                    reason = (
                        f"能取到序列（PE {detail['window_sample']['pe_days']} 点 / "
                        f"PB {detail['window_sample']['pb_days']} 点）但分位无定义"
                        "（percentile_rank 需要 ≥8 个样本）⇒ 档位按平台口径为「数据不足」")
                else:
                    reason = (
                        f"能产点：PE 分位 {stats.get('pe_percentile')} / "
                        f"PB 分位 {stats.get('pb_percentile')}")
            else:
                reason = (
                    f"行情仓 quant_daily_basic 里没有 {code} 在 {window_end} 及之前的 "
                    "PE/PB 读数（该股不在行情仓覆盖范围内）⇒ 没量到")
            return FamilyDiagnosis(prefix, code, reason, detail)

        if prefix == "概念拥挤度":
            points, gap = self._crowding_points(code)
            detail = self._concept_context(code)
            return FamilyDiagnosis(
                prefix, code, gap or f"能产点：{len(points)} 条（按相关度排序）", detail)

        if prefix in ("主线告警", "个股告警"):
            window = _window(start_date, end_date, _ALERT_WINDOW_DAYS)
            detail = dict(self._alert_detail(prefix, code, window))
            gap = str(detail.get("empty_reason") or "")
            return FamilyDiagnosis(
                prefix, code,
                gap or f"能产点：窗口内 {detail['rows_in_window']} 条", detail)

        window = _window(start_date, end_date, _UNLOCK_WINDOW_DAYS, forward=True)
        detail = dict(self._unlock_detail(code, window))
        state = str(detail.get("state"))
        if state in ("unmeasured", "out_of_covered_window"):
            # 这两种都是「没量到」，必须**原样转发**连接器给出的原因
            # （"超出已落库窗口"与"日历没同步"是两件事，各自的下一步动作不同）。
            reason = str(detail.get("reason"))
        elif state == "measured_zero":
            reason = (
                f"能产点（量到 0，逐股表口径）：窗口 {window[0]}~{window[1]} 落在"
                f"已落库覆盖 {detail['common'].get('covered_window', {}).get('start')}~"
                f"{detail['common'].get('covered_window', {}).get('end')} 内，"
                f"表内 {detail['common'].get('table_rows_total')} 行没有该标的"
                f" ⇒ 真结论：无解禁计划")
        elif state == "measured_zero_in_truncated_detail":
            reason = (
                f"能产点但**结论强度受限**：窗口 {window[0]}~{window[1]} 内日历有 "
                f"{len(detail['dates_in_window'])} 个解禁日、"
                f"{detail.get('entries_scanned')} 条明细，未见该标的；"
                f"而该明细每天只留市值最大的 "
                f"{detail['common'].get('detail_truncated_per_day')} 只 ⇒ "
                "只能表述为「未见于明细」（落 unlock_plan 表后可给完整结论）")
        else:
            reason = f"能产点：窗口内 {len(detail['hits'])} 个解禁日命中该标的"
        return FamilyDiagnosis(prefix, code, reason, detail)

    # ------------------------------------------------------------ 族 A：估值水位

    async def _valuation_points(
        self, code: str, start_date: str | None, end_date: str | None,
    ) -> tuple[list[DataPoint], str | None]:
        """估值空间分（value = `space.score` / `score` ∈[-1,1]）。

        平台路径优先（与前端逐字段一致）；取不到才退化到本地行情仓序列。
        """
        window_end = _as_of(end_date)
        space = await self._platform_valuation(code)
        if space is not None and bool(getattr(space, "available", False)):
            return self._point_from_space(code, space, window_end), None

        series = await asyncio.to_thread(
            self._valuation_series, code, window_end, start_date)
        if not self._has_valuation_data(series):
            return [], (
                f"行情仓 quant_daily_basic 里没有 {code} 在 {window_end} 及之前的 "
                "PE/PB 读数（该股不在行情仓覆盖范围内）")
        return self._point_from_series(code, series, window_end, start_date), None

    @staticmethod
    def _has_valuation_data(series: dict[str, Any]) -> bool:
        return bool(series.get("pe_days_full") or series.get("pb_days_full"))

    def _raw_inputs(self, code: str, window_end: str) -> dict[str, Any]:
        """`quant_daily_basic` 最新一行的**原始字段**（供人复核估值分）。

        与 `_valuation_series` 分开：这里只取**一行**（走 `(code, trade_date)` 索引，
        毫秒级），所以**两条估值路径都能带上它** —— 同一口径两份实现必然漂移，
        而漂移的症状是"界面一个数、Agent 另一个数"。

        ⚠️ **取不到不许打挂这一族**：本函数是**附加信息**，共享行情仓在试点/主实例上
        可能压根没有这张表（`catalog/column_index.py` 登记过这个环境差异）。
        所以失败时返回**带原因的缺口**（`unavailable_reason` + `absent_columns`），
        而不是抛异常，也**不填 0** —— "没量到"必须看得见。
        """
        def _load() -> dict[str, Any]:
            try:
                store = _table_store("quant_daily_basic")
                fields, absent = _selectable(
                    "quant_daily_basic", _VALUATION_RAW_FIELDS)
                if not fields:
                    return {"values": {}, "absent_columns": absent,
                            "unavailable_reason": (
                                "共享行情仓的 quant_daily_basic 一列都没匹配上")}
                projection = ", ".join(["trade_date", *fields])
                with _readonly(store) as conn:
                    row = conn.execute(
                        f"SELECT {projection} FROM quant_daily_basic "
                        "WHERE code = ? AND trade_date <= ? "
                        "ORDER BY trade_date DESC LIMIT 1",
                        (code, _compact_date(window_end))).fetchone()
            except (DataFetchError, sqlite3.Error) as exc:
                logger.warning(
                    "平台自有数据：quant_daily_basic 原始字段取不到（%s）—— "
                    "随 extra 如实披露，不影响估值分本身", exc)
                return {
                    "values": {},
                    "absent_columns": list(_VALUATION_RAW_FIELDS),
                    "unavailable_reason": (
                        f"本环境读不到共享行情仓 quant_daily_basic"
                        f"（{type(exc).__name__}: {exc}）⇒ 原始字段**没量到**"
                        "，不是 0；估值分来自另一条路径，仍有效"),
                }
            return {
                "as_of": _iso_date(row["trade_date"]) if row else None,
                "values": ({column: row[column] for column in fields}
                           if row else {}),
                "absent_columns": absent,
                "unavailable_reason": (
                    None if row else
                    f"行情仓里没有 {code} 在 {window_end} 及之前的行"),
            }

        return dict(_cached(
            f"raw_inputs:{code}:{window_end}", _VALUATION_TTL, _load))

    @staticmethod
    def _raw_inputs_extra(raw: dict[str, Any]) -> dict[str, Any]:
        """`_raw_inputs()` → `extra.raw_inputs`（口径随数据一起下发）。"""
        payload: dict[str, Any] = {
            "store": "warehouse::quant_daily_basic",
            "as_of": raw.get("as_of"),
            "values": raw.get("values") or {},
            "absent_columns": raw.get("absent_columns") or [],
            "basis": _VALUATION_RAW_BASIS,
        }
        if raw.get("unavailable_reason"):
            payload["unavailable_reason"] = raw["unavailable_reason"]
        return payload

    async def _platform_valuation(self, code: str) -> Any | None:
        """平台权威路径：`ValuationProvider.fetch()`（与前端同一份实现与缓存）。

        未注入采集链时返回 None（调用方退化到本地行情仓路径，并在 extra 披露）。
        """
        if self._backend is None:
            return None
        try:
            cfg = load_intraday_config(None)   # 按 mtime 缓存，进程内只解析一次
            if (self._valuation_provider is None
                    or self._valuation_cfg is not cfg
                    or self._valuation_backend is not self._backend):
                self._valuation_provider = ValuationProvider(
                    cfg, IntradayDataProvider(cfg), self._backend)
                self._valuation_cfg = cfg
                self._valuation_backend = self._backend
            watch = cfg.watch(code)            # 与界面同路：在自选池则带 peers/industry
            return await self._valuation_provider.fetch(
                code, name=watch.name if watch else "", watch=watch)
        except Exception as exc:  # noqa: BLE001 估值面板不可用不该拖垮整个采集
            logger.warning(
                "平台自有数据：ValuationProvider 取数失败（%s），退化到本地行情仓序列：%s",
                code, exc)
            return None

    def _point_from_space(
        self, code: str, space: Any, window_end: str,
    ) -> list[DataPoint]:
        """把 `ValuationSpace` 序列化成**一个** DataPoint（value = score）。"""
        score = space.score
        bucket = headroom_bucket(score)
        label = HEADROOM_LABELS[bucket]
        extra: dict[str, Any] = {
            "valuation_path": "valuation_provider",
            "valuation_path_zh": "平台权威路径（ValuationProvider，与前端同一实现）",
            "headroom": bucket,
            "headroom_label": label,
            "verdict": space.verdict,
            "score": score,
            "score_as_of": window_end,
            "available": bool(space.available),
            "pe_ttm": space.pe_ttm,
            "pb": space.pb,
            "pe_percentile": space.pe_percentile,
            "pb_percentile": space.pb_percentile,
            "pe_series_days": space.pe_series_days,
            "pb_series_days": space.pb_series_days,
            "pe_min": space.pe_min,
            "pe_max": space.pe_max,
            "pe_median": space.pe_median,
            "pb_min": space.pb_min,
            "pb_max": space.pb_max,
            "pb_median": space.pb_median,
            # ★ 用户口径三件套：分数含不含同业/行业对标必须是**可判的**
            "peer_source": space.peer_source,
            "peer_label": space.peer_label,
            "peer_count": space.peer_count,
            "peer_pe_median": space.peer_pe_median,
            "peer_pb_median": space.peer_pb_median,
            "industry_pe_median": space.industry_pe_median,
            "industry_name": space.industry_name,
            "industry_company_count": space.industry_company_count,
            "pe_vs_peer_pct": space.pe_vs_peer_pct,
            "pb_vs_peer_pct": space.pb_vs_peer_pct,
            "source_name": space.source_name,
            "gap": space.gap,
            "components_used": (
                ["自身历史分位"] if space.pe_percentile is not None
                or space.pb_percentile is not None else []
            ) + (["同业中位数"] if space.peer_pe_median or space.peer_pb_median else [])
            + (["行业中位数"] if space.industry_pe_median else []),
            "components_missing": (
                [] if space.peer_pe_median or space.pb_percentile is None
                 else ["同业中位数"]
            ),
            "headroom_labels_source": "src.intraday.valuation.HEADROOM_LABELS",
            "basis": (
                f"档位/标签唯一来源 src/intraday/valuation.py：估值空间分 "
                f"{'None' if score is None else round(float(score), 4)} → "
                f"headroom_bucket()={bucket!r} → HEADROOM_LABELS={label!r}；"
                f"样本 {space.pe_series_days} 个 PE 点 / {space.pb_series_days} 个 PB 点"
                f"（采集链口径 = 百度近三年日频，与前端**同一份缓存**）；"
                f"同业对标源={space.peer_source}、peer_pe_median="
                f"{space.peer_pe_median}、行业中位数={space.industry_pe_median}；"
                f"评分时点 score_as_of={window_end}"
                f"（ValuationSpace 不暴露序列末日，故 period_date 取评分时点）；"
                f"分档阈值不在本连接器内定义，本连接器不重新定义任何阈值"
            ),
        }
        # ★ 原始字段（`quant_daily_basic` 的全部可用列）随点下发 —— 两条路径
        #   **共用同一个 `_raw_inputs()`**，不各写一份（见该函数的 docstring）。
        extra["raw_inputs"] = self._raw_inputs_extra(
            self._raw_inputs(code, window_end))
        if score is None:
            extra["insufficient_reason"] = (
                "有 PE/PB 读数但分位无定义（样本 <8）⇒ 档位按平台口径为"
                f"{label!r}，**不是** 0 分、也不是'估值透支'")
        return [self._make_point(
            indicator=f"估值水位:{code}",
            value=None if score is None else round(float(score), 6),
            period_date=window_end,
            extra=extra,
            source_name=f"{space.source_name or '项目采集链'}（↑经 ValuationProvider）",
            source_url="store://app_db#fact_data_points",
            fetch_method=FetchMethod.API_CALL,
            confidence=0.85,
        )]

    def _point_from_series(
        self, code: str, series: dict[str, Any], window_end: str,
        start_date: str | None,
    ) -> list[DataPoint]:
        """退化路径：本地行情仓序列 + **同一组判据函数**（局限性全部写进 extra）。"""
        stats = series["window_stats"]
        score, notes = compute_headroom_score(
            pe_percentile=stats.get("pe_percentile"),
            pb_percentile=stats.get("pb_percentile"),
            pe=stats.get("pe_current"), pb=stats.get("pb_current"),
            peer_pe_median=None, peer_pb_median=None, industry_pe_median=None)
        bucket = headroom_bucket(score)
        label = HEADROOM_LABELS[bucket]
        custom = bool(start_date)
        window_desc = "自定义 start_date（**与平台口径不同**）" if custom else "近三年"
        extra: dict[str, Any] = {
            "valuation_path": "local_warehouse_series",
            "valuation_path_zh": (
                "本地行情仓 quant_daily_basic（**退化路径**：未注入采集链 / 采集链取不到）"),
            "headroom": bucket,
            "headroom_label": label,
            "verdict": ValuationProvider._verdict(  # noqa: SLF001 复用同一份结论构造
                bucket, stats, None, notes) if hasattr(
                    ValuationProvider, "_verdict") else label,
            "score": score,
            "available": True,
            "pe_ttm": stats.get("pe_current"),
            "pb": stats.get("pb_current"),
            "pe_percentile": stats.get("pe_percentile"),
            "pb_percentile": stats.get("pb_percentile"),
            "pe_series_days": stats.get("pe_days", 0),
            "pb_series_days": stats.get("pb_days", 0),
            "pe_min": stats.get("pe_min"), "pe_max": stats.get("pe_max"),
            "pe_median": stats.get("pe_median"),
            "pb_min": stats.get("pb_min"), "pb_max": stats.get("pb_max"),
            "pb_median": stats.get("pb_median"),
            # ★ 三件套**必须存在**（本路径恒为"没有对标"，但不许省略）
            "peer_source": "unavailable",
            "peer_pe_median": None,
            "peer_pb_median": None,
            "industry_pe_median": None,
            "components_used": ["自身历史分位"],
            "components_missing": ["同业中位数", "行业中位数"],
            "sample": series["window_sample"],
            "full_history": series["full_history"],
            "score_scope": "percentile_only",
            "headroom_labels_source": "src.intraday.valuation.HEADROOM_LABELS",
            "basis": (
                f"档位/标签唯一来源 src/intraday/valuation.py（本连接器不定义阈值）："
                f"估值空间分 {None if score is None else round(float(score), 4)} → "
                f"{bucket!r} → {label!r}；"
                f"分位口径={window_desc}，样本 {series['window_sample']['pe_days']} 个 PE 点"
                f"（{series['window_sample']['pe_start']}~{series['window_sample']['pe_end']}）"
                f" / {series['window_sample']['pb_days']} 个 PB 点；"
                f"全历史对照样本 {series['full_history']['pe_days']} 个 PE 点，"
                f"全历史 PE 分位={series['full_history']['pe_percentile']}、"
                f"PB 分位={series['full_history']['pb_percentile']}"
                f"（**近三年分位 ≠ 全历史分位**，两者不许混用）；"
                f"⚠️ 本分只含「自身历史分位」分量，**未含**同业/行业中位数"
                f"（peer_source=unavailable）⇒ 与前端（含对标那一路）在分档边界上"
                f"可能不一致；样本集也不同（行情仓按交易日，百度按自然日）"
            ),
        }
        if custom:
            extra["requested_start_date"] = _iso_date(start_date)
        # ★ 与平台路径**同一份**原始字段构造（见 `_raw_inputs`）。
        extra["raw_inputs"] = self._raw_inputs_extra(
            self._raw_inputs(code, window_end))
        if score is None:
            extra["insufficient_reason"] = (
                f"有 {series['window_sample']['pe_days']} 个 PE 点 / "
                f"{series['window_sample']['pb_days']} 个 PB 点，但分位无定义（<8）"
                f"⇒ 平台五档口径给 {label!r}")
        return [self._make_point(
            indicator=f"估值水位:{code}",
            value=None if score is None else round(float(score), 6),
            period_date=series["last_trade_date"] or window_end,
            extra=extra,
            source_name=f"{series['store']}::quant_daily_basic（本地行情仓）",
            source_url=f"store://{series['store']}#quant_daily_basic",
            fetch_method=FetchMethod.FILE_READ,
            confidence=0.7,
        )]

    def _valuation_series(
        self, code: str, window_end: str, start_date: str | None,
    ) -> dict[str, Any]:
        """行情仓 PE/PB 序列（一次取全历史，再按窗口切片 → 两个口径都能给）。

        实测：600036 4,981 行 / 冷调用 **806 ms**（索引 `(code, trade_date)`），
        6h TTL 内 0 次库调用。
        """
        def _load() -> dict[str, Any]:
            store = _table_store("quant_daily_basic")
            # ★ 列清单**现读 schema**（不写死）：想要 13 列，实际有哪几列由
            #   `_selectable` 决定，缺的随 extra 如实标出（详见 `_VALUATION_RAW_FIELDS`）。
            raw_fields, raw_absent = _selectable(
                "quant_daily_basic", _VALUATION_RAW_FIELDS)
            projection = ", ".join(["trade_date", "pe_ttm", "pb", *raw_fields])
            with _readonly(store) as conn:
                rows = conn.execute(
                    f"SELECT {projection} FROM quant_daily_basic "
                    "WHERE code = ? AND trade_date <= ? ORDER BY trade_date",
                    (code, _compact_date(window_end))).fetchall()
            pe_full: list[_SeriesPoint] = []
            pb_full: list[_SeriesPoint] = []
            dropped_pe = dropped_pb = 0
            for row in rows:
                period = _iso_date(row["trade_date"]) or ""
                pe = row["pe_ttm"]
                pb = row["pb"]
                # ⚠️ 亏损股的 PE 无意义，源用 0/NULL 表示"没有读数" —— 按"不产点"处理，
                #    绝不把 0 当分位样本（那会把分位人为压低）。
                if pe is None or float(pe) <= 0:
                    dropped_pe += 1
                else:
                    pe_full.append(_SeriesPoint(float(pe), period))
                if pb is None or float(pb) <= 0:
                    dropped_pb += 1
                else:
                    pb_full.append(_SeriesPoint(float(pb), period))

            window_start = start_date or _shift_days(window_end, -_VALUATION_WINDOW_DAYS)
            pe_win = [p for p in pe_full if p.period_date >= window_start]
            pb_win = [p for p in pb_full if p.period_date >= window_start]
            window_stats = valuation_from_series(pe_win, pb_win)
            full_stats = valuation_from_series(pe_full, pb_full)
            return {
                "store": store,
                "rows": len(rows),
                "last_trade_date": _iso_date(rows[-1]["trade_date"]) if rows else None,
                "dropped_pe": dropped_pe,
                "dropped_pb": dropped_pb,
                # 最新一行的**原始字段**（随 extra 下发，供人复核估值分；口径见
                # `_VALUATION_RAW_BASIS`）。`raw_absent` = 本环境这张表没有的列。
                "raw_fields": raw_fields,
                "raw_absent": raw_absent,
                "latest_raw": (
                    {column: rows[-1][column] for column in raw_fields}
                    if rows else {}),
                "window_stats": window_stats,
                "pe_days_full": len(pe_full),
                "pb_days_full": len(pb_full),
                "window_sample": {
                    "window": "近三年" if not start_date else "自定义",
                    "window_start": window_start if start_date else _shift_days(
                        window_end, -_VALUATION_WINDOW_DAYS),
                    "window_end": window_end,
                    "pe_days": len(pe_win),
                    "pb_days": len(pb_win),
                    "pe_start": pe_win[0].period_date if pe_win else None,
                    "pe_end": pe_win[-1].period_date if pe_win else None,
                    "pb_start": pb_win[0].period_date if pb_win else None,
                    "pb_end": pb_win[-1].period_date if pb_win else None,
                },
                "full_history": {
                    "pe_days": len(pe_full),
                    "pb_days": len(pb_full),
                    "pe_start": pe_full[0].period_date if pe_full else None,
                    "pe_end": pe_full[-1].period_date if pe_full else None,
                    "pe_percentile": full_stats.get("pe_percentile"),
                    "pb_percentile": full_stats.get("pb_percentile"),
                },
            }

        return dict(_cached(f"valuation:{code}:{window_end}:{start_date or ''}",
                            _VALUATION_TTL, _load))

    # -------------------------------------------------------- 族 B：概念拥挤度

    async def _crowding_points_dummy(self) -> None:  # pragma: no cover - 占位
        return None

    def _crowding_points(self, code: str) -> tuple[list[DataPoint], str | None]:
        """相关度最高的概念板块的拥挤度水位（`value = water_level`）。"""
        ctx = self._concept_context(code)
        rows = ctx["related"]
        measured = [r for r in rows if (r.get("crowding") or {}).get("water_level") is not None]
        if not rows and not ctx["themes"]:
            return [], (
                "ml_member_corr / ml_stock_theme 里都没有该 code 的相关板块"
                "（这两张表分别覆盖 5,215 / 4,996 只，未覆盖即「没量到」）")
        if not measured:
            return [], (
                f"{len(rows)} 个相关板块都没有可用的 water_level（水位未量到）"
                f"，as_of={ctx['crowding_as_of']}")
        top = measured[0]
        crowding = dict(top["crowding"])
        top_name = crowding.get("sector_name")
        peak_ratio = self._peak_ratio(crowding)
        extra: dict[str, Any] = {
            "value_semantics": (
                f"value = 相关度最高（corr={top['corr']}，{top_name}）"
                "且水位已量到的那个概念板块的 water_level（0~1）"),
            "value_board": {
                "board_code": top["board_code"],
                "sector_name": top_name,
                "corr": top["corr"],
                "samples": top.get("samples"),
                "rank_by_corr": 1,
            },
            "as_of": ctx["crowding_as_of"],
            "top_n": _RELATED_TOP_N,
            "related_total": len(rows),
            "related_with_crowding": len(measured),
            "related_without_crowding": [
                r["board_code"] for r in rows
                if (r.get("crowding") or {}).get("water_level") is None
            ],
            "boards": [
                self._board_payload(r) for r in rows[:_RELATED_TOP_N]
            ],
            "sectors_truncated": len(rows) > _RELATED_TOP_N,
            "themes": [self._theme_payload(t) for t in ctx["themes"][:_THEME_TOP_N]],
            "relatedness_caliber": (
                "两条口径都下发：① ml_member_corr 的 corr = **走势相关**（带 samples 与样本区间）；"
                "② ml_stock_theme 的 final_score = **主营相关**（带 business_score 与 reason）"
                "—— 它们回答的是不同问题，不是同一件事的两种写法"
            ),
            "theme_provenance_basis": _THEME_PROVENANCE_BASIS,
            "store": ctx["crowding_store"],
            "relatedness_store": ctx["relatedness_store"],
            "basis": (
                "相关度口径=ml_member_corr（以个股为键，实测覆盖 5,215 只）；"
                "name join 口径=ml_stock_theme.theme → sector_crowding_daily.sector_name"
                "（实测 3/4 命中，未命中的如实标 no_crowding）；"
                "按 corr 降序取相关板块，**先相关、再看拥挤**；"
                "water_level 未量到的板块保留在 boards 里并标 water_level_measured=false"
                "（不填 0）；缺拥挤度的相关板块不丢弃（no_crowding=true 逐条标出）"
            ),
        }
        if peak_ratio is not None:
            extra["value_board_ma5_vs_peak"] = peak_ratio
        return [self._make_point(
            indicator=f"概念拥挤度:{code}",
            value=float(crowding["water_level"]),
            period_date=_iso_date(crowding.get("trade_date")) or ctx["crowding_as_of"],
            extra=extra,
            source_name=(
                f"{ctx['crowding_store']}::sector_crowding_daily × "
                f"{ctx['relatedness_store']}::ml_member_corr"),
            source_url=f"store://{ctx['crowding_store']}#sector_crowding_daily",
            fetch_method=FetchMethod.FILE_READ,
            confidence=0.8,
        )], None

    @staticmethod
    def _peak_ratio(crowding: dict[str, Any]) -> float | None:
        """`ma5_crowding ÷ sector_crowding_max_ma5.max_ma5`（1.0 = 正在历史峰值）。"""
        ma5 = crowding.get("ma5_crowding")
        peak = crowding.get("max_ma5")
        if ma5 is None or not peak:
            return None
        return round(float(ma5) / float(peak), 4)

    def _board_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        crowding = row.get("crowding")
        if crowding is None:
            return {
                "board_code": row["board_code"],
                "corr": row.get("corr"),
                "samples": row.get("samples"),
                "sample_range": row.get("sample_range"),
                "no_crowding": True,
                "no_crowding_reason": (
                    "该 board_code 在 sector_crowding_daily 里没有行"
                    "（拥挤度表本体没覆盖这个板块）"),
            }
        ratio = self._peak_ratio(crowding)
        payload: dict[str, Any] = {
            "board_code": row["board_code"],
            "sector_name": crowding.get("sector_name"),
            "corr": row.get("corr"),
            "samples": row.get("samples"),
            "sample_range": row.get("sample_range"),
            "trade_date": _iso_date(crowding.get("trade_date")),
            "water_level": crowding.get("water_level"),
            "water_level_measured": crowding.get("water_level") is not None,
            "raw_crowding": crowding.get("raw_crowding"),
            "ma5_crowding": crowding.get("ma5_crowding"),
            "max_ma5": crowding.get("max_ma5"),
            "ma5_vs_peak": ratio,
            "gap_to_peak_pct": None if ratio is None else round((1 - ratio) * 100, 2),
            "no_crowding": False,
        }
        return payload

    def _theme_payload(self, theme: dict[str, Any]) -> dict[str, Any]:
        resolved = theme.get("resolved_code")
        crowding = theme.get("crowding")
        payload: dict[str, Any] = {
            "theme": theme.get("theme"),
            "rank": theme.get("rank"),
            "business_score": theme.get("business_score"),
            "corr": theme.get("corr"),
            "final_score": theme.get("final_score"),
            "reason": theme.get("reason"),
            "provenance": theme.get("provenance"),
            "provenance_absent": theme.get("provenance_absent"),
            "resolved_by": theme.get("resolved_by"),
            "candidate_codes": theme.get("candidate_codes"),
        }
        if resolved is None:
            payload.update({
                "no_crowding": True,
                "no_crowding_reason": (
                    "该 theme 名在 sector_crowding_list 与最近交易日截面里都找不到"
                    "同名 sector_name（**如实标出，不丢弃**）"),
            })
        elif crowding is None:
            payload.update({
                "no_crowding": True,
                "no_crowding_reason": (
                    f"已解析到 board_code={resolved}，但该板块在拥挤度表里没有行"),
                "board_code": resolved,
            })
        else:
            ratio = self._peak_ratio(crowding)
            payload.update({
                "no_crowding": False,
                "board_code": resolved,
                "sector_name": crowding.get("sector_name"),
                "trade_date": _iso_date(crowding.get("trade_date")),
                "water_level": crowding.get("water_level"),
                "water_level_measured": crowding.get("water_level") is not None,
                "raw_crowding": crowding.get("raw_crowding"),
                "ma5_crowding": crowding.get("ma5_crowding"),
                "max_ma5": crowding.get("max_ma5"),
                "ma5_vs_peak": ratio,
            })
        return payload

    # ---- 相关度 + 拥挤度上下文（概念拥挤度 与 主线告警 **共用**一次取数） ----

    def _concept_context(self, code: str) -> dict[str, Any]:
        def _load() -> dict[str, Any]:
            related = self._load_related_boards(code)
            themes = self._load_stock_themes(code)
            codes = [r["board_code"] for r in related]
            crowding = self._load_crowding(codes) if codes else {}
            for row in related:
                row["crowding"] = crowding.get(row["board_code"])

            # ---- business(theme) → board_code：两级解析，单一 code 选择规则 ----
            name_index = self._load_name_index()
            related_codes = {r["board_code"] for r in related}
            theme_codes: list[str] = []
            for theme in themes:
                candidates = list(name_index.get(str(theme.get("theme") or "")) or [])
                chosen = next(
                    (c for c in candidates if c in related_codes),
                    candidates[0] if candidates else None)
                theme["resolved_code"] = chosen
                theme["candidate_codes"] = candidates
                theme["resolved_by"] = (
                    None if chosen is None else
                    "related_board_name" if chosen in related_codes else "name_index")
                if chosen:
                    theme_codes.append(chosen)
            extra_codes = [c for c in dict.fromkeys(theme_codes) if c not in crowding]
            if extra_codes:
                crowding.update(self._load_crowding(extra_codes))
            for theme in themes:
                theme["crowding"] = crowding.get(theme.get("resolved_code") or "")

            boards_for_alerts = list(dict.fromkeys(
                [r["board_code"] for r in related] + theme_codes))
            as_of = max(
                (_iso_date(c.get("trade_date")) or "" for c in crowding.values()),
                default=None)
            return {
                "related": related,
                "themes": themes,
                "boards_for_alerts": boards_for_alerts,
                "crowding_as_of": as_of,
                "crowding_store": _table_store("sector_crowding_daily"),
                "relatedness_store": _table_store("ml_member_corr"),
            }

        return dict(_cached(f"concept_ctx:{code}", _CROWDING_TTL, _load))

    def _load_related_boards(self, code: str) -> list[dict[str, Any]]:
        """`ml_member_corr`（**以个股为键**的走势相关度），按 corr 降序。"""
        def _load() -> list[dict[str, Any]]:
            store = _table_store("ml_member_corr")
            with _readonly(store) as conn:
                rows = conn.execute(
                    "SELECT board_code, corr, samples, start_date, end_date "
                    "FROM ml_member_corr WHERE code = ? "
                    "ORDER BY corr DESC, board_code ASC", (code,)).fetchall()
            out = []
            for row in rows:
                out.append({
                    "board_code": str(row["board_code"]),
                    "corr": None if row["corr"] is None else float(row["corr"]),
                    "samples": row["samples"],
                    "sample_range": (
                        f"{_iso_date(row['start_date'])}~{_iso_date(row['end_date'])}"
                        if row["start_date"] or row["end_date"] else None),
                    "crowding": None,
                })
            return out

        return list(_cached(
            f"related_boards:{code}", _CONCEPT_RELATEDNESS_TTL, _load))

    def _load_stock_themes(self, code: str) -> list[dict[str, Any]]:
        """`ml_stock_theme`（**以个股为键**的主营相关度），按生产者的 `rank` 升序。

        逐条带**溯源**（`raw_name/model/prompt_sig/scored_at`）：`business_score`
        与 `final_score` 是**LLM 当时打的**分，不带出处会被读成客观读数。
        列清单同样**现读 schema**（缺列不填 0，见 `_THEME_PROVENANCE_FIELDS`）。
        """
        def _load() -> list[dict[str, Any]]:
            store = _table_store("ml_stock_theme")
            provenance_fields, provenance_absent = _selectable(
                "ml_stock_theme", _THEME_PROVENANCE_FIELDS)
            projection = ", ".join([
                "rank", "theme", "business_score", "corr", "final_score",
                "reason", *provenance_fields])
            with _readonly(store) as conn:
                rows = conn.execute(
                    f"SELECT {projection} "
                    "FROM ml_stock_theme WHERE code = ? ORDER BY rank ASC",
                    (code,)).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                out.append({
                    "rank": row["rank"],
                    "theme": str(row["theme"] or ""),
                    "business_score": row["business_score"],
                    "corr": row["corr"],
                    "final_score": row["final_score"],
                    "reason": row["reason"],
                    "provenance": {column: row[column]
                                   for column in provenance_fields},
                    "provenance_absent": list(provenance_absent),
                    "crowding": None,
                })
            return out

        return list(_cached(f"stock_themes:{code}", _CONCEPT_RELATEDNESS_TTL, _load))

    def _load_crowding(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """每个板块**自己的**最新一行（含 `sector_crowding_max_ma5` 的历史峰值）。

        ⚠️ 用相关子查询取"每板块最新"，**不用**全局最新交易日截面 ——
        实测 2,510 个板块里 1,038 个自身最新日早于全局最新日。
        查询走 `idx_crowding_sector_date (sector_code, trade_date)`，**不扫 218 万行**。
        """
        key_codes = list(dict.fromkeys(codes))

        def _load() -> dict[str, dict[str, Any]]:
            store = _table_store("sector_crowding_daily")
            placeholder = ",".join("?" * len(key_codes))
            sql = (
                "SELECT d.sector_code, d.sector_name, d.trade_date, d.water_level, "
                "d.raw_crowding, d.ma5_crowding, m.max_ma5 "
                "FROM sector_crowding_daily d "
                "LEFT JOIN sector_crowding_max_ma5 m ON m.sector_code = d.sector_code "
                f"WHERE d.sector_code IN ({placeholder}) AND d.trade_date = ("
                "  SELECT MAX(x.trade_date) FROM sector_crowding_daily x "
                "  WHERE x.sector_code = d.sector_code)")
            with _readonly(store) as conn:
                rows = conn.execute(sql, key_codes).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for row in rows:
                out[str(row["sector_code"])] = {
                    "sector_name": row["sector_name"],
                    "trade_date": row["trade_date"],
                    "water_level": row["water_level"],
                    "raw_crowding": row["raw_crowding"],
                    "ma5_crowding": row["ma5_crowding"],
                    "max_ma5": row["max_ma5"],
                }
            return out

        return dict(_cached(
            f"crowding:{','.join(sorted(key_codes))}", _CROWDING_TTL, _load))

    def _load_name_index(self) -> dict[str, list[str]]:
        """`sector_name → [sector_code]`（供 `ml_stock_theme.theme` 的 name join）。

        两个来源合并：`sector_crowding_list`（平台自己的板块池，1,226 行）+
        最近交易日截面（1,472 行 / 实测 2.3 ms，走 `idx_crowding_date`）。
        """
        def _load() -> dict[str, list[str]]:
            index: dict[str, list[str]] = {}

            def _add(name: Any, sector_code: Any) -> None:
                key = str(name or "").strip()
                code = str(sector_code or "").strip()
                if not key or not code:
                    return
                bucket = index.setdefault(key, [])
                if code not in bucket:
                    bucket.append(code)

            list_store = _table_store("sector_crowding_list")
            with _readonly(list_store) as conn:
                for row in conn.execute(
                        "SELECT sector_code, sector_name FROM sector_crowding_list"):
                    _add(row["sector_name"], row["sector_code"])
            xs_store = _table_store("sector_crowding_daily")
            with _readonly(xs_store) as conn:
                for row in conn.execute(
                        "SELECT sector_code, sector_name FROM sector_crowding_daily "
                        "WHERE trade_date = (SELECT MAX(trade_date) "
                        "FROM sector_crowding_daily)"):
                    _add(row["sector_name"], row["sector_code"])
            for bucket in index.values():
                bucket.sort()
            return index

        return dict(_cached("crowding_name_index", _BOARD_POOL_TTL, _load))

    # ---------------------------------------------------------- 族 C：主线告警

    def _mainline_alert_points(
        self, code: str, start_date: str | None, end_date: str | None,
    ) -> tuple[list[DataPoint], str | None]:
        window = _window(start_date, end_date, _ALERT_WINDOW_DAYS)
        detail = self._alert_detail("主线告警", code, window)
        if detail["empty_reason"]:
            return [], str(detail["empty_reason"])
        points: list[DataPoint] = []
        for row in detail["rows"][:_ALERT_MAX_POINTS]:
            points.append(self._make_point(
                indicator=f"主线告警:{code}",
                value=None if row["score"] is None else float(row["score"]),
                period_date=_iso_date(row["trade_date"]) or window[1],
                extra={
                    "board_code": row["board_code"],
                    "board_name": row["board_name"],
                    "level": row["level"],
                    "score": row["score"],
                    "gate_bonus": row["gate_bonus"],
                    "resonance": row["resonance"],
                    "entry_close": row["entry_close"],
                    "max_gain_pct": row["max_gain_pct"],
                    "max_gain_date": _iso_date(row["max_gain_date"]),
                    "kind": row["kind"],
                    "corr": row["corr"],
                    "samples": row["samples"],
                    "is_exact_related_board": row["is_exact_related_board"],
                    "payload": row["payload"],
                    "window": {"start": window[0], "end": window[1],
                               "days": _ALERT_WINDOW_DAYS},
                    "levels_filtered": list(_MAINLINE_ALERT_LEVELS),
                    "concepts_scanned": detail["concepts_scanned"],
                    "rows_in_window": detail["rows_in_window"],
                    "truncated": detail["truncated"],
                    "store": detail["store"],
                    "basis": (
                        "口径=mainline_alert（主线挖掘自己的告警表），只取 "
                        f"level ∈ {list(_MAINLINE_ALERT_LEVELS)}（**weak 档被过滤**）；"
                        f"所属概念=ml_member_corr 的 board_code ∪ ml_stock_theme 的 "
                        f"theme 名 join 结果；**不设 corr 阈值**（那会发明一个没有数据"
                        f"支撑的阈值）—— 每条告警都带该板块与本股的 corr，由消费方权衡；"
                        f"value=主线挖掘的告警分（0~100）"
                    ),
                },
                source_name=f"{detail['store']}::mainline_alert",
                source_url=f"store://{detail['store']}#mainline_alert",
                fetch_method=FetchMethod.FILE_READ,
                confidence=0.8,
            ))
        return points, None

    def _alert_detail(
        self, family: str, code: str, window: tuple[str, str],
    ) -> dict[str, Any]:
        """`主线告警`/`个股告警` 的**唯一**取数实现（fetch 与 diagnose 共用）。"""
        if family == "主线告警":
            ctx = self._concept_context(code)
            boards = ctx["boards_for_alerts"]
            if not boards:
                return {
                    "rows": [], "empty_reason": (
                        "该股在 ml_member_corr / ml_stock_theme 里都没有相关板块 ⇒ "
                        "无法判断「所属概念」——这是「没量到」，不是「没有告警」"),
                    "store": _table_store("mainline_alert"),
                    "concepts_scanned": 0, "rows_in_window": 0, "truncated": False,
                }
            rows_all = self._load_mainline_rows(boards)
            rows = [r for r in rows_all
                    if window[0] <= (_iso_date(r["trade_date"]) or "") <= window[1]]
            if not rows and not rows_all:
                reason = (
                    f"{len(boards)} 个相关板块在 mainline_alert 里**一条告警都没有**"
                    f"（该表只收录被主线挖掘触发的板块；全库 level 分布 "
                    f"medium/strong/weak，已按中高强度过滤）")
            elif not rows:
                newest = max((_iso_date(r["trade_date"]) or "" for r in rows_all), default="")
                reason = (
                    f"窗口 {window[0]}~{window[1]} 内没有中高强度告警；"
                    f"这 {len(boards)} 个相关板块历史上有 {len(rows_all)} 条"
                    f"（最新 {newest}，在窗口之外）⇒ 窗口内确实没有，**不是没量到**")
            else:
                reason = ""
            corr_map = {r["board_code"]: r for r in ctx["related"]}
            packed = [{
                "board_code": r["board_code"], "board_name": r["board_name"],
                "level": r["level"], "score": r["score"],
                "gate_bonus": r["gate_bonus"], "resonance": r["resonance"],
                "entry_close": r["entry_close"], "max_gain_pct": r["max_gain_pct"],
                "max_gain_date": r["max_gain_date"], "kind": r["kind"],
                "trade_date": r["trade_date"], "payload": r["payload"],
                "corr": (corr_map.get(r["board_code"]) or {}).get("corr"),
                "samples": (corr_map.get(r["board_code"]) or {}).get("samples"),
                "is_exact_related_board": r["board_code"] in corr_map,
            } for r in rows]
            return {
                "rows": packed,
                "empty_reason": reason,
                "store": _table_store("mainline_alert"),
                "concepts_scanned": len(boards),
                "rows_in_window": len(packed),
                "truncated": len(packed) > _ALERT_MAX_POINTS,
            }

        rows_all = self._load_stock_alerts(code)
        rows = [r for r in rows_all
                if window[0] <= (r["date"] or "") <= window[1]]
        if not rows and not rows_all:
            reason = (
                "fact_alerts 里没有任何一条 affected_stocks_json 点名该 code 的告警"
                "（该表是全市场事件告警，未被点名即「没量到」）")
        elif not rows:
            newest = max((r["date"] or "" for r in rows_all), default="")
            reason = (
                f"窗口 {window[0]}~{window[1]} 内没有点名该 code 的告警；"
                f"历史上有 {len(rows_all)} 条（最新 {newest}，在窗口之外）")
        else:
            reason = ""
        packed = sorted(rows, key=lambda r: (r["date"] or ""), reverse=True)
        return {
            "rows": packed, "empty_reason": reason,
            "store": _table_store("fact_alerts"),
            "concepts_scanned": 0, "rows_in_window": len(packed),
            "truncated": len(packed) > _ALERT_MAX_POINTS,
        }

    def _load_mainline_rows(self, boards: list[str]) -> list[dict[str, Any]]:
        """这些概念的**全部**中高强度告警（窗口过滤在内存里做 → 换窗口不重查）。"""
        key_boards = list(dict.fromkeys(boards))
        placeholder = ",".join("?" * len(key_boards))
        levels = ",".join("?" * len(_MAINLINE_ALERT_LEVELS))

        def _load() -> list[dict[str, Any]]:
            store = _table_store("mainline_alert")
            sql = (
                "SELECT trade_date, board_code, board_name, kind, level, score, "
                "gate_bonus, resonance, entry_close, max_gain_pct, max_gain_date, payload "
                f"FROM mainline_alert WHERE board_code IN ({placeholder}) "
                f"AND level IN ({levels}) ORDER BY trade_date DESC, score DESC")
            with _readonly(store) as conn:
                rows = conn.execute(
                    sql, [*key_boards, *_MAINLINE_ALERT_LEVELS]).fetchall()
            return [{
                "trade_date": row["trade_date"],
                "board_code": str(row["board_code"]),
                "board_name": row["board_name"],
                "kind": row["kind"],
                "level": row["level"],
                "score": row["score"],
                "gate_bonus": row["gate_bonus"],
                "resonance": row["resonance"],
                "entry_close": row["entry_close"],
                "max_gain_pct": row["max_gain_pct"],
                "max_gain_date": row["max_gain_date"],
                "payload": _loads(row["payload"]),
            } for row in rows]

        return list(_cached(
            f"mainline:{','.join(sorted(key_boards))}", _ALERTS_TTL, _load))

    # ---------------------------------------------------------- 族 D：个股告警

    def _stock_alert_points(
        self, code: str, start_date: str | None, end_date: str | None,
    ) -> tuple[list[DataPoint], str | None]:
        window = _window(start_date, end_date, _ALERT_WINDOW_DAYS)
        detail = self._alert_detail("个股告警", code, window)
        if detail["empty_reason"]:
            return [], str(detail["empty_reason"])
        points: list[DataPoint] = []
        for row in detail["rows"][:_ALERT_MAX_POINTS]:
            value, value_field = self._alert_value(row)
            points.append(self._make_point(
                indicator=f"个股告警:{code}",
                value=value,
                period_date=row["date"] or window[1],
                extra={
                    "alert_id": row["alert_id"],
                    "alert_type": row["alert_type"],
                    "alert_level": row["alert_level"],
                    "title": row["title"],
                    "description": row["description"],
                    "impact": row["impact"],
                    "reason": row["reason"],
                    "risk_score": row["risk_score"],
                    "opportunity_score": row["opportunity_score"],
                    "confidence": row["confidence"],
                    "value_field": value_field,
                    "status": row["status"],
                    "expired": row["expired"],
                    "trigger_time": row["trigger_time"],
                    "expire_time": row["expire_time"],
                    "event_publish_time": row["event_publish_time"],
                    "impact_path": row["impact_path"],
                    "affected_industries": row["affected_industries"],
                    "source_name": row["source_name"],
                    "source_url": row["source_url"],
                    "tenant_id": row["tenant_id"],
                    "matches_in_alert": row["matches"],
                    "window": {"start": window[0], "end": window[1],
                               "days": _ALERT_WINDOW_DAYS},
                    "rows_in_window": detail["rows_in_window"],
                    "truncated": detail["truncated"],
                    "store": detail["store"],
                    "basis": (
                        "口径=fact_alerts.affected_stocks_json **点名该 code** 的告警"
                        "（LIKE 预筛 + 内存精确校验，防子串假命中）；"
                        f"value={value_field}（原样直取，不做任何合成）；"
                        "**不按 status 过滤**（main 实例实测 7 条全是 expired，"
                        "过滤掉会让「两周前的降准利好」消失）；"
                        "多租户未过滤（tenant_id 原样带出，本连接器是数据层生产者）"
                    ),
                },
                source_name=row["source_name"] or f"{detail['store']}::fact_alerts",
                source_url=row["source_url"] or f"store://{detail['store']}#fact_alerts",
                fetch_method=FetchMethod.FILE_READ,
                confidence=0.75,
            ))
        return points, None

    @staticmethod
    def _alert_value(row: dict[str, Any]) -> tuple[float | None, str]:
        """方向性分数：positive → `opportunity_score`，否则 `risk_score`（**原样直取**）。"""
        impact = str(row.get("impact") or "").strip().lower()
        if not impact:
            impact = "positive" if str(row.get("alert_type")) == "opportunity" else "risk"
        if impact == "positive":
            score = row.get("opportunity_score")
            return (None if score is None else float(score), "opportunity_score")
        if impact == "negative":
            score = row.get("risk_score")
            return (None if score is None else float(score), "risk_score")
        # 中性：两个分数都在 extra 里，value 取置信度更高的那一个方向
        score = row.get("risk_score")
        return (None if score is None else float(score), "risk_score(中性impact)")

    def _load_stock_alerts(self, code: str) -> list[dict[str, Any]]:
        """**点名该 code** 的告警（LIKE 预筛 + 内存精确校验）。"""
        target = _digits_code(code)

        def _load() -> list[dict[str, Any]]:
            store = _table_store("fact_alerts")
            with _readonly(store) as conn:
                rows = conn.execute(
                    "SELECT alert_id, alert_type, alert_level, title, description, "
                    "risk_score, opportunity_score, confidence, affected_stocks_json, "
                    "affected_industries_json, impact_path, source_name, source_url, "
                    "event_publish_time, trigger_time, expire_time, status, tenant_id, "
                    "created_at FROM fact_alerts "
                    "WHERE affected_stocks_json LIKE ?", (f"%{target}%",)).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                stocks = _loads(row["affected_stocks_json"])
                if not isinstance(stocks, list):
                    continue
                matches = [s for s in stocks if isinstance(s, dict)
                           and _digits_code(s.get("code")) == target]
                if not matches:
                    continue   # LIKE 的假命中（子串）在这里被挡掉
                entry = matches[0]
                trigger = str(row["trigger_time"] or "")
                expire = str(row["expire_time"] or "")
                created = str(row["created_at"] or "")
                date_iso = _iso_date(trigger[:10] or created[:10])
                out.append({
                    "alert_id": row["alert_id"],
                    "alert_type": row["alert_type"],
                    "alert_level": row["alert_level"],
                    "title": row["title"],
                    "description": row["description"],
                    "risk_score": row["risk_score"],
                    "opportunity_score": row["opportunity_score"],
                    "confidence": row["confidence"],
                    "impact": entry.get("impact"),
                    "reason": entry.get("reason"),
                    "matches": [{
                        "code": _digits_code(s.get("code")),
                        "name": s.get("name"),
                        "impact": s.get("impact"),
                        "reason": s.get("reason"),
                    } for s in matches],
                    "affected_industries": _loads(row["affected_industries_json"]),
                    "impact_path": row["impact_path"],
                    "source_name": row["source_name"],
                    "source_url": row["source_url"],
                    "event_publish_time": row["event_publish_time"],
                    "trigger_time": trigger,
                    "expire_time": expire,
                    "status": row["status"],
                    "expired": str(row["status"] or "").lower() == "expired" or (
                        bool(expire) and expire[:10] < _today()),
                    "tenant_id": row["tenant_id"],
                    "date": date_iso,
                })
            out.sort(key=lambda r: (r["date"] or ""), reverse=True)
            return out

        return list(_cached(f"stock_alerts:{code}", _ALERTS_TTL, _load))

    # ---------------------------------------------------------- 族 E：解禁计划

    def _unlock_points(
        self, code: str, start_date: str | None, end_date: str | None,
    ) -> tuple[list[DataPoint], str | None]:
        """窗口内该股的解禁计划（**四态分开**，见 `_unlock_detail`）。"""
        window = _window(start_date, end_date, _UNLOCK_WINDOW_DAYS, forward=True)
        detail = self._unlock_detail(code, window)
        state = detail["state"]
        if state in ("unmeasured", "out_of_covered_window"):
            return [], str(detail["reason"])
        common = detail["common"]
        #: 表口径（逐股一行，完整）与 JSON 回退口径（每日只留 top10，**可能截断**）
        #: 的"0"不是同一个可信度 ⇒ 用**不同的态名**，并在 `basis` 里说清差别。
        zero_state = (
            "measured_zero" if state == "measured_zero"
            else "measured_zero_in_truncated_detail")
        if state in ("measured_zero", "measured_zero_in_truncated_detail"):
            from_table = state == "measured_zero"
            return [self._make_point(
                indicator=f"解禁计划:{code}",
                value=0.0,
                period_date=window[1],
                extra={
                    **common,
                    "state": zero_state,
                    "value_semantics": (
                        "0.0 = **量到 0**：窗口内确有解禁计划数据，但表里没有该标的"
                        " ⇒ 真结论「该窗口无解禁计划」" if from_table else
                        "0.0 = 日历明细里没有该标的；**但该明细每日只留 top N，"
                        "存在被截断的可能** ⇒ 只能当「未见于明细」"),
                    "period_date_semantics": (
                        "窗口末日（表示这一结论覆盖的区间右端，**不是解禁日**）"),
                    "covered_window": common.get("covered_window"),
                    "unlock_dates_in_window": detail["dates_in_window"],
                    "detail_entries_scanned": detail.get("entries_scanned"),
                    "basis": (
                        f"窗口 {window[0]}~{window[1]} 落在已落库覆盖范围 "
                        f"{common.get('covered_window', {}).get('start')}~"
                        f"{common.get('covered_window', {}).get('end')} 内，"
                        f"表内共 {common.get('table_rows_total')} 行、"
                        f"**未出现 {code}** ⇒ 真结论：未来该窗口无解禁计划。"
                        f"这是「量到 0」（源查过了），**不是**「没量到」"
                        if from_table else
                        f"窗口 {window[0]}~{window[1]} 内日历共 "
                        f"{len(detail['dates_in_window'])} 个解禁日 / "
                        f"{detail.get('entries_scanned')} 条个股明细，未出现 {code}；"
                        f"⚠️ 该明细**每天只保留市值最大的 "
                        f"{common.get('detail_truncated_per_day')} 只**，"
                        "所以这是「未见于明细」而非「一定没有解禁」；"
                        "落 `unlock_plan` 表后即可给出完整结论"
                    ),
                },
                source_name=common["source_name"],
                source_url=common["source_url"],
                fetch_method=FetchMethod.FILE_READ,
                confidence=0.8 if from_table else 0.6,
            )], None

        points: list[DataPoint] = []
        for hit in detail["hits"]:
            total = sum(float(e.get("market_cap") or 0.0) for e in hit["entries"])
            from_table = common.get("unlock_source") == "unlock_plan"
            points.append(self._make_point(
                indicator=f"解禁计划:{code}",
                value=round(total, 2),
                period_date=hit["date"],
                extra={
                    **common,
                    "state": "measured_hit",
                    "value_semantics": "解禁市值合计（元），按该解禁日的明细求和",
                    "unlock_date": hit["date"],
                    "entries": hit["entries"],
                    "entry_count": len(hit["entries"]),
                    "share_types": hit["share_types"],
                    "shares_total": hit.get("shares_total"),
                    "pct_of_float_max": hit["pct_of_float_max"],
                    "dates_in_window": detail["dates_in_window"],
                    "basis": (
                        f"窗口 {window[0]}~{window[1]} 内该股有解禁："
                        f"{hit['date']} 共 {len(hit['entries'])} 条明细，"
                        f"解禁市值合计 {round(total, 2)} 元（解禁数量合计 "
                        f"{hit.get('shares_total')} 股）；"
                        + ("明细来自逐股表 app_db::unlock_plan"
                           "（code/name/unlock_date/shares/market_cap/"
                           "pct_of_float/share_type，索引点查）"
                           if from_table else
                           "明细来自 fact_data_points.extra_json.top_stocks"
                           "（code/name/market_cap/pct_of_float/share_type），"
                           "⚠️ 该路径每日只留 top N、可能截断")
                    ),
                },
                source_name=common["source_name"],
                source_url=common["source_url"],
                fetch_method=FetchMethod.FILE_READ,
                confidence=0.85 if from_table else 0.7,
            ))
        return points, None

    def _unlock_detail(self, code: str, window: tuple[str, str]) -> dict[str, Any]:
        """`解禁计划` 的**唯一**取数实现（fetch 与 diagnose 共用）。

        ## 取数面（2026-09-29 用户口径改造）

        用户原话：「按投研分析要查的股票、解禁日期、**解禁数量**落入到数据库
        **单独的表中**，每月自动调度一次……agent 数据连接**对接这个单独的表**，
        查询未来一个月这个股的解禁数据。**直接精准哈希就找到了**。」

        所以现在的顺序是：

        1. **首选**：`app_db::unlock_plan`（`repositories/unlock_plan_repo.py`）——
           逐股一行 + `(code, unlock_date)` 索引 ⇒ 一次索引点查；
        2. **回退**（仅当该表**一行都没有**，即还没落过库）：老的
           `fact_data_points.extra_json.top_stocks` 路径，并在 extra 里
           **明写 `unlock_source="cal_json_fallback"`** —— 因为那条路的每日明细
           **只留市值最大的 10 只**（`calendar_store` 的 `[:10]`），
           它的"没有该标的"**不能**当成"无解禁"（假阴性）。

        ## 四态（「没量到」与「量到 0」分开，且"超出窗口"单独一态）

        | 态 | 判据 | 行为 |
        |---|---|---|
        | `measured_hit` | 窗口内有该股的行 | 每天一个点，value=该日解禁市值合计 |
        | `measured_zero` | 窗口**在覆盖范围内**且无该股的行 | 产 `value=0.0`（真结论：无解禁） |
        | `out_of_covered_window` | 查询窗口**超出**表的覆盖范围 | **不产点** + 明说超到哪 |
        | `unmeasured_fallback` | 表里一行都没有（未落库） | 回退 JSON 路径，**不许**当"无解禁" |
        """
        table = self._unlock_table_detail(code, window)
        if table is not None:
            return table
        return self._unlock_detail_from_calendar(code, window)

    def _unlock_table_detail(
        self, code: str, window: tuple[str, str],
    ) -> dict[str, Any] | None:
        """首选路径：`unlock_plan` 表（返回 None = 表还没落库，交给回退路径）。"""
        try:
            from src.infrastructure.repositories import unlock_plan_repo

            win = unlock_plan_repo.window()
        except Exception as exc:  # noqa: BLE001 表/仓储不可用 → 交给回退路径
            logger.warning("平台自有数据：unlock_plan 表不可用（回退日历 JSON）：%s", exc)
            return None
        if not win["rows"]:
            return None                     # 还没落库 → 回退（由回退路径标注来源）

        rows = unlock_plan_repo.query(code, window[0], window[1])
        covered = win["start"] <= window[0] and window[1] <= win["end"]
        common = {
            "unlock_source": "unlock_plan",
            "unlock_source_zh": "逐股明细表 app_db::unlock_plan（索引点查）",
            "store": win["store"],
            "table": unlock_plan_repo.TABLE,
            "window": {"start": window[0], "end": window[1],
                       "days": _UNLOCK_WINDOW_DAYS},
            "covered_window": {"start": win["start"], "end": win["end"]},
            "table_rows_total": win["rows"],
            "table_fetched_at": win["fetched_at"],
            "source_name": f"{win['store']}::{unlock_plan_repo.TABLE}（东财个股解禁明细）",
            "source_url": "store://app_db#unlock_plan",
            "basis": (
                "口径=app_db::unlock_plan（逐股一行，主键 (code, unlock_date, "
                "share_type)，索引 idx_unlock_plan_code_date）＝**与投资日历同源**"
                "（东财 stock_restricted_release_detail_em 个股明细），"
                "由月频作业 unlock_plan_monthly 落库；value=该解禁日解禁市值合计（元）"
            ),
        }
        if not covered:
            return {
                "state": "out_of_covered_window",
                "reason": (
                    f"查询窗口 {window[0]}~{window[1]} **超出**已落库覆盖范围 "
                    f"{win['start']}~{win['end']}（表内 {win['rows']} 行，"
                    f"最近落库 {win['fetched_at']}）⇒ **没量到**，"
                    "不是「无解禁」；跑 `uv run python scripts/unlock_plan.py --ingest` "
                    "或等月频作业扩窗"),
                "common": common, "dates_in_window": [], "hits": [],
            }
        if not rows:
            return {
                "state": "measured_zero", "reason": "", "common": common,
                "hits": [], "dates_in_window": [],
                "entries_scanned": win["rows"],
            }
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_date.setdefault(str(row["unlock_date"]), []).append(row)
        hits = [{
            "date": day,
            "entries": entries,
            "share_types": sorted({str(e.get("share_type")) for e in entries
                                   if e.get("share_type")}),
            "shares_total": sum(float(e.get("shares") or 0.0) for e in entries),
            "pct_of_float_max": max(
                (float(e["pct_of_float"]) for e in entries
                 if e.get("pct_of_float") is not None), default=None),
        } for day, entries in sorted(by_date.items())]
        return {
            "state": "measured_hit", "reason": "", "common": common,
            "hits": hits, "dates_in_window": sorted(by_date),
            "entries_scanned": win["rows"],
        }

    def _unlock_detail_from_calendar(
        self, code: str, window: tuple[str, str],
    ) -> dict[str, Any]:
        """回退路径：老的 `cal:unlock:*` JSON 明细（**每日只留 top10**，故不许断言"无解禁"）。"""
        cal = self._load_unlock_calendar()
        in_window = {d: m for d, m in cal["dates"].items() if window[0] <= d <= window[1]}
        common = {
            "unlock_source": "cal_json_fallback",
            "unlock_source_zh": (
                "**回退路径**：投资日历 cal:unlock:* 的 extra_json.top_stocks"
                "（逐股明细表 app_db::unlock_plan 还没有落库）"),
            "indicator_source": cal["indicator"],
            "store": cal["store"],
            "window": {"start": window[0], "end": window[1],
                       "days": _UNLOCK_WINDOW_DAYS},
            "calendar_rows_total": cal["rows"],
            "calendar_dates_total": len(cal["dates"]),
            "calendar_latest_date": max(cal["dates"]) if cal["dates"] else None,
            "source_name": cal["source_name"],
            "source_url": cal["source_url"],
            # ★ 回退路径的**关键局限**（不许省略）：每日明细只留市值最大的 10 只
            "detail_truncated_per_day": _UNLOCK_JSON_TOP_N,
            "fallback_note": (
                "逐股明细表未落库（跑 `uv run python scripts/unlock_plan.py --ingest` "
                "或等月频作业 unlock_plan_monthly）⇒ 本次用日历 JSON 明细，"
                f"而它**每天只留市值最大的 {_UNLOCK_JSON_TOP_N} 只**："
                "所以「明细里没有该标的」**不能**当成「无解禁」（可能是被截断）"),
            "basis": cal["basis"],
        }
        if not in_window:
            return {
                "state": "unmeasured",
                "reason": (
                    f"窗口 {window[0]}~{window[1]} 内库里**没有** cal:unlock:* 日历行"
                    f"（该表共 {len(cal['dates'])} 个解禁日，最新 "
                    f"{max(cal['dates']) if cal['dates'] else None}）⇒ **没量到**，"
                    "不是「无解禁」；这是数据缺口，不是结论"),
                "common": common,
                "dates_in_window": [],
                "entries_scanned": 0,
            }
        hits: list[dict[str, Any]] = []
        entries_scanned = 0
        for unlock_date in sorted(in_window):
            for entries in in_window[unlock_date].values():
                entries_scanned += len(entries)
            own = in_window[unlock_date].get(_digits_code(code)) or []
            if not own:
                continue
            hits.append({
                "date": unlock_date,
                "entries": own,
                "share_types": sorted({str(e.get("share_type")) for e in own
                                       if e.get("share_type")}),
                "pct_of_float_max": max(
                    (float(e["pct_of_float"]) for e in own
                     if e.get("pct_of_float") is not None), default=None),
            })
        # 同一 payload 会在三个 cal:unlock:* 指标里各存一份（实测逐字节相同）→
        # `_load_unlock_calendar` 已按 (code, share_type, market_cap) 去重，此处只判有无。
        if not hits:
            # ⚠️ 回退路径的"没有"只能报**有限结论**：明细按日截断过，
            #    所以这里用 `measured_zero_in_truncated_detail` 这个**不同的态名**，
            #    而不是与"表口径量到 0"共用一个词 —— 两者的可信度不一样。
            return {
                "state": "measured_zero_in_truncated_detail",
                "reason": "",
                "common": common,
                "dates_in_window": sorted(in_window),
                "entries_scanned": entries_scanned,
                "hits": [],
            }
        return {
            "state": "measured_hit", "reason": "", "common": common,
            "dates_in_window": sorted(in_window),
            "entries_scanned": entries_scanned, "hits": hits,
        }

    def _load_unlock_calendar(self) -> dict[str, Any]:
        """`cal:unlock:*` 的个股明细索引（**全进程共享**，6h TTL）。

        ⚠️ 用前缀**范围扫描**而不是 `LIKE 'cal:unlock:%'`：实测 `LIKE` 的
        `EXPLAIN QUERY PLAN` 是 `SCAN ... USING COVERING INDEX`（200 万行 / 107 ms），
        范围扫描是 `SEARCH ... (indicator>? AND indicator<?)`（0 ms）。
        """
        def _load() -> dict[str, Any]:
            store = _table_store("fact_data_points")
            with _readonly(store) as conn:
                rows = conn.execute(
                    "SELECT indicator, period_date, extra_json, source_name, source_url "
                    "FROM fact_data_points "
                    "WHERE indicator >= ? AND indicator < ? "
                    "ORDER BY period_date",
                    ("cal:unlock:", "cal:unlock;")).fetchall()
            dates: dict[str, dict[str, list[dict[str, Any]]]] = {}
            seen: set[tuple[str, str, str, Any]] = set()
            source_name = "投资日历"
            source_url = ""
            metrics: list[str] = []
            for row in rows:
                metrics.append(str(row["indicator"]))
                if row["source_name"]:
                    source_name = str(row["source_name"])
                if row["source_url"] and not source_url:
                    source_url = str(row["source_url"])
                payload = _loads(row["extra_json"])
                if not isinstance(payload, dict):
                    continue
                date_iso = _iso_date(row["period_date"])
                if not date_iso:
                    continue
                bucket = dates.setdefault(date_iso, {})
                for entry in (payload.get("top_stocks") or []):
                    if not isinstance(entry, dict):
                        continue
                    stock = _digits_code(entry.get("code"))
                    if not stock:
                        continue
                    key = (date_iso, stock, str(entry.get("share_type") or ""),
                           entry.get("market_cap"))
                    if key in seen:      # 三个指标同一天是同一份 payload → 去重
                        continue
                    seen.add(key)
                    bucket.setdefault(stock, []).append({
                        "code": stock,
                        "name": entry.get("name"),
                        "market_cap": entry.get("market_cap"),
                        "pct_of_float": entry.get("pct_of_float"),
                        "share_type": entry.get("share_type"),
                    })
            return {
                "store": store,
                "rows": len(rows),
                "dates": dates,
                "indicator": sorted(set(metrics)),
                "source_name": source_name,
                "source_url": source_url,
                "basis": (
                    "口径=fact_data_points 里 cal:unlock:* 的 extra_json.top_stocks"
                    "（个股级明细装在 **JSON 列**里，不是独立表/列）；"
                    "同一天三条聚合指标携带的是**同一份** payload（实测逐字节相同），"
                    "已按 (code, share_type, market_cap) 去重；"
                    "latest 日期是该表自己的 period_date（解禁日）"
                ),
            }

        return dict(_cached("unlock_calendar", _UNLOCK_TTL, _load))

    # ------------------------------------------------------ 族 F：行业拥挤度

    def _industry_crowding_points(
        self, industry_name: str,
    ) -> tuple[list[DataPoint], str | None]:
        """行业名 → **最相近的所属概念板块** → 该板块 `water_level`。

        用户原话：「用户输入内容中含有**行业**的，要连接到概念板块拥挤度的数据表，
        找与之**最相近的所属概念板块**，**如果找不到就不提示未找到数据**。」

        ⚠️ 候选池 = `_load_name_index()`（`sector_crowding_list` ∪ 最近交易日截面）
        —— 与 `概念拥挤度` 的 name join **同一份**，所以两个族不会各有一套"最相近"。
        """
        name_index = self._load_name_index()
        matched, tier = _match_board_name(industry_name, list(name_index))
        if matched is None:
            return [], (
                f"行业「{industry_name}」在拥挤度板块池（{len(name_index)} 个板块名）里"
                f"没有相近匹配 {_NO_MATCH_REASON}")
        codes = [c for c in (name_index.get(matched) or [])]
        rows = self._load_crowding(codes) if codes else {}
        hit = next((rows[c] for c in codes
                    if c in rows and rows[c].get("water_level") is not None), None)
        if hit is None:
            return [], (
                f"已匹配到板块「{matched}」（{tier}），但该板块在拥挤度表里"
                f"没有可用 water_level {_NO_MATCH_REASON}")
        peak = self._peak_ratio(hit)
        sector_code = next(c for c in codes if rows.get(c) is hit)
        return [self._make_point(
            indicator=f"行业拥挤度:{industry_name}",
            value=float(hit["water_level"]),
            period_date=_iso_date(hit.get("trade_date")),
            extra={
                "industry_name": industry_name,
                "matched_board": matched,
                "match_tier": tier,
                "sector_code": sector_code,
                "sector_name": hit.get("sector_name"),
                "trade_date": _iso_date(hit.get("trade_date")),
                "water_level": hit.get("water_level"),
                "raw_crowding": hit.get("raw_crowding"),
                "ma5_crowding": hit.get("ma5_crowding"),
                "max_ma5": hit.get("max_ma5"),
                "ma5_vs_peak": peak,
                "gap_to_peak_pct": None if peak is None else round((1 - peak) * 100, 2),
                "candidate_pool": "sector_crowding_list ∪ sector_crowding_daily 最近交易日截面",
                "candidate_count": len(name_index),
                "candidate_codes_for_matched": codes,
                "value_semantics": (
                    f"value = 与行业「{industry_name}」最相近的板块「{matched}」的 "
                    "water_level（0~1）"),
                "not_found_policy": (
                    "★ 命中时的策略说明：**匹配得上就产点**。反过来的情形（本地"
                    "匹配不上）本连接器不产点，**由采集链的联网兜底继续去找** —— "
                    "口径 2026-09-29 变更（原先是「不提示」，现行为「找不到就去联网搜索找」）"),
                "store": _table_store("sector_crowding_daily"),
                "basis": (
                    "匹配口径：候选池 = 拥挤度板块池（板块池表 ∪ 最近交易日截面，"
                    "与 `概念拥挤度` 的 name join 同源）；匹配档位 exact（归一化相等）"
                    "→ contains（互为子串，取命中长度最长、板块名最短者）；"
                    "归一化只做大小写与空白，**不去括号/不去后缀**"
                    "（`银行(A股)` 与 `银行` 是两个板块，不许合并）；"
                    "水位未量到的板块不填 0，直接不产点"
                ),
            },
            source_name=f"{_table_store('sector_crowding_daily')}::sector_crowding_daily",
            source_url=f"store://{_table_store('sector_crowding_daily')}#sector_crowding_daily",
            fetch_method=FetchMethod.FILE_READ,
            confidence=0.75,
        )], None

    # ------------------------------------------------------ 族 G：板块资金流

    async def _sector_flow_points(
        self, name: str,
    ) -> tuple[list[DataPoint], str | None]:
        """板块**近期资金流方向**（复用 `FundFlowProvider`，不绕过它自己取数）。

        用户原话：「也可以接入"**板块资金流**"功能板块的数据，查询该板块
        **近期资金流方向**。」

        ## 口径分层（**实测驱动**，不是设计偏好）

        | 口径 | 入口 | 实测 | 用途 |
        |---|---|---|---|
        | 板块截面（最新日） | `sector_snapshot()` | **0.7s / 1030 板块 / 可用** | `value` 基线 |
        | 近 N 日序列 | `sector_history(name, 5)` | 东财 `RemoteDisconnected` 时
          **35.6s 且不可用**（失败结果被 provider 缓存 1800s） | 有则升级为"N 日合计" |

        所以：**截面优先**；历史序列**最多等 `_FUNDFLOW_HISTORY_TIMEOUT` 秒**
        （超时/不可用则用截面当日口径，并在 extra 写明 `window_days=1` 与原因），
        且判定不可用后进入 `_FUNDFLOW_HISTORY_COOLDOWN` 冷却，不再白等。
        """
        provider = _fundflow_provider()
        try:
            snapshot = await provider.sector_snapshot()
        except Exception as exc:  # noqa: BLE001 上游异常必须显式化
            raise DataFetchError(
                f"板块资金流截面取数失败（{type(exc).__name__}: {exc}）") from exc
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        matched, tier = _match_board_name(name, list(snapshot))
        info = dict(snapshot.get(matched) or {}) if matched else {}

        series: list[Any] = []
        history_gap: str | None = None
        entity_source = ""
        history_state = _FUNDFLOW_HISTORY_STATE.get("last")
        cooling = bool(history_state) and (
            time.time() - float(history_state.get("at") or 0) < _FUNDFLOW_HISTORY_COOLDOWN)
        if not cooling:
            try:
                entity = await asyncio.wait_for(
                    provider.sector_history(matched or name,
                                            window_days=_FUNDFLOW_WINDOW_DAYS),
                    timeout=_FUNDFLOW_HISTORY_TIMEOUT)
                series = list(getattr(entity, "series", None) or [])
                entity_source = str(getattr(entity, "data_source", "") or "")
                if not series:
                    history_gap = str(getattr(entity, "gap", None) or "序列为空")
            except TimeoutError:
                history_gap = (
                    f"近 {_FUNDFLOW_WINDOW_DAYS} 日序列等待超过 "
                    f"{_FUNDFLOW_HISTORY_TIMEOUT:g}s（实测东财口径冷调用 35.6s），"
                    f"已改用截面当日口径")
            except Exception as exc:  # noqa: BLE001
                history_gap = f"序列取数异常：{type(exc).__name__}: {exc}"
            if history_gap:
                _FUNDFLOW_HISTORY_STATE["last"] = {"at": time.time(),
                                                   "reason": history_gap}
        else:
            history_gap = (
                f"序列处于冷却中（{_FUNDFLOW_HISTORY_COOLDOWN // 60} 分钟内不重试）："
                f"{history_state.get('reason')}")

        nets = [float(p.net) for p in series if getattr(p, "net", None) is not None]
        # 窗口天数 = 这个 `value` **覆盖了几天**：有序列就是序列长度，
        # 降级到截面就是 1 天（**绝不许把"当日"写成"近 N 日"**）。
        window_days = len(nets) if nets else 1
        if nets:
            total = round(sum(nets), 2)
            window_basis = "history_series"
            latest_net = str(getattr(series[-1], "date", "") or "")
            data_source = entity_source
        else:
            total = _finite_number(info.get("net"))
            window_basis = "snapshot_latest_day"
            latest_net = _iso_date(info.get("trade_date")) or ""
            data_source = "Tushare moneyflow_ind_dc（板块截面）"
        if total is None:
            return [], (
                f"板块「{name}」既没有近 {_FUNDFLOW_WINDOW_DAYS} 日序列、"
                f"也不在板块截面里（截面共 {len(snapshot)} 个板块）"
                f"；序列原因：{history_gap or '未知'}{_NO_MATCH_REASON}")
        direction = "inflow" if total > 0 else "outflow" if total < 0 else "flat"
        direction_zh = {"inflow": "净流入", "outflow": "净流出", "flat": "基本持平"}[direction]
        return [self._make_point(
            indicator=f"板块资金流:{name}",
            value=total,
            period_date=latest_net if window_basis == "snapshot_latest_day"
            else _iso_date(latest_net),
            extra={
                "board_name": name,
                "matched_board": matched,
                "match_tier": tier,
                "in_snapshot": bool(info),
                "snapshot": info or None,
                "snapshot_code": info.get("code"),
                "snapshot_rank": info.get("rank"),
                "snapshot_net": _finite_number(info.get("net")),
                "snapshot_net_rate": info.get("net_rate"),
                "snapshot_pct_change": info.get("pct_change"),
                "snapshot_trade_date": _iso_date(info.get("trade_date")),
                "snapshot_net_realtime": info.get("net_realtime"),
                "snapshot_content_type": info.get("content_type"),
                "direction": direction,
                "direction_zh": direction_zh,
                "direction_basis": (
                    "按窗口内净额**合计的符号**判（>0 净流入 / <0 净流出 / ==0 基本持平）；"
                    "**不设阈值** —— 无数据支撑的阈值就是假精度"
                ),
                "window_days": window_days,
                "window_requested_days": _FUNDFLOW_WINDOW_DAYS,
                "window_basis": window_basis,
                "window_basis_zh": (
                    f"近 {window_days} 个交易日序列合计" if nets else
                    "板块截面（最新交易日当日净额；近 N 日序列本次不可用，原因见 history_gap）"),
                "net_total": total,
                "series": [p.to_dict() for p in series],
                "history_gap": history_gap,
                "history_timeout_s": _FUNDFLOW_HISTORY_TIMEOUT,
                "unit": "元",
                "data_source": data_source,
                "value_semantics": (
                    f"value = {f'近 {window_days} 个交易日' if nets else '最新交易日'}"
                    f"板块主力净额（元，{direction_zh}）"),
                "basis": (
                    "取数入口=src/fundflow/provider.py::FundFlowProvider."
                    "sector_snapshot()（板块截面，实测 0.7s）+ sector_history()"
                    f"（近 N 日序列，最多等 {_FUNDFLOW_HISTORY_TIMEOUT:g}s）；"
                    "**没有第二个实现**（不自己调 Tushare/东财）。"
                    "单位=元；序列不可用时**降级为截面当日口径并在此写明**，"
                    "绝不把「当日」说成「近 N 日」；某日无读数则该日不计入合计（不填 0）"
                ),
            },
            source_name=f"板块资金流（{data_source or 'FundFlowProvider'}）",
            source_url="store://fundflow_cache#sector_frame",
            fetch_method=FetchMethod.API_CALL,
            confidence=0.7 if nets else 0.55,
        )], None

    # ------------------------------------------------------ 族 H：行业轮动

    def _rotation_points(self, industry_name: str) -> tuple[list[DataPoint], str | None]:
        """「行业轮动日报」里该行业那一行 + 全局研判（当下/近期行情参考）。"""
        report = self._load_rotation_report()
        payload = report.get("payload")
        if not isinstance(payload, dict):
            return [], (
                f"本机没有可用的行业轮动日报（已落盘报告 {report.get('history_count')} 份，"
                f"最新 {report.get('latest')}）{_NO_MATCH_REASON}")
        rows = [r for r in (payload.get("industries") or []) if isinstance(r, dict)]
        matched, tier = _match_board_name(
            industry_name, [str(r.get("name") or "") for r in rows])
        if matched is None:
            return [], (
                f"行业「{industry_name}」不在行业轮动日报的 {len(rows)} 个板块里"
                f"{_NO_MATCH_REASON}")
        row = next(r for r in rows if str(r.get("name") or "") == matched)
        narrative = payload.get("narrative") or {}
        body = str(narrative.get("body") or "")
        truncated = len(body) > _ROTATION_BODY_MAX
        return [self._make_point(
            indicator=f"行业轮动:{industry_name}",
            value=None if row.get("pct") is None else float(row["pct"]),
            period_date=_iso_date(report.get("latest")),
            extra={
                "industry_name": industry_name,
                "matched_board": matched,
                "match_tier": tier,
                "report_date": _iso_date(report.get("latest")),
                "expected_trade_date": _iso_date(report.get("expected")),
                "is_stale": report.get("stale"),
                "stale_days": report.get("stale_days"),
                "stale_note": report.get("stale_note"),
                "row": row,
                "heat_top": report.get("heat"),
                "index_cards": payload.get("indices"),
                "market": payload.get("market"),
                "flow_1d": payload.get("flow_1d"),
                "flow_5d": payload.get("flow_5d"),
                "narrative": {
                    "headline": narrative.get("headline"),
                    "body": body[:_ROTATION_BODY_MAX],
                    "body_truncated": truncated,
                    "views": narrative.get("views"),
                },
                "meta": payload.get("meta"),
                "value_semantics": (
                    "value = 该行业当日涨跌幅（%，来自行业轮动日报的 industries 行）"),
                "basis": (
                    "取数入口=src/sector_rotation/store.py（按交易日落盘的 report_*.json）"
                    "+ service.pick_heat/expected_trade_date/is_stale；"
                    "**没有第二个实现**。报告可能落伍 ⇒ `is_stale`/`stale_days`"
                    "随数据下发，**不许当成「今天的行情」**"
                ),
            },
            source_name="行业轮动日报（src/sector_rotation）",
            source_url=f"store://sector_rotation#report_{report.get('latest') or ''}",
            fetch_method=FetchMethod.FILE_READ,
            confidence=0.7,
        )], None

    def _load_rotation_report(self) -> dict[str, Any]:
        """最近一份行业轮动日报 + 落伍判定（进程内缓存，文件读很便宜但别每次重读）。"""
        def _load() -> dict[str, Any]:
            from src.sector_rotation import service as rotation_service
            from src.sector_rotation import store as rotation_store

            latest = rotation_store.latest_date()
            payload = rotation_store.load(latest) if latest else None
            expected = rotation_service.expected_trade_date()
            stale = rotation_service.is_stale()
            stale_days: int | None = None
            if latest and expected:
                try:
                    stale_days = (
                        date(int(expected[:4]), int(expected[4:6]), int(expected[6:8]))
                        - date(int(latest[:4]), int(latest[4:6]), int(latest[6:8]))
                    ).days
                except ValueError:
                    stale_days = None
            boards = [b for b in ((payload or {}).get("industries") or [])
                      if isinstance(b, dict)]
            return {
                "latest": latest,
                "payload": payload,
                "expected": expected,
                "stale": stale,
                "stale_days": stale_days,
                "stale_note": (
                    None if not stale else
                    f"报告落在 {latest}，应有交易日是 {expected}"
                    + (f"，**落伍 {stale_days} 天**" if stale_days else "")
                    + " —— 不得当成今天的行情"
                ),
                "heat": rotation_service.pick_heat(boards, top=_ROTATION_TOP_N,
                                                   bottom=_ROTATION_TOP_N),
                "history_count": len(rotation_store.history()),
            }

        return dict(_cached("rotation_report", _ROTATION_TTL, _load))

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _make_point(
        *,
        indicator: str,
        value: float | None,
        period_date: str | None,
        extra: dict[str, Any],
        source_name: str,
        source_url: str,
        fetch_method: FetchMethod,
        confidence: float,
    ) -> DataPoint:
        """统一构造 DataPoint（本地库 → `source_type=FILE`）。"""
        return DataPoint(
            indicator=indicator,
            value=value,
            unit=None,
            period_date=period_date,
            extra=extra,
            source_name=source_name,
            source_url=source_url,
            source_type=DataSourceType.FILE,
            fetch_method=fetch_method,
            confidence=confidence,
            verified=False,
        )


__all__ = ["FamilyDiagnosis", "PlatformDataConnector", "reset_cache"]
