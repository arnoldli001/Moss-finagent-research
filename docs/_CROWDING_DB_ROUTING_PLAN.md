# 拥挤度库归属整改：影响面评估与实施方案

> 目标（用户 2026-09-30 指令）：
> **第 1 步** 把拥挤度登记进 `configs/data_stores.yaml`（堵住写者闸门缺口）
> **第 2 步** 把**用户配置**表（`sector_crowding_list` / `_watch` / `_alert`）
> 从共享遗留主库迁到**本环境应用库 `app_db`**
> 要求：改动前完成全面影响面评估与测试范围圈定，实施后相关判据全通过。

## 0. 现状实测（不是推断）

### 0.1 数据实际落在哪

逐库列 `sector_crowding_*` / `sector_meta` / `sector_member` / `ml_*`：

| 表 | legacy_main | pilot app_db | dev app_db | mainline_cache |
|---|---|---|---|---|
| `sector_crowding_daily`（218 万行） | **有** | — | — | — |
| `sector_crowding_max_ma5` | **有** | — | — | — |
| `sector_crowding_metric` | **有** | — | — | — |
| `sector_crowding_list`（1,226 行） | **有** | — | — | — |
| `sector_crowding_watch`（7 行） | **有** | — | — | — |
| `sector_crowding_alert`（0 行） | **有** | — | — | — |
| `sector_meta` / `sector_member` | **有** | — | — | — |
| `ml_*`（全部） | — | — | — | **有** |

**结论**：拥挤度的**全部**表只在 `legacy_main`；`app_db` 一张都没有。

### 0.2 路径解析（为什么 pilot 会落到共享库）

```
manage.py:1013   pilot 注入 MOSS_SQLITE_PATH = data/pilot/moss_pilot.db
                 → settings.sqlite_path（账号/告警/池子/选股结果）✅ 隔离正确

sector_crowding/config.yaml:12   path: "data/moss_finagent.db"   ← 写死
                 → load_config().db_path  ❌ 绕过 --env 隔离，三个实例同一个文件
auction_select/config.yaml:452   同样写死
```

### 0.3 数据新鲜度（顺带查到）

```
sector_crowding_daily  最新 trade_date = 20260924
                      最后写入 updated_at = 2026-09-27T18:52:01
sector_meta            最近更新同为 2026-09-27T18:52:01
```

**没有任何调度作业更新它** —— 全仓库只有 `crowding_metrics_weekly`（写
`sector_crowding_metric`）。`sector_crowding_daily` 只由**手动一键刷新**
（`POST /refresh_all`）或脚本写。最后写入是 09-27，已陈旧。

## 1. 影响面评估

### 1.1 `get_db_connection` 的调用方（**门禁要挂在这里，所以这是核心半径**）

生产代码 **18 处**，另有测试 11 处 + 脚本 2 处：

| 模块 | 处数 | 读/写 | 说明 |
|---|---|---|---|
| `src/sector_crowding/refresh.py` | **4** | **读+写** | 写 `sector_crowding_daily` / `sector_meta` / `max_ma5` |
| `src/sector_crowding/metrics.py` | **5** | **读+写** | 读 daily，写 `sector_crowding_metric` |
| `src/sector_crowding/sources.py` | 1 | 只读 | 读板块列表/日线 |
| `src/api/routes/sector_crowding.py` | 1 | 读+写 | `_conn()`，所有拥挤度接口共用 |
| `src/api/routes/auction_select.py` | 1 | 读+写 | `_conn()` |
| `src/auction_select/{local_source,rulebook,scheduler,service}.py` | 5 | 读+写 | 竞价选股 |
| `src/sector_crowding/db.py` | 1 | 内部 | `init_tables()` 自建连接 |

⚠️ **关键约束**：`refresh.py` 与 `metrics.py` 是**同一个连接既读又写**。
所以门禁**不能**无条件挂在 `get_db_connection` 上 —— 那会把**读**也一起挡掉，
而 pilot 必须能读共享的 `sector_crowding_daily`。

### 1.2 跨模块读 `sector_crowding_list` 的地方（**第 2 步会动到它们**）

| 消费方 | 位置 | 现状 | 第 2 步之后 |
|---|---|---|---|
| 平台自有数据连接器 | `platform_data_connector.py:258` `_TABLE_CANDIDATES` | `(legacy_main, app_db)` | 需改为 `(app_db, legacy_main)` |
| 主线入池 | `mainline/datastore.py:1867` `import_crowding_pool()` | 读 `settings.sqlite_path` = **app_db** → 表不在 → **failed** | ✅ **自动修好** |
| 主线成分股 | `mainline/datastore.py:2016` `import_crowding_members()` | 同上（`sector_member` 不在 app_db） | `sector_member` 留在共享库 → 仍 failed（**本轮不动**） |
| 调度作业 | `scheduler/jobs.py:1472` 注释 | `board_crowding`/`member_crowding` 已被列入 `_MAINLINE_OPTIONAL_DATASETS`（天天 failed 但不牵连整轮） | 部分修好 |
| 主线黑名单生成 | `mainline/datastore.py:1908` | 读上面那个 path | 同 ① |

