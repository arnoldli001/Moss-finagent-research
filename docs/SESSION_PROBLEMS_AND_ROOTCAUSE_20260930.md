# 会话问题清单 / 根因 / 解决方案 — 2026-09-30 会话

> **范围**：本文件只记录 **2026-09-30 这一轮会话**（拥挤度详情慢 → 浮点精度 → 库归属 → 自动刷新）
> 里发现并处理的问题。
> 全项目的问题盘点看 `docs/PROBLEM_INVENTORY_20260926.md` 与 `docs/PROJECT_AUDIT_2026-09-27.md`。
>
> **写法纪律**（沿用本项目既有约定）：
> 每条都写 **报障原话 / 实测证据 / 根因 / 处理 / 判据 / 已知缺口**，
> 且证据必须是**可复跑的文件:行号或命令**，不写"已确认""见代码"。
> 区分「量到 0」与「没量到」——没量到就写没量到。

---

## 零、总览

| # | 问题 | 类型 | 严重度 | 状态 |
|---|---|---|---|---|
| P1 | 拥挤度详情「十几秒才出数据」 | 性能（传输体积） | 高（用户直接报障） | ✅ 已修 |
| P2 | 平台下发浮点精度不统一、尾数污染体积 | 口径缺失 | 中 | ✅ 已修 + 立口径 |
| P3 | 拥挤度**用户配置**跨环境共用一份 | 数据分类错位 | **高**（客户数据） | ✅ 已修 |
| P4 | 写者闸门对拥挤度**完全没有约束** | 治理缺口 | 高 | ✅ 已修 |
| P5 | 拥挤度数据**没有任何调度更新它** | 运维缺口 | 高（数据陈旧） | ✅ 已修 |
| P6 | 「一键刷新」的调用次数**没有预算上限** | 成本治理 | 中 | ✅ 已修 + 硬约束 |
| P7 | 隧道劣化被误认为"带宽不够" | 归因错误 | 中（决策错误） | ✅ 已纠正 |
| P8 | 本轮**我自己**造成的 9 处失误 | AI 过程缺陷 | 中 | ✅ 已修 + 立 skill |
| P9 | 腾讯云备用入口（Cloudflare）长期不通 | 基础设施 | 中 | ⚠️ **未修**（登记） |

---

## P1 拥挤度详情「十几秒才出数据」

### 报障原话

> 「板块拥挤度 里，打开单个概念的历史拥挤度数据的图，**十几秒才出数据**，
>   看下什么原因，加载这么慢」

### 实测证据

`src/sector_crowding/db.py`（`_REFERENCE_SCHEMA` 上方注释记录了这组实测）：

| 环节 | 实测 |
|---|---|
| `db.query_sector_meta`（全表 2517 行） | 5.9 ms |
| `db.query_sector_crowding`（1268 根，走 `idx_crowding_sector_date`） | 3.3 ms |
| Python 组装（水位扫描 + 元数据线性查找） | 0.2 ms |
| **后端合计** | **≈ 13 ms** |
| JSON **明文** | **424~502 KB** |
| gzip 后（`GZipMiddleware(compresslevel=6)`） | **66~76 KB** |
| 隧道传输 @51 KB/s | 1.3~1.5 s |
| **隧道传输 @4.6 KB/s（劣化档）** | **14.1~16.1 s** ← 用户看到的就是这个 |

### 根因（三个，都是"体积"不是"速度"）

| # | 根因 | 实测代价 |
|---|---|---|
| ① | **浮点按完整 double 序列化**：前端只显示 3 位小数，却传 `0.00024339722008355307`（19 位）。实测 **2348 个值带 18~20 位小数** | **gzip 压不动**（高熵尾数不可压）——最大头 |
| ② | **`SELECT *` 带出前端从不读的列**：`created_at`(12.5%) + `updated_at`(12.5%) + `id`(4.0%) = **28.9% 明文**；两个时间戳在 1268 行里**逐字相同** | 明文 120,467 B 纯冗余 |
| ③ | 该接口**没有客户端缓存** | 关掉弹窗再开同一板块，424 KB 原样重传 |

