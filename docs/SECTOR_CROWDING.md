# 板块概念拥挤度（Sector Crowding）

> 前端位置：**资金流监控** tab → 第三个页签 **「板块拥挤度」**
> 一键刷新：页签顶部「一键刷新全部板块拥挤度」
> 代码：`src/sector_crowding/`（config / db / sources / refresh）+ `src/api/routes/sector_crowding.py`
> 配置：`sector_crowding/config.yaml`
> 日志：`logs/sector_crowding.log`

---

## 一、指标定义

```
板块每日成交额   = 同花顺板块指数当日成交额（ths_daily 的 vol × avg_price）
全市场每日成交额 = 全部 A 股当日成交额之和（本地仓库 quant_daily，只统计 amount > 0）
原始拥挤度       = 板块每日成交额 / 全市场每日成交额
平滑拥挤度       = 原始拥挤度的 MA5（ma_window 可配置）
拥挤度水位       = 平滑拥挤度 / 近 6 年平滑拥挤度最大值 × 100%
```

- **分母是"该板块自身"近 6 年的平滑拥挤度最大值**，不是全市场、也不是跨板块 ——
  「这个板块自己历史上最拥挤的时候」才是水位 100% 的定义。
- **本轮口径统一为近 6 年**（上一轮是 5 年）；改 `window.max_lookback_years` 即可。
- 分母为 0、或有效样本不足 60 根 → **水位 = NULL**，前端显示"数据不足"。
  把 NULL 当 0 会漏告警、当 100 会误告警，两者都错。

### 为什么水位比拥挤度绝对值更有用

拥挤度的绝对值没有跨板块可比性（大盘股板块天然成交额高）。水位是相对该板块
**自己**历史的位置，因此"87%"在任何一个板块上都读得通：它现在处于自己历史上
第 87 分位的拥挤状态。

---

## 二、数据来源（实测结论）

| 内容 | 来源 | 实测 |
|---|---|---|
| 板块列表 | Tushare `ths_index`（7 个 type）+ 本地 `dim_concept` | 并集 **2517** 个板块（其中概念 2337） |
| 板块成交额 | Tushare `ths_daily`：`vol × avg_price` | 0.2s/板块/全历史 |
| 全市场成交额 | 本地仓库 `quant_daily.amount` 全A求和 | 近 6 年 1454 个交易日，3.5s（可缓存） |
| 板块成分股（参考） | Tushare `ths_member` | 只给**当前**快照，落 `sector_member` 表 |

### 为什么板块成交额不用"成分股成交额求和"

任务书写的是"板块成交额 = 成分股当日成交额之和"。两条路实测都通了，选了后者：

- **成分股求和**（`ths_member` + 仓库 `quant_daily`）：可用，但 `ths_member`
  只返回**当前**成分股。用它回算 6 年历史会有**前视偏差** —— 今天的成分不等于
  2020 年的成分，新纳入的票会把它的历史成交额算进这个板块的过去。
- **板块指数日线**（`ths_daily`）：`vol × avg_price` 就是成交额，
  与成分股求和同量级，且完全不受成分变更影响。

### 本机网络下不可用的源（已实测排除）

| 候选 | 结果 |
|---|---|
| `ak.stock_board_concept_cons_em`（东财概念成分） | ❌ `ConnectionError`（TLS-SNI 阻断，`MOSS_EM_DIRECT=1` 也无效） |
| `ak.stock_board_concept_name_em`（东财概念列表） | ❌ 同上 |
| `ak.stock_board_concept_cons_ths`（同花顺概念成分） | ❌ 该版本 akshare（1.18.94）**没有这个函数** |
| Tushare `ths_member` | ✅ 可用（最终采用） |

---

## 三、SQL 存储

建在**主库** `data/moss_finagent.db`（与做T/量化选股同库，便于事务与复用连接范式）。

### `sector_crowding_daily`（每日每板块一行）

| 列 | 说明 |
|---|---|
| `trade_date` / `sector_code` | 联合唯一 `UNIQUE(trade_date, sector_code)` |
| `sector_name` | 板块名（冗余存，便于直接查） |
| `sector_amount` / `market_amount` | 板块、全市场成交额（元） |
| `raw_crowding` | 原始拥挤度 |
| `ma5_crowding` | 平滑拥挤度（MA5） |
| `water_level` | 水位（0~1+）；NULL = 数据不足 |
| `created_at` / `updated_at` | UPSERT 时 `created_at` 保持不变 |

### `sector_meta`（板块元数据 + 增量水位线）