★ **重要发现**：`mainline/datastore.py` 的 `import_crowding_pool()` **已经**在按
"app_db" 口径读（用 `settings.sqlite_path`），只是**表不在那儿**所以一直 failed。
`jobs.py:1472` 的注释也写着"要读**应用库**里的 `sector_crowding_list`"。
⇒ **第 2 步是把现实对齐到"代码早就声明的意图"**，不是改口径。

### 1.3 脚本（6 个，全部写死 `MAIN_DB = data/moss_finagent.db`）

| 脚本 | 读写哪个表 | 第 2 步之后 |
|---|---|---|
| `scripts/narrow_crowding_scope.py` | list | ⚠️ 失效，需改读 app_db |
| `scripts/gen_sector_blacklist.py` | list | ⚠️ 同上 |
| `scripts/prune_crowding.py` | list（UPDATE visible） | ⚠️ 同上 |
| `scripts/prune_concepts.py` | list（UPDATE visible） | ⚠️ 同上 |
| `scripts/resolve_crowding_list.py` | list（只读） | ⚠️ 同上 |
| `scripts/toggle_board_watch.py` | list（增删改） | ⚠️ 同上 |

⚠️ `scripts/` **不入库**（`.gitignore`），但会被运维用 ⇒ 仍必须一起改，
并在文档里写明"拥挤度用户配置在**本环境应用库**"。

### 1.4 写者闸门的真实机制（**关键：登记 ≠ 生效**）

实测 `writable_here()` 有两态：

```
dev    legacy_main  allowed=False decided=False   ← 「待裁定」只报告不阻断
pilot  legacy_main  allowed=False decided=False
dev    app_db       allowed=True  decided=True    ← per_env + writer: own
pilot  app_db       allowed=True  decided=True
```

且全仓库只有 **`QuantWarehouse`** 真正调 `writable_here` 做 fail-closed。
**拥挤度没有任何写闸门** —— 它的写走自己的 `get_db_connection`。

⇒ 所以：
* **登记本身只带来两件事**：① `/health` 与 `describe()` 里可见 ②
  `JobSpec.updates` 的裁剪有据可依（拥挤度没有更新的调度作业，所以这条暂无效果）；
* **真正的强制必须另加**：在拥挤度的写路径上显式 `assert_writable`。

`writer` 取值语义（实测）：

| 取值 | `decided` | 效果 |
|---|---|---|
| `own` / `per_env` | True | 本环境私有，恒可写（`app_db` 用的这个） |
| `dev` / `test` / `pilot` | **True** | **点名到具体环境 → fail-closed 真拦截** |
| `main` | **False** | 隔离档只报告不阻断（`legacy_main` 现状） |

## 2. 方案（按最小侵入 + 默认值即护栏）

### 2.0 关键设计决定：**一条连接 + ATTACH**，而不是两条连接

`db.py` 里有 **10 处 SQL 同句引用参考表（`sector_crowding_daily` /
`sector_meta` / `max_ma5`）与配置表（`list` / `watch` / `alert`）**：

```
query_all_latest_water_level   LEFT JOIN LIST_TABLE
query_alerts                   LEFT JOIN LIST_TABLE
query_list_view                LEFT JOIN LIST/META/WATCH/ALERT
seed_list                      SELECT ... FROM META_TABLE
hide_dead_boards               UPDATE LIST ... WHERE EXISTS(SELECT 1 FROM META_TABLE)
has_hideable_dead_boards       同上（SELECT）
query_hidden_boards            LEFT JOIN META_TABLE
count_hidden_dead              LEFT JOIN META_TABLE
```

若把它们拆成两条连接、在 Python 里合并，要重写 **10 处查询 + 5 个导出函数**
（`seed_list` / `hide_dead_boards` / `has_hideable_dead_boards` /
`query_hidden_boards` / `count_hidden_dead`）—— 而 `seed_list` 与
`hide_dead_boards` 是**跨库写**，SQLite 明确不支持（`UPDATE ... WHERE EXISTS`
跨库会报 "no such table"）。

⇒ 采用**项目既有范式**（`data_stores.open_readonly()` 就是 `ATTACH` 多库，
实测 4.6 ms 零字节复制）：

* **连接主体 = `crowding_shared`**（参考数据所在），
* **`ATTACH` 本环境 `app_db`**，别名 `app_db`，
* 配置表在 SQL 里写 **`app_db.sector_crowding_list`**（加 schema 前缀即可）。

这样 10 处 JOIN 全部保持 Sqlite 原生执行，**Python 侧一行合并逻辑都不用写**。

