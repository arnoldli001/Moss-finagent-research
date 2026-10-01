# 数据索引体系 · 设计要求对账表（可执行审计）

> **本文件的存在理由**（用户原话，2026-09-28）：
> 「你改了≠生效了……要通过设计要求进行约束 …… 要变成**必须验证审计的项**，
>   避免实现与要求不一致。」
>
> **一键审计**：`uv run python scripts/audit_data_index.py`
> **退出码 0 = 7/7 合规**；非 0 = 有未达标项（可直接接 CI）
> **自动修复**：`uv run python scripts/audit_data_index.py --fix`
> **回归护栏**：`tests/unit/test_data_index_audit.py`（含 5 条反向测试）

---

## 一、需求原文（不得改写，作为审计基准）

用户在 2026-09-28 给出的原始要求：

> 「数据采集可以建立数据索引清单，根据所需数据快速判断哪些可以从数据库取，
>   哪些要联网获取，从数据库里快速批获取，联网获取的的数据也可以通过数据源
>   渠道快速获取，取到的新数据字段和数据源地址，存到到本地数据库，并加入到
>   索引表。根据数据源更新此数据的周期特点，记录到数据库更新周期，定期更新
>   下载数据。下次方便直接取。」

拆成 7 条：

| ID | 要求原文（拆解） |
|---|---|
| **R1** | 建立数据索引清单 |
| **R2** | 根据所需数据快速判断哪些可以从数据库取、哪些要联网获取 |
| **R3** | 从数据库里快速批获取 |
| **R4** | 联网获取的数据也可以通过数据源渠道快速获取 |
| **R5** | 取到的新数据字段和数据源地址，存到本地数据库，并加入到索引表 |
| **R6** | 根据数据源更新此数据的周期特点，记录到数据库更新周期，定期更新下载数据 |
| **R7** | 下次方便直接取 |

---

## 二、要求 → 实现 → 审计判据（逐条对账）

### R1 · 建立数据索引清单

| 项 | 内容 |
|---|---|
| **实现** | **两层索引**：<br>· 资产层 `data_asset_catalog`（`src/infrastructure/catalog/assets.py`）—— 表/目录级家底<br>· 指标层 `indicator_catalog`（`catalog_repo.py`）—— 单指标新鲜度 |
| **元数据来源** | `configs/indicators.yaml`（43 条指标，声明来源/频率/新鲜度/存储位置） |
| **自动扫描** | SQLite 全表遍历 + 目录递归统计；**不手写清单**（手写必漂移） |
| **审计判据** | ① 两张表都存在<br>② **资产层覆盖主库全部表**（允许 2 张差） |
| **实测** | `indicator_catalog=769 条`；`data_asset_catalog=55 条`；**主库 51 张表全部登记** |
| **修复的缺陷** | 原实现只覆盖 `fact_data_points`（199 万行），**漏了 `sector_crowding_daily`（218 万行，比它还大）** 与 `data/quant/`（35,855 文件 / 17.3 GB） |

### R2 · 快速判断 DB vs 联网

| 项 | 内容 |
|---|---|
| **实现** | `SmartFetcher.fetch_many`（`smart_fetch.py`）：<br>① `CatalogRepository.bulk_get` 一次拿全 freshness<br>② `FreshnessState.is_fresh()` 判定<br>③ fresh → DB-only；stale → 联网 |
| **交易时段感知** | `market_calendar.py`：非盘中时，`realtime`/`intraday` 指标只要数据日期 == 最近交易日就判 fresh |
| **审计判据** | ★ **不是"有代码"，而是"两条路径都真实走过"**：<br>① `fresh > 0`（快路径生效）<br>② `stale > 0`（慢路径判据有效）<br>③ `fresh/total ≥ 20%`（索引真的在起作用） |
| **实测** | `enabled=769；fresh=752；stale=17`（fresh 占比 **97.8%**） |
| **修复的缺陷** | 原判据只看代码存在 → 会漏掉"索引建了但从未回填"（实测就是这样） |

### R3 · 从数据库快速批获取

| 项 | 内容 |
|---|---|
| **实现** | `DataPointRepository.query_points_batch`（抽象）+ `MacroRepository._query_batch_sync`（SQLite 优化） |
| **审计判据** | ① 方法存在<br>② ★ **SQL 里必须真有 `IN (...)`** —— 用 `inspect.getsource` 检查，防止退化成 N 次单查<br>③ 实测批量 vs 串行加速比 + **结果一致性**（AGENTS.md：「两条路径结果必须相等」） |
| **实测** | 批量 **59.1ms** vs 串行 442.7ms（30 指标，**加速 7.5×**，结果一致 ✓） |
| **修复的缺陷** | ★ **`limit_per_indicator` 参数存在了三轮但从未真正生效**：原实现在 Python 端截断，SQL 仍拉全量（199 万行表里某指标 10 万行就拉 10 万行再扔 99.94%）。<br>修复前：批量 396ms vs 串行 402ms（**加速 1.0×，等于没优化**）<br>修复后：**59ms（7.5×）**<br>修法：窗口函数 `ROW_NUMBER() OVER (PARTITION BY indicator ORDER BY period_date DESC)` 把 limit 下推到 SQL |