| 列 | 说明 |
|---|---|
| `sector_code` | 主键 |
| `sector_name` / `board_type` / `is_concept` | 名称、同花顺 type、是否概念板块 |
| `first_trade_date` / `last_update_date` | **`last_update_date` 就是增量刷新的水位线** |
| `bars` | 已入库行数 |

### `sector_member` / `sector_crowding_watch`

成分股（参考数据，不参与计算）、自选池。

---

## 四、增量刷新逻辑

```
手动点「一键刷新」→ POST /refresh_all → 立即返回 task_id → 后台线程执行
  ├─ 每个板块：读 sector_meta.last_update_date
  │    ├─ 为空   → 从近 6 年起始日**全量回填**
  │    └─ 非空   → 从 last_update_date 的**下一个交易日**拉到最新交易日
  ├─ 按 UNIQUE(trade_date, sector_code) UPSERT（幂等，重复触发不产生重复行、不报错）
  ├─ **成功才推进** last_update_date
  └─ 单板块失败只记日志 + 计入 failed_sectors，不影响其它板块
```

### 三个必须守住的正确性要点

1. **水位必须用完整历史算，不能只用本次增量窗口**
   增量只拉"上次之后"的几天。若只用这几天算 MA5 与 6 年最大值，MA5 会因缺前 4 天
   失真、6 年最大值退化成"这几天里的最大" → 水位虚高、天天告警。
   所以每次刷新都从库里**重读该板块完整历史**再算。
2. **滚动窗口无未来函数**
   第 i 天的 MA5 只用 i-4..i；第 i 天的"6 年最大值"只用到 i 为止（`expanding`）。
   否则历史水位会被后来的高点压低，回看曲线时"当时到底警没警"就失真了。
3. **水位线只在成功时推进**
   拉取成功但写入失败、或中途异常早退时不能推进 —— 否则下次从错误日期开始，
   那段日期成为**永久空洞**。

---

## 五、API

前缀 `/api/v1/sector_crowding`（任务书写的是 `/api/sector_crowding`，
项目路由统一挂 `/api/v1`，这里从项目约定）。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/refresh_all` | 一键刷新（立即返回 task_id） |
| GET | `/refresh_status?task_id=` | 刷新进度（留空 → 最近一次任务） |
| GET | `/latest?concepts_only=` | 全板块最新水位（散点总览） |
| GET | `/alerts?threshold=0.8&concepts_only=true` | 水位 ≥ 阈值的板块（降序） |
| GET | `/{sector_code}?limit=` | 单板块历史曲线（含水位） |
| GET | `/sectors?keyword=` | 板块搜索 |
| GET | `/members/{sector_code}` | 板块成分股（参考） |
| GET/POST | `/watchlist` | 自选池读 / 加 |
| DELETE | `/watchlist/{sector_code}` | 自选池移除 |
| GET | `` (空) | 模块自检（表行数/上次刷新） |

`refresh_all` 的实现是**立即返回 + 后台线程 + 前端轮询**：一轮全量回填要几分钟，
同步等待必然撞上反代/浏览器超时（项目里已踩过 300 秒超时的坑）。

---

## 六、前端页签结构

「板块拥挤度」排在「板块资金流」「个股资金流」之后。自上而下：

1. **一键刷新栏**（`SectorCrowdingRefreshBar`）：刷新按钮 + 进度条 + 最后更新日期；
   刷新中按钮置灰，完成后自动刷新图表与告警面板。
2. **高拥挤度告警面板**（`SectorCrowdingAlertPanel`）：水位 ≥ 80%；
   ≥ 90% 红色、80~90% 橙色；按水位降序（可切名称排序）；
   支持复制列表 / 全部加入自选池 / 导出 CSV / 逐条查看详情。
3. **全板块水位总览**（`SectorCrowdingOverview`）：散点图
   （X = 成交额、Y = 水位），滚轮缩放、悬停读数、点圆点看详情，
   画 80% / 90% 参考线。右上"又大又挤"最该警惕、左上"小但极挤"可能是新热点。
4. **单板块详情**（`SectorCrowdingDetail`）：双面板共享时间轴 ——
   上=水位（含告警线/高危线）、下=成交额占全市场比例（细线原始、粗线 MA5）。
5. **自选池管理**（`SectorCrowdingWatchlist`）：搜索加入 / 移出 / 显示各自最新水位。

### 页签宿主为什么要拆组件

`FundFlowPanel` 原本自己管页签、自己抓资金流数据、并且**每 60 秒轮询一次**。
若在同一个组件里加第三个页签，切到「板块拥挤度」时那套轮询照样在跑
（白耗数据源与 CPU）。因此把资金流主体整段搬进 `FundFlowBoard`（**其内部逻辑
一行未改**），由 `FundFlowPanel` 只保留页签状态并**按页签挂载**对应组件。

---

## 七、改配置后如何让新口径生效

三个参数影响水位（`ma_window` / `max_lookback_years` / `min_bars_for_water_level`）。
改完**不需要重新抓 2517 个板块**（那要十分钟且白耗接口配额）——板块成交额与
全市场成交额都已在库里：

```bash
# 方式一：接口（不联网，实测 82 秒重算 217 万行）
curl -X POST http://127.0.0.1:8100/api/v1/sector_crowding/recompute