### 处理

| 层 | 落点 |
|---|---|
| 存储层 | `db.query_sector_crowding(..., slim=False)` 新增 **opt-in** 参数 + `SLIM_COLUMNS` / `SLIM_PRECISION` / `_slim_row()` |
| 接口层 | `routes/sector_crowding.py::_sector_detail_sync` 显式 `slim=True` + 元数据改**主键直查** |
| 前端 | 新增 `web/src/crowdingDetailCache.ts`（localStorage + SWR + **LRU 上限 8**），弹窗改"先画缓存再核对" |

**关键实测（反直觉）**：**只去字段只降 9%**（gzip 本来就能压掉重复值）；
**真正让 gzip 重新有效的是"按显示精度取整"** —— gzip 66,507 B → **33,491 B（砍半）**，
@4.6 KB/s 从 14.1 s → 7.1 s。**所以精度既是体积问题、也是压缩率问题。**

### 判据

`tests/integration/test_crowding_detail_payload_size.py`（12 条）+
`tests/unit/test_crowding_slim_projection.py`（11 条）。含两条**反向断言**：
`slim=False` 必须显著更大；明文传输必须超阈值（**带宽假设过时 ⇒ 全部阈值需重评**）。

### 已知缺口

**33.5 KB 在劣化档（4.6 KB/s）仍是 7.1 秒** —— 本轮把"十几秒"降到"几秒"，
**没有**降到"瞬时"。要再降必须改**传输形态**（分页/降采样/二进制列式），
而"近 6 年全量曲线"是用户明确要的口径 ⇒ 属产品变更，本轮没做。

---

## P2 平台下发浮点精度不统一

### 需求原话（两轮）

> 第一轮：「检索下项目平台的所有业务、功能板块，所有涉及浮点数传递的都
>   **小数点后最多保留 3 位**，避免传输数据过大」
>
> 第二轮（看到实测后裁定）：「**一律 3 位有效数字**，同时作用于**服务端出口**
>   和**前端缓存写入**」

### ★ 根因里最值得记的一条：**字面口径会销毁数据**

```
字段 raw_crowding（板块成交额占比），真实量级 1e-4
原值 0.00024339722008355307  →  取 3 位小数 = 0.0
该字段 96 个不同取值           →  只剩 1 个，1416 个曲线点全部归零
```

占比 / 胜率 / IC / 相关系数 / `net_to_mv`(1e-9~1e-3) / 佣金率(0.00025) **全是这一族**。
**有效数字口径自适应量级，天然不归零。**

### 实测对照（拥挤度详情，最坏板块 1480 根）

| 有效数字 | gzip | vs 基线 | 曲线不同取值 | 归零 |
|---|---|---|---|---|
| 基线 | 36,258 B | — | 96 | 0 |
| **3 位（现行）** | **22,318 B** | **−38%** | **96** | **0** |
| 4 位 | 24,287 B | −33% | 96 | 0 |
| 5 位 | 26,200 B | −28% | 96 | 0 |
| 6 位 | 28,016 B | −23% | 96 | 0 |

### 处理：两端各一处「唯一出口」

| 端 | 落点 | 为什么是这里 |
|---|---|---|
| 服务端 | `src/api/main.py::_SafeJSONResponse.render` → `src/core/float_precision.py::round_payload` | 全平台 JSON **唯一出口**（原为兜 NaN 而建），**一次覆盖 216 个 HTTP 端点** |
| 前端 | `web/src/floatPrecision.ts` + `crowdingDetailCache.writeCrowdingDetail` | localStorage 存**明文不压缩**，一份 258 KB ⇒ 约 **19 份撑爆 5 MB 配额**，且会**连累告警/情报/因子库的缓存一起写不进去** |

### 各业务板块实测收益