### R4 · 联网通过数据源渠道快速获取

| 项 | 内容 |
|---|---|
| **实现** | ① 多源容灾链（`build_daily_connector_chain`：AkShare → 腾讯 → Tushare → baostock → 本地CSV → [QMT]）<br>② TCP 预检（`net_probe.probe_tcp`，2s + 60s 结论缓存）<br>③ 失败冷却（`source_cooldown`） |
| **审计判据** | ① 日线链源数 ≥2<br>② TCP 预检超时 **≤3s**（否则快速失败失效）<br>③ 失败冷却模块存在 |
| **实测** | 日线链 **5 源**；TCP 预检 **2.0s**；失败冷却 ✓ |

### R5 · 新数据（字段 + 源地址）入库并加入索引表

| 项 | 内容 |
|---|---|
| **实现** | ① 事实表溯源四件套：`source_url` / `source_name` / `fetch_time` / `raw_content_hash`（+`publish_time`）<br>② A04 落库后**幂等重算**索引（`refresh_stats_from_facts`） |
| **审计判据** | ① 溯源字段齐全<br>② ★ **实测"入库 → 索引同步"这条链真的接通**：抽检事实表里有数据的指标，索引 `row_count` 必须 > 0 |
| **实测** | 溯源四件套 ✓；**入库→索引同步 ✓（抽检 20 个）** |
| **修复的缺陷** | 原实现 SmartFetcher 直接写库 → **绕过 A03 校验**。已改 `persist=False`，落库仍由 A04 负责 |

### R6 · 记录更新周期 + 定期更新下载

| 项 | 内容 |
|---|---|
| **实现** | ① `frequency` + `freshness_hours` 字段（YAML 声明 → DB 索引）<br>② `catalog_jobs.py`：**频率 → cron 的唯一权威映射**，自动生成批量采集作业<br>③ lifespan 自动注册进 `JOB_REGISTRY` |
| **审计判据** | ① 频率标注覆盖率（>`unknown` 不超过 50%）<br>② **实际生成了作业**（不是"代码里有"）<br>③ `catalog_coverage()` 的 **uncovered == 0**<br>④ 白名单里每个 frequency 都有 cron 决策 |
| **实测** | 频率已标注 **769/769**；catalog 作业 **4 个**；**43 指标 100% 覆盖** |
| **可执行性** | `uv run python -m src.scheduler.catalog_jobs --dry-run --table --coverage` |

### R7 · 下次方便直接取

| 项 | 内容 |
|---|---|
| **实现** | `SmartFetcher` 的 DB-only 路径 + 索引回填 |
| **审计判据** | ★ **实测**（不是读代码）：<br>① 真实库上跑 `fetch_many`，DB 命中率 ≥60%<br>② 耗时 **≤500ms**<br>③ 与生产同构（必须传 `limit_per_indicator=60`，否则拉全量导致判据失真） |
| **实测** | **12/12 命中 DB，耗时 318ms，联网 0 个** |
| **端到端验证** | 用户问题命中的 16 个指标：**13,100ms → 39ms**，联网指标 **3 → 0** |

---

## 三、审计器的自我验证（防止审计器本身骗人）

AGENTS.md 硬约束：
> 「**自己的检查脚本必须先自证**：喂一个"已知答案"的输入，确认它报 0。
>   本轮实测：自写正则报 6 个假告警，差点去修没坏的东西。」

审计器若本身有 bug，会在两个方向害人：
- **假绿** → 明明没实现却报 PASS（正是用户担心的"改了≠生效了"）
- **假红** → 明明实现对了却报 FAIL（会去修没坏的东西）

`tests/unit/test_data_index_audit.py` 守三件事：