# 方式二：Python
python -c "from src.sector_crowding import refresh; print(refresh.recompute_stored_water_levels())"
```

概念判定规则（`non_concept_name_patterns` / `non_concept_code_prefixes` /
`NON_CONCEPT_BOARD_TYPES`）改了之后重跑分类（也是秒级、不联网）：

```bash
python -c "from src.sector_crowding import refresh; print(refresh.reclassify_boards())"
```

---

## 八、已知限制（不粉饰）

1. **成分股用当前快照**：`ths_member` 只给当前成分，`sector_member` 表因此是
   "此刻的成分"。它**不参与**拥挤度计算（计算走板块指数日线），所以不影响水位；
   但前端展示"这个板块有哪些票"时看到的是当前成分，不是历史某天的成分。
2. **不是所有板块都有 6 年历史**：新概念（2023 年后发布的）自然只有上市以来的
   数据。实测同一批板块里既有 1472 根的也有 638 根的 —— 这是真实覆盖差异，
   不是抓漏（已用"分片请求 vs 单次全区间请求"逐一对账，结果完全一致）。
3. **`min_bars_for_water_level` 为什么是 750 而不是 60**
   水位 = 当前平滑拥挤度 / **近 6 年**最高值。对只有半年历史的新板块，它自己的
   "历史最高"就是最近这几天 —— 水位**恒为 100%**，一上线就误告警。
   实测首次全量回填后：`min_bars=60` 时有 218 个板块 ≥80%（其中 101 个 ≥90%），
   绝大多数是这种"历史太短所以永远是自己最高"的板块；提到 750（≈3 个交易年）后
   降为 26 个，且都能对上真实题材（PCB概念 / 培育钻石 / 超导概念 / CPO / PET铜箔…）。
   代价：2023 年后发布的新概念在被回填满 3 年前不出水位（如实显示"数据不足"）。
4. **概念/行业边界不是非黑即白**：同花顺的 `type` 里，`I`（行业/777 个）、
   `R`（地区）、`S`（统计口径）、`BB`（全A口径）、`ST`（风格）、`TH`（自建组合）
   都已排除；但 `type='N'` 是"指数族"，里面既有沪深300样本股（已靠名称挡掉）
   也有窄行业段如「疫苗」「半导体产品与设备」—— 后者被算作概念。
   实测这类边界项数量很少，且它们对"拥挤度"是有信息量的（疫苗/半导体设备确实是
   题材炒作的对象）。真要严格区分需要额外的行业名录映射，本轮未做。
5. **`ths_daily` 单次返回有上限**（实测无日期参数时约 1900~3000 行）。
   代码按 `fetch_chunk_years: 3` 切片请求以规避。
6. **全市场成交额只统计 `amount > 0`** 的行：仓库里有少量 amount 为空/0 的记录，
   当 0 加进去会**低估分母 → 高估拥挤度 → 正常板块也被顶成告警**。
7. **板块口径是"同花顺板块指数"**，不是"成分股求和"。两者同量级但不等价。
   想要成分股求和口径需改 `sources.fetch_board_daily`（但会引入限制 1 的前视偏差）。
8. **"有数据"与"有水位"的板块数不同**：`/latest` 返回的是**最新交易日**有行的板块
   （实测 666 个概念板块），而 `sector_meta` 里的概念板块总数更多（933 个）——
   差额是上市晚/已停更、在最新交易日没有行的板块。前端文案写的是
   "N 个板块有水位"，不是"N 个概念板块"。
9. **不做定时自动刷新**（本轮明确要求）：只有前端一键手动触发。
10. **不做消息推送**：告警只在前端面板展示，不接邮件/微信/钉钉。