| 端点 | 轮询 | gzip | 收益 |
|---|---|---|---|
| `/sector_crowding/sectors_max_ma5` | 页面加载 | 31,485→**12,305** | **−61%** |
| `/sector_crowding/latest` | — | 14,945→**9,747** | **−35%** |
| `/sector_crowding/alerts` | — | 27,170→**19,225** | **−29%** |
| `/sector_crowding/config_list` | **60 s** | 24,333→**18,503** | **−24%** |
| `/sector_crowding/{code}`（已瘦身） | — | 36,258→36,258 | **0%（幂等反向证据）** |

### 判据

`tests/unit/test_float_precision.py` **30 条**。两条最关键：
★ **小量级不许归零**（含 `net_to_mv` 1e-9、佣金率 0.00025）；
★ **NaN/±Inf→null 不许丢**（那是 2026-09-27 做T快照 500 的修复，与精度压缩共用同一次遍历，最容易在重构时掉一个）。

### 已知缺口

- **3 位有效数字对千元以上价格是粗的**（`1109.28→1110` 丢分位）。当前无"必须保分位"
  的字段；将来若出现应走**字段自己模块显式取整 + PRD 记录**的正路，**不要往豁免表加**；
- 审计的 **~27 个端点字段集未能静态确认**，"这些字段 3 位是否够用"**没有逐一复核**；
- ★ **两个 WebSocket 端点（`/ws/alerts`、`/ws/intraday`）不走 HTTP 出口，传的浮点未被覆盖**，
  是否覆盖**尚未裁定**。

---

## P3 拥挤度**用户配置**跨环境共用一份（最高严重度）

### 实测证据

逐库列 `sector_crowding_*`：

| 表 | legacy_main | pilot app_db | dev app_db |
|---|---|---|---|
| `sector_crowding_list` | **有（1,226 行）** | — | — |
| `sector_crowding_watch` | **有（7 行）** | — | — |
| `sector_crowding_alert` | **有** | — | — |

⇒ **dev 与 pilot 共用同一份板块清单与告警阈值**：开发时点掉的板块，**客户那边也消失**。

### 根因：**数据分类错位**（不是配置笔误）

拥挤度把两类**生命周期完全不同**的数据塞进同一个库、同一条连接：

| 数据 | 本质 | 该在哪 |
|---|---|---|
| `sector_crowding_daily` / `sector_meta` / `sector_member` / `max_ma5` | **市场参考数据**（无用户维度） | 共享库 ✅ |
| `sector_crowding_list` / `_watch` / `_alert` | **用户配置**（每人/每租户一份） | **本环境应用库** ❌ 原在共享库 |

★ **平台另有 11 张表带 `tenant_id`**（`dim_user_pool` / `dim_user_watchlist_v2` /
`fact_alerts` / `fact_events`…），**拥挤度是唯一例外**。

★ **代码早就写着正确答案、只是没执行**：
`scheduler/jobs.py:1472` 注释说"要读**应用库**里的 `sector_crowding_list`"；
`platform_data_connector.py:253` 说"dev/pilot 下 `data/<env>/moss_<env>.db` 里**没有**
`sector_crowding_*`"；而 `mainline/datastore.py:1867` 的 `import_crowding_pool()`
**已经**按应用库口径读 —— 只是表不在那儿，所以**一直 failed**（被
`_MAINLINE_OPTIONAL_DATASETS` 兜着，天天记 failed 但不牵连整轮）。
⇒ **本轮是把现实对齐到已声明的意图。**

### 处理

| # | 改动 |
|---|---|
| ① | `configs/data_stores.yaml` 新增 `crowding_shared`（`writer: dev`，用户裁定） |
| ② | `db.py`：DDL **拆两半**（参考表/配置表）；`get_db_connection(writable=False)` + **无条件 `ATTACH`**；`assert_writable()`；`config_table()`；`ensure_config_tables()`；**43 处 SQL 加 `app_db.` 前缀** |
| ③ | `config.py`：新增 `config_path` / `config_db_path`（默认 registry `app_db`） |
| ④ | `platform_data_connector._TABLE_CANDIDATES["sector_crowding_list"]` → **`(app_db, legacy_main)`** |
| ⑤ | 6 个脚本路径解析改走 `load_config().config_db_path`（原先全部写死 `data/moss_finagent.db`） |