⚠️ 一处必须改逻辑：`seed_list` 的 `INSERT INTO list SELECT ... FROM meta`
是**跨库 INSERT...SELECT**（SQLite 同样禁止）。改为**两趟**：
共享库读 meta → 本环境库写 list。语义不变，且这是**一次性种子化**路径。

### 第 1 步：登记 + 真闸门

1. `configs/data_stores.yaml` 新增 `crowding_shared`：
   `isolation: shared`、`role: source`、`path: data/moss_finagent.db`、
   **`writer: dev`**（用户 2026-09-30 裁定）、`writable: true`。✅ 已完成
2. `src/sector_crowding/db.py::get_db_connection(..., writable: bool = False)`：
   **默认 `False`（安全侧）**；`writable=True` 时先问 `assert_writable` → 非写者 fail-closed。
   ✅ 已完成
3. `refresh.py` 的 4 处写连接显式传 `writable=True`。

**为什么默认 False**：`AGENTS.md`「默认值即护栏，安全的一侧做成默认」。
读路径（pilot 读共享参考数据）必须照常工作。

### 第 2 步：用户配置迁到 app_db

4. `SectorCrowdingConfig` 新增 `config_path`（本环境应用库）：
   * `path` → `data/moss_finagent.db`（共享参考数据，**保持不变**）
   * `config_path` → `store_rel("app_db")`（本环境）
   * 单测用 `path=` 传临时库时，`config_path` **跟随同一个临时库**
     （保既有单测语义不变）。
5. `db.init_tables()` DDL 按库拆分：参考表建在 `path`，配置表建在 `config_path`。
6. `platform_data_connector._TABLE_CANDIDATES`：
   `sector_crowding_list` → `(app_db, legacy_main)`（**顺序反转**）。

   ⚠️ **必须反转**：`_table_store()` 取**第一个存在该表**的候选。不反转的话
   永远命中 `legacy_main` 里那张旧表，迁移**静默失效**。
   `crowding_daily` / `max_ma5` **保持** `(legacy_main, app_db)`（它们没搬）。
7. 6 个脚本改读本环境应用库。
8. pilot 首次切换用**已有的** `db.seed_list()` 种子化（用户裁定：**种全量概念板块**）。


## 3. 用户裁定（2026-09-30，已确认）

| 问题 | 裁定 |
|---|---|
| `crowding_shared.writer` | **`dev`** —— 本机开发实例是写者（实测刷新历来在它跑）；pilot 点刷新 fail-closed 拒 |
| pilot 清单初始化 | **种全量概念板块**（用已有的 `db.seed_list()`；**不迁移** legacy 那 1,226 行） |


## 4. 测试范围圈定（G5.4.2）

| 改动 | 必跑 |
|---|---|
| `src/sector_crowding/db.py` | `test_sector_crowding.py`、`test_crowding_slim_projection.py` |
| `src/sector_crowding/refresh.py` | `test_sector_crowding.py` |
| `src/sector_crowding/config.py` | `test_sector_crowding.py`、`test_store_registry.py` |
| `configs/data_stores.yaml` | `test_store_registry.py`、`test_warehouse_write_ownership.py`、`test_scheduler_role_split.py` |
| `src/api/routes/sector_crowding.py` | `test_api.py`、`test_crowding_*` |
| `platform_data_connector.py` | `test_connector_field_reachability.py`、`test_connector_capabilities*` |
| `mainline/datastore.py` | `test_mainline_daily_job.py`、`test_mainline_blacklist.py` |
| 新增判据 | 本文件 §5 |

## 5. 待新增判据

1. `crowding_shared` 必须登记且 `writer` 必须是**具体环境**（不是 `main`）；
2. 拥挤度**用户配置表**必须落在 `app_db`（主键查 `sqlite_master`）；
3. 拥挤度**参考表**必须留在共享库；
4. `get_db_connection` 默认 `writable=False`（语法树/签名判据，防有人改成默认 True）；
5. 非写者实例上传 `writable=True` 必须 fail-closed 且理由含写者名；
6. `_TABLE_CANDIDATES["sector_crowding_list"]` 首选必须是 `app_db`；
7. `refresh.py` 的 4 处写连接必须显式 `writable=True`（防漏传 → 静默只读）。

## 6. 已知缺口（诚实登记）

* `sector_member` / `sector_meta` 留在共享库 ⇒ `import_crowding_members()` 在
  隔离档**仍会 failed**（`member_crowding` 已在 `_MAINLINE_OPTIONAL_DATASETS` 里，
  不牵连整轮）。本轮**不动**，登记为已知缺口；
* `auction_select` 同样写死路径，**本轮不动**（同一形状，另开一轮）；
* `sector_crowding_daily` 已陈旧（最新 20260924 / 最后写入 09-27）——
  这是**数据更新责任**问题，与本轮存储归属整改正交，登记为已知缺口。