| 测试 | 守什么 |
|---|---|
| `test_auditor_runs_all_seven_requirements` | 7 条都能跑完，每条都给证据（**空证据 = 无法 review**） |
| `test_current_repo_passes_all_requirements` | 当前状态 7/7（**配置漂移哨兵**） |
| `test_r1_fails_when_asset_table_missing` | R1 认得出"缺表" |
| `test_r1_fails_when_asset_coverage_incomplete` | R1 认得出"表有但没登记全"（本轮修的核心缺陷） |
| `test_r2_fails_when_nothing_is_fresh` | R2 认得出"全 stale"（索引没起作用） |
| `test_r3_detects_non_batched_implementation` | R3 认得出"退化成了 for 循环单查" |
| `test_r6_fails_when_no_frequency_mapping` | R6 认得出"频率缺 cron 映射" |
| `test_r7_threshold_is_meaningful` | R7 与生产同构（传 limit_per_indicator） |
| `test_asset_scan_finds_big_tables` | 扫描能发现非主事实表（`sector_crowding_daily`） |

**5 条反向测试**（构造坏输入 → 必须 FAIL）确保审计器不会假绿。

---

## 四、本轮"改了≠生效了"清单（我实际踩的坑）

| # | 我"改了"什么 | 为什么没生效 | 怎么发现的 |
|---|---|---|---|
| 1 | 建了 `indicator_catalog` 表 | **从没有地方在启动时调 `refresh_from_registry()`** → YAML 43 条，表里 16 条 | 用户问"今天没开盘，库里明明有最新数据" |
| 2 | 写了 `refresh_stats_from_facts()` | **只在 A04 节点对"本次任务涉及的指标"调用** → 索引表建立前的存量数据（199 万行）从未登记 | 同上：事实表 241 条，索引记 `row_count=0` |
| 3 | 加了 `expands_to` 参数前，`mkt:cybkcb:turnover:all` | 它是**聚合请求**，返回 3 条子指标，"all" 永不入库 → `row_count` 恒 0 → **每次都联网** | 修完前两项后仍有 3 个指标联网 |
| 4 | `limit_per_indicator` 参数（存在三轮） | 只在 Python 端截断，**SQL 仍拉全量** | 审计 R7 判 FAIL（1491ms） |
| 5 | `is_market_open` 用了带时钟的 `is_trading_day` | 市场时钟只回答"今天是不是交易日"，判历史/未来日期必然 False → 不可测 + 语义错位 | 加了 freshness 测试后 3 条红 |

**共同特征**：全部**不报错**，全部**看起来像"已实现"**。
唯一能发现它们的方式是**端到端实测 + 反向测试** —— 这正是本对账表存在的意义。

---

## 五、接入方式（让审计成为流程的一部分）

```bash
# 1) 收尾必跑（人工/CI 都适用，退出码非 0 即未达标）
uv run python scripts/audit_data_index.py

# 2) 自动修复后重审
uv run python scripts/audit_data_index.py --fix

# 3) 机器可读（接 CI 用）
uv run python scripts/audit_data_index.py --json

# 4) 回归护栏（CI 必跑）
uv run python -m pytest tests/unit/test_data_index_audit.py -q

# 5) 运维：索引差异检查（只读）
uv run python scripts/rebuild_indicator_catalog.py --check

# 6) 运维：数据在哪
uv run python scripts/rebuild_indicator_catalog.py --locate CPI
uv run python scripts/rebuild_indicator_catalog.py --by-backend file

# 7) 运维：哪些数据该更新了
uv run python scripts/rebuild_indicator_catalog.py --stale

# 8) 运维：本地家底盘点
uv run python scripts/inventory_data_assets.py
```

后端启动时（`api/main.py` lifespan）自动执行：
1. `CatalogRepository.rebuild_all()` —— 指标索引全量重建
2. `DataAssetCatalog.scan_and_store()` —— 数据资产全量扫描
3. `install_catalog_jobs()` —— 从频率生成调度作业

---

## 六、当前状态（2026-09-28 实测）

```
数据索引体系 · 设计要求合规审计    7/7 通过
  R1 建立数据索引清单              ✅ indicator_catalog=769 / data_asset_catalog=55 / 51 张表全登记
  R2 快速判断 DB vs 联网           ✅ enabled=769 / fresh=752 / stale=17（fresh 97.8%）
  R3 从数据库快速批获取             ✅ IN(...) 下推 / 59.1ms vs 442.7ms（7.5×）
  R4 联网渠道快速获取               ✅ 5 源 / TCP 预检 2.0s / 失败冷却
  R5 入库并加入索引表               ✅ 溯源四件套 / 入库→索引同步（抽检 20）
  R6 记录周期 + 定期更新            ✅ 频率 769/769 / 4 个作业 / 43 指标 100% 覆盖
  R7 下次方便直接取                 ✅ 12/12 命中 DB / 318ms / 联网 0
```

**端到端效果**（用户真实问题命中的 16 个指标）：

| | 修复前 | 修复后 |
|---|---:|---:|
| 采集墙钟 | **13,100ms** | **39ms** |
| 走 DB | 13 个 | **18 个** |
| 联网 | 3 个 | **0 个** |