### ★ 关键设计决定：一条连接 + `ATTACH`，不是两条连接

`db.py` 有 **10 处 SQL 同句引用参考表与配置表**（`query_list_view` / `query_alerts` /
`query_all_latest_water_level` / `seed_list` / `hide_dead_boards` /
`query_hidden_boards` / `count_hidden_dead` / `has_hideable_dead_boards`…）。
拆两条连接要重写这 10 处 + 5 个导出函数，其中
**`seed_list`（`INSERT ... SELECT`）与 `hide_dead_boards`（`UPDATE ... WHERE EXISTS`）
是跨库写，SQLite 明确不支持**。

⇒ 沿用项目既有范式（`data_stores.open_readonly()` 同样是 `ATTACH` 多库）：
主库 = 共享参考数据，`ATTACH` 上本环境配置库（别名 `app_db`），
配置表 SQL 写 `app_db.<table>`。**10 处 JOIN 全部保持 SQLite 原生执行，Python 侧零合并逻辑。**

### 判据

`tests/unit/test_crowding_db_routing.py` **18 条**，含
★ **跨库 JOIN 与跨库写的真实 SQL 验收**（不是只断言常量）、
★ **两侧对称的"不许出现对方的表"**、4 条语法树接入判据。
**自证跑过**：候选顺序改回 `legacy` 在前 ⇒ 2 条红；`writable` 默认改 `True` ⇒ 1 条红。

### 已知缺口

- `sector_member` / `sector_meta` 留共享 ⇒ `import_crowding_members()` 在隔离档**仍 failed**；
- **旧库那两张表不清**（留作回滚）⇒ 代价是连接器的"按表存在选库"**永远**需要候选顺序；
- **手动「一键刷新」与自动任务没有跨进程互斥**（API 侧任务锁是**每进程**的）。

---

## P4 写者闸门对拥挤度完全没有约束

### 实测证据

拥挤度的表**根本没登记**在 `configs/data_stores.yaml`，而写者闸门是
从登记派生的（`JobSpec.updates × writable_here()`）⇒

- 前端「一键刷新」（`POST /sector_crowding/refresh_all`）往共享库 **UPSERT 218 万行**，
  **不受任何闸门管**；
- `/health` 与 `describe()` 里**看不见它**。

### ★ 根因里最反直觉的一条：**登记 ≠ 生效**

实测 `writable_here()` 的两态与调用方：

```
dev    legacy_main  allowed=False decided=False   ← 「待裁定」只报告不阻断
pilot  legacy_main  allowed=False decided=False
dev    app_db       allowed=True  decided=True
```

而全仓库**只有 `QuantWarehouse`** 真正调 `writable_here()` 做 fail-closed。
**拥挤度写的是自己的连接** ⇒ 光登记不构成强制。

### 处理（两处一起才生效）

1. 登记 `crowding_shared`（`writer: dev`）；
2. `db.get_db_connection(..., writable: bool = False)` —— **默认 False（安全侧）**，
   `writable=True` 时 `assert_writable()` fail-closed；`refresh.py` 的 4 处写连接显式传 `True`。

**为什么默认必须是 False**：读路径（pilot 读共享参考数据）绝不能因此变红，
而 `refresh.py` / `metrics.py` 是**同一个连接既读又写**。

**为什么写者点名 `dev` 而不是 `main`**：`main` ⇒ `writable_here()` 对隔离档只能给
`decided=False`（只报告不阻断）⇒ **闸门形同虚设**。

### 判据

`test_get_db_connection_writable_defaults_to_false`（语法树）、
`test_non_writer_is_rejected_fail_closed`、`test_refresh_write_paths_declare_writable`。

---

## P5 拥挤度数据没有任何调度更新它

### 实测证据

```
sector_crowding_daily  最新 trade_date = 20260924    而仓库里最新交易日 = 20260929
最后写入 = 2026-09-27T18:52
调度作业 = 只有 crowding_metrics_weekly（写 sector_crowding_metric）
```

⇒ 「一键刷新」**只能靠人手动点**，没人点就一直是旧的。

### 处理

用户要求「**不依赖服务是否启动**」⇒ OS 级 Windows 计划任务 `MossCrowdingRefresh`：

| 项 | 取值 |
|---|---|
| 触发 | 周二 11:00 + 周六 11:00 |
| 身份 | `SYSTEM` / `RunLevel Highest` |
| 错过补跑 | `StartWhenAvailable = True` |
| 口径 | `pool_only=True`（与前端按钮同口径） |
| 环境 | `--env dev`（`crowding_shared.writer`） |

**端到端实跑（不是"应该能跑"）**：全量回填 **262/262、失败 0、417.1 s**；
紧接着增量 **262/262、失败 0、52.5 s**；数据 `20260924` → **`20260929`**，
行数 2,181,780 → **2,182,304**。

### 已知缺口

**`StartWhenAvailable` 只保证"开机后补跑"，不保证补跑落在闲时**；
两次都在 11:00，若周二/周六 11:00 都关机则整周顺延。

---

## P6「一键刷新」的调用次数没有预算上限

### 需求原话

> 「拥挤度会花费 tokens 和搜索用量，设置**一周最大调用 2 次**」

### ★ 一处口径更正（实测）

拥挤度刷新**完全不调用 LLM** —— `src/sector_crowding/` 下
`llm|openai|deepseek|embedding` **零命中**，`crowding_metrics_weekly` 同样。
**它不花 token。** 真实成本是 **Tushare 外部配额**：

```
sources.py:149  ths_index    （板块列表，1 次）
sources.py:233  ths_daily    （每板块 1 次 ← 大头）
sources.py:274  ths_member   （成分股，按需）
```

### 处理：把上限做成硬约束（不是注释）

`AGENTS.md`：「**上限必须写进代码，不能留在注释里**」⇒ 落在**注册入口**：

```python
# scripts/setup_crowding_refresh_task.py
MAX_RUNS_PER_WEEK = 2
def weekly_run_count(triggers=TRIGGERS) -> int:
    return sum(7 if dow == "EveryDay" else 1 for dow, _h, _m in triggers)
```

`--register` 先跑 `_enforce_weekly_budget()` / `_enforce_blocked_window()`，
**超限直接拒绝注册**。**自证**：默认 2 次 ✅；加 `EveryDay` → 9 次 ❌；
两个 `EveryDay` → 14 次 ❌；触发放 09:30 ❌。

★ **这一条直接否掉了用户自己提的"每天 21:00"**（`EveryDay` 折算周频 = 7 次，
违反他刚定的 2 次上限）。

### 已知缺口

**手动刷新与自动任务没有跨进程互斥**；**定时触发本身尚未验证**（要等 2026-10-03）。

---

## P7 隧道劣化被误认为"带宽不够"（归因错误）

### 用户的问题

> 「腾讯云 **峰值带宽 20Mbps 升级套餐到 30Mbps**，会改善这种情况吗？
>   为什么会出现隧道劣化，和套餐带宽有关吗？」

### 实测证据（算术上就不成立）

| 项 | 数值 |
|---|---|
| 20 Mbps 理论峰值 | 2,441 KB/s |
| 30 Mbps 理论峰值 | 3,662 KB/s（**提升 1.5×**） |
| 项目实测（好时段） | **51 KB/s** = **0.42 Mbps = 套餐的 2.1%** |
| 项目实测（劣化） | **4.6 KB/s** = 套餐的 **0.19%** |

**反证**：真跑满 20 Mbps，本轮那 36 KB 响应只要 **0.015 秒** —— 用户不可能感觉到慢。

### 日志证据：是"连接失效"不是"带宽饱和"

链路 **5 跳**：`浏览器 → VPS nginx → frps:7000 → 【SSH 隧道】→ frpc → 8110`。

```
frp_tunnel.log：SSH 被远端强制关闭 4 次 / 会话失效 4 次 / 重连 4 次
                / 「端口 17000 已被占用」11 次（重连自己打架）
frpc.log      ：3 次 connection write timeout + 1 次 dial ... i/o timeout
tunnel_incidents.jsonl（09-27 16:02）：四次探测全部 14~15 秒超时
```

**几百字节的请求等 15 秒** —— 带宽不足只会"慢"，不会"15 秒零字节"。

### 机制（三条，都与套餐带宽无关）

1. **TCP-over-TCP 塌陷**：丢包时内外层重传/拥塞控制互相打架，吞吐掉 1~2 个数量级
   （正是"有时 51、有时 4.6 KB/s"的特征）；
2. **单连接无多路复用池**（`frpc.toml` 单 `localPort`）：断一根，用户看到的是
   "后端不可达"而不是"慢"；
3. **本机资源紧张**：incident 记录 `mem_free_gb 2.59` / `commit_pct 77~83%`。

### 处理：结论 + 建议顺序

**升级到 30 Mbps 基本不会改善。** 建议顺序：
先减字节（P1/P2 已落地）→ **修 SSH 隧道稳定性**（keepalive / 退避重连 / 消端口打架）
→ **建立带宽持续测量** → 考虑 VPS 直连绕掉最不稳的一跳 → CF 备用路仅应急。

### 已知缺口

**未做 VPS 侧分时段压测 ⇒ "劣化到底在哪一跳"尚未定位到跳**；
升级收益**未做 A/B 实测**（结论是算术 + 日志推断）。

---

## P8 本轮**我自己**造成的失误（9 处，全部已修）

> 这一节是本文件最重要的部分 —— 它们不是产品缺陷，是**过程缺陷**，
> 而且多数**在犯的时候不觉得错**。逐条给"下次怎么避免"。

| # | 失误 | 为什么当时觉得对 | 后果 | 下次怎么避免 |
|---|---|---|---|---|
| 1 | 字面实现"3 位小数" | 用户原话就是"小数点后 3 位" | ⚠️ **会静默销毁小量级数据**（96 个取值→1 个，曲线变直线）。**实测才发现** | 实现前先做**自洽性检验**：把口径套到真实数据的一个样本上，看会不会归零/饱和/溢出。**字面执行但破坏数据 = 没理解需求** |
| 2 | 把"每周两次"与"21:00 每日"**同时**装进配置 | 两条都是用户说的（不同轮次） | ⚠️ 一周变 **9 次**，直接违反"最多 2 次" | 多轮需求要**回归所有已定约束**：新加一条时问"它和上一轮的上限冲突吗"。**频率是导出量，不是独立参数** |
| 3 | 先断言"SQLite REAL 8 字节 ⇒ 取整一字节不省" | 教科书结论，且单值实测确实都是 10 字节 | ⚠️ **与页占用实测矛盾**（省 8.7%）。真机制是"整数值浮点走变长整数 serial type" | **测量冲突时，先找第三种解释，不要挑一边信。**页占用是 ground truth，单值宽度不是 |
| 4 | 用 `*>&1` 收 stdout+stderr | 想"失败原因必须留住" | 日志混进 stderr 横幅噪声（3 行无关内容 + PowerShell 包装） | 台账要**结论**；诊断信息**只在失败时**留。分开 `2>` 到临时文件 |
| 5 | 计划任务用 07:30 | 我按"闲时"推导 | ⚠️ **电脑那时候没开机** | **执行前提也是需求**："闲时"要问清是"数据源闲"还是"**机器活着**" |
| 6 | 测试只改 `database.path`，`config_db_path` 解析到**真实应用库** | 沿用既有 fixture 形状 | ⚠️ 单测**读到真实数据**（隔离泄漏）或 `no such table` | 改共享函数时，**把它的每个调用方的真实取值打印出来**；不完整的 mock 比没有 mock 更危险 |
| 7 | 把 `start_refresh_all` 的**说明文字**当调用判据（子串断言） | 想快速断言"没调后台线程版" | ⚠️ 判据误报（我自己的 docstring 里正提到它） | 断言**代码事实**要用 AST，不要搜字符串。**判据要适应代码，不要让代码迁就判据** |
| 8 | 沿用"跟随 `settings.sqlite_path`"的范式 | 既有代码就这么写的 | ⚠️ 该库**没有参考表** ⇒ `no such table`。既有代码是**既有缺陷**，不是范式 | **不要盲从既有写法**：先确认它当前是否真的成立（那份代码自己就 failed） |
| 9 | `manage.py stop --env dev --port 8100` 把 **pilot 也停了** | 我以为 `--env/--port` 会限定范围 | pilot 短暂不可用 | 读 docstring：`stop`/`--replace` 按**命令行**枚举本项目**全部**后端进程。⚠️ **测试环境停服前先确认它的真实影响面** |

### 另有一条"差点漏掉"的（值得单列）

**`test_shipped_deps` 的盲区**：我新加的两个脚本被 `.gitignore` 忽略，
而我的测试 `read_text()` 它们 —— 判据**16 passed 全绿**，
但那份测试在克隆仓库里会**文件不存在直接 AssertionError**。
该护栏只识别 `python scripts/x.py` 这种**调用式**引用，识别不了 `read_text()`。

**处理**：两个脚本加 `.gitignore` 白名单并 `git add`（与既有 4 个计划任务包装同类且同样入库），
理由写进 `.gitignore` 注释。
**教训**：**"我没有触发某个护栏" ≠ "我没有这类问题"。** 护栏的盲区要用一次人工核对去补。

---

## P9 腾讯云备用入口长期不通（未修，登记）

### 实测证据

```
moss.wujiaitool.cn/api/v1/health/live  → Cloudflare 530
VPS 43.128.5.94:80（直连 + Host 头）    → 530（**说明请求到了 CF，不是 nginx**）
VPS 本机 curl localhost:80             → 301（nginx 正常）
VPS 本机 curl localhost:18110          → 200（frps → 后端正常）
VPS cloudflared 服务                    → inactive
tunnel-watchdog.log 09:37              → 「已重启 Cloudflared（**原状态 Stopped**）；复检备用 仍不通」
```

### 结论与处置

**备用入口本来就是坏的，不是本轮改坏的** —— 09:37 的日志已经记着它原状态是 `Stopped`。
**主用入口 `hk.wujiaitool.cn` 全程 200**，客户不受影响。
修复需要 CF 隧道凭据，**没有擅自动**。⇒ **登记为已知缺口**。

---

## 附：本轮建立的判据清单（防复发）

| 文件 | 条数 | 守什么 |
|---|---|---|
| `tests/integration/test_crowding_detail_payload_size.py` | 12 | 拥挤度详情体积（含两条反向断言） |
| `tests/unit/test_crowding_slim_projection.py` | 11 | 瘦身投影的列契约与精度 |
| `tests/unit/test_float_precision.py` | 30 | 3 位有效数字口径（含小量级不许归零） |
| `tests/unit/test_crowding_db_routing.py` | 18 | 库归属（含跨库 JOIN/写的真实 SQL 验收） |
| `tests/unit/test_crowding_refresh_schedule.py` | 14 | 定时刷新（含成本上限与交易时段两条反向断言） |
| `tests/unit/test_ps1_encoding.py` | 26 | `.ps1` 的 BOM + 可解析（既有护栏，本轮复用） |
| `tests/unit/test_shipped_deps.py` | 16 | 白名单/交付完整性（既有护栏，本轮**补了它的盲区**） |

**相关判据合计 291 passed**（最后一次全跑）。

---

## 附：本轮的口径落账

| CHG | PRD 章节 | 内容 |
|---|---|---|
| `CHG-0137` | §22 | 载荷体积与隧道带宽硬约束 + 拥挤度详情瘦身 |
| `CHG-0142` | §24 / §25 | 浮点 3 位有效数字 + 带宽升级结论 |
| `CHG-0143` | §26 | 拥挤度库归属（参考数据共享 / 用户配置隔离） |
| `CHG-0144` | §27 | 拥挤度自动定时刷新（OS 计划任务 + 成本上限） |

台账门禁 `prd_sync_check.py --ledger` 与 `--self-test` 均 0 ERROR。
