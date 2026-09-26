# 面试讲解手册（二）：数据新鲜度 / 调度自愈 / 面板交互（2026-09-23 会话沉淀）

> **这份文档是什么**
> `docs/INTERVIEW_PLAYBOOK.md` 覆盖的是**选股与交易主链路**（六式语料、11 维打分、三档模型、回测）。
> 本文覆盖同一天的**第二个会话**：数据新鲜度与同步自愈、情绪周期五线图的成型、主线挖掘的面板日期、
> 做T/日K 的取数与交互。两边**不要重复讲**——面试里挑一条主线讲深，比把两份都念一遍强。
>
> **怎么用**
> 1. 第 0 节是本次会话版的 30 秒 / 3 分钟讲法，可直接当开场。
> 2. 第 1 节是**交付清单**（模块 → 交付 → 硬指标 → 代码位置 → 测试），可以照着写简历条目。
> 3. 第 3 节是**踩坑集**，每条都带代码位置与钉住它的测试文件——面试官追问"你怎么保证不再犯"时用它。
> 4. 第 4 节按岗位分题库（量化开发 / AI 应用开发 / AI 应用架构师）。
> 5. **第 6 节务必看**：哪些话不能说满。
>
> **⚠️ 文件可见性约定**：标 🔒 的是**本地私有资产**，仓库做开源裁剪时被 `.gitignore` 排除
> （见 `.gitignore` 第 49/85/87/88/90/100/114 行）。面试时可以说"这块在私有目录里、
> 开源版被裁掉了"，**不要**说"你 clone 下来就能看到"。

---

## 0. 两套讲法

### 0.1 三十秒版（电梯陈述）

> "这次我做的是**投研系统的数据底座与面板**这条线。系统本身是多 Agent 投研工作台
> （FastAPI + SQLite + React，15 GB 本地行情仓），我这次解决的是三件事：
> **第一，让'今天的数据到底到没到'变成可判定的**——交易日历缓存、分区、仓库三层水位互相比较，
> 判不出来就说判不出来，绝不声称'已同步'；
> **第二，让同步作业自愈**——收盘后每 30 分钟一轮，缺什么补什么，
> 而且要能识别'上游是逐板块陆续发布'这种假新鲜；
> **第三，把每条结论都钉上测试**——本次回归面 204 个用例，新增的 19 个专门钉住上面这些坑。"

### 0.2 三分钟版（按"问题—判断—动作—结果"讲）

| 段落 | 讲什么 | 落点数字 |
|---|---|---|
| 问题 | 用户报"9/23 都收盘了，情绪周期图还停在 9/22"；同一天主线挖掘也停在 9/22，刷新还报 `Failed to fetch` | 两次报障，两块面板 |
| 判断一 | 这不是画图/打分的问题，是**本地仓库没有那一天的数据**；而"应该有哪一天"这件事被一个**陈旧的交易日历缓存**锁死了 | `trade_cal` 缓存末 20260922，作业 17:30/18:00/18:30 三次 `success + 0 行 + 54s` |
| 判断二 | 更麻烦的是**它自己解不开**：`calendar(start,end)` 只在 `end > 缓存末日` 时才联网，而这个 `end` 正是缓存算出来的 → 循环依赖 | 上游其实早有数据（`daily(trade_date=20260923)` 返回 5556 行） |
| 动作一 | 用**墙上时钟**顶一次日历打破环；顺手修掉 `index_daily` 走错下载入口把整轮打断在灌库之前 | 8 档数据集全部推到 20260923 |
| 动作二 | 给"逐板块发布当天指数"加**校验 + 有上限重试 + 如实降级 `partial`**；打分侧补一条 gap 明说"N 个板块当日无数据、分数沿用上一交易日" | 66/138 → 138/138 |
| 结果 | 情绪周期图末点 = 20260923（小周期 4 / 大周期 6 / 大肉 50 / 大面 15 / 连板数 12）；主线快照 `trade_date=20260923 已收盘`，138 板块、候选 28、精选 10、告警 9~10 | 见第 5 节 |
| 收尾 | 我在这一轮里最看重的是**"不猜"**：判不了就 `unknown`，缺数据就说缺哪一档，宁可让用户看到 `partial`，也不让他以为数据是齐的 | — |

---

## 1. 交付清单（可直接当简历条目）

| 模块 | 交付 | 硬指标 | 代码位置 | 测试 |
|---|---|---|---|---|
| **同步作业自愈** | 交易日历视野自愈 + `index_daily` 分流 + 逐档隔离 | 8 档日频数据集推到最新；单档异常不再中断整轮 | `src/scheduler/jobs.py::_refresh_calendar_horizon` / `_download_one` / `_quant_data_sync` | `tests/unit/test_quant_freshness.py`（+5 用例） |
| **主线面板日期** | 面板日期 = 数据水位 → 修到 09-23；刷新报错定位与容错 | 快照 `trade_date=20260923`、138 板块、gaps 为空 | `src/mainline/datastore.py::sync_board_bars`(2b 段)、`board_codes_with_bar_on`；`src/mainline/service.py::_compute` | `tests/unit/test_mainline_board_bar_freshness.py`（6）、`tests/unit/test_mainline_daily_job.py`（7） |
| **情绪周期五线图** 🔒 | 大肉/大面/连板数/小周期/大周期（仅主板）；逐点标值 + 防压字；图例点选隐藏；左轴固定下限 200 | 60 交易日 × ~5200 只（**31 万行**）≈1.6s，五线 2.5~4s，缓存 30 分钟 | `src/auction_select/sentiment_cycle.py::build_cycle` / `_trim`；`web/src/components/AuctionSelectPanel.tsx`（`CYCLE_LINES` / `CYCLE_COUNT_FLOOR`） | `tests/unit/test_sentiment_cycle.py`（8） |
| **日K 取数提速** | 当天未收盘 bar 补齐 + 陈旧一跳跳过 + 前端 SWR 不清屏 | 取数 **10s → 0.16~0.23s**，面板切换 99ms~2s | `src/infrastructure/connectors/tencent_daily_connector.py::_wants_forming_bar`；`src/infrastructure/connectors/router.py`（`_STALE_SKIP_SECONDS=90`）；`web/src/components/IntradayDailyPanel.tsx` | `tests/unit/test_tencent_daily_connector.py`、`test_connector_router.py`、`test_daily_freshness.py` |
| **日K 交互** 🔒 | 十字光标（日期/价/涨跌幅/高低/量/换手/资金）+ 拖拽区间测量 | 区间统计 12~31ms（一次 SQL） | `web/src/components/NiuLineChart.tsx`（`.niuline-hover` / `.niuline-measure`） | 前端手工 + headless 几何核对 |
| **竞价口径** 🔒 | 竞价量比阈值 10%→**13%**；抢跑否决理由带上真实数值；落选区只显示被否决票 | `0.964333`（命中②档）、000910 由"疑似 bug"变可解释 | `src/auction_select/scoring.py::_concrete_rush_veto`、`src/auction_select/features.py`（`RUSH_OUT_VOLUME_PCT`） | `tests/unit/test_auction_rulebook.py::test_*rush*` 🔒 |
| **面板交互** | 市场环境条加高 + 全文 +2 号；徽标文字收敛白+蓝；删「不做T降本」；「日内分时」→「分时」；页签右侧风险提示 | 旧文案在产物中出现 **0 次** | `web/src/components/MarketContextStrip.tsx`、`IntradayTPanel.tsx`、`QuantTabContainer.tsx`、`web/src/styles.css` | 产物字符串核对（`npm run build` + 检索） |

---

## 2. 数据底座拆解（面试里最容易被低估的一块）

### 2.1 三层水位：谁该跟谁比

| 层 | 是什么 | 怎么看 |
|---|---|---|
| **分区** | `data/quant/tushare/a_share/<dataset>/<date>.parquet` | 数据**已下载** |
| **仓库** | `data/quant/warehouse.db` 各表 `MAX(trade_date)` | 数据**可被查询**（15 GB SQLite） |
| **应该有** | `freshness.latest_complete_trade_date()` | 有**发布线**：16:30（`EOD_RELEASE`）之后今天的 EOD 才算"应该有" |

判定四态（`src/quant/sync_gap.py::build_sync_gap`）：`ok / need_ingest / need_download / unknown`，
而 **`unknown` 不算已同步** —— "没发现落后"与"确认同步"必须分开表达。

> 面试可直接说：**"我把'是不是最新'从一个数字变成了一个判定函数，判定不出来时返回 unknown 而不是 ok。"**

### 2.2 调度：为什么用进程内 Cron，怎么防止它变成"假成功"

- 注册表 `src/scheduler/registry.py`（cron 只在这一处声明）；执行器 `src/scheduler/jobs.py`；
  运行记录 JSONL `src/scheduler/run_log.py`。
- 关键作业：`quant_data_sync`（`*/30 16-23 * * 1-5`，收盘后每 30 分钟补数）、
  `mainline_daily`（`30 17 * * 1-5`）、`mainline_etf_flow`（`50 17 * * 1-5`）。
- **连续失败自动暂停**：`PAUSE_AFTER_CONSECUTIVE_FAILURES = 3`；启动自检触发的补偿（`trigger="startup"`）
  不受暂停拦截（重启本身可能已消除失败原因）。
- **"成功"的指纹**：`success + rows=0 + 50 秒` = 判定成"已是最新"只跑了扫描；
  真有缺口时 duration 是分钟级。**这条判断标准是本次排查里最快的一把尺子。**

### 2.3 前端与后端的契约（两处硬道理）

1. **后端不返回"假 0"**：缺数据给 `null` 或空数组 + `gaps`，前端不拿 0 兜底
   （"没数据"和"真的是 0"是两件事）。
2. **长任务一律"提交 + 轮询"**：`POST /refresh` 返回 `task_id`，前端轮询 `/refresh_status`；
   任务表在**进程内存**里 → 重启即失联，所以前端必须容错（见坑 5）。

---

## 3. 踩坑集（现象 → 根因 → 修法 → 防复发 → 代码/测试）

> 讲法统一：**现象 → 根因（一句话）→ 修法 → 防复发（测试/文档/显式上报）**。
> 能被追问的坑，都是"它不会报错，只会让结果悄悄变错"这一类。

### A. 数据新鲜度（头号事故类型）

**坑 1｜交易日历缓存把"今天"锁死（循环依赖）**
- 现象：9/23 收盘后情绪周期图末点仍是 9/22；`quant_data_sync` 17:30/18:00/18:30 三次
  `success / rows=0 / 52~54s`。
- 根因：`latest_complete_trade_date()` 只读**本地 `trade_cal` 缓存**（刻意不联网），缓存末 20260922
  → 目标日恒等于 09-22 → 与水位相等 → 走「已是最新」分支。而 `TushareDownloader.calendar()`
  只在 `end > 缓存末日` 时才联网重拉，这个 `end` 又来自缓存 → **它自己解不开**。
- 修法：`_refresh_calendar_horizon()` 用**墙上时钟**顶一次日历；失败/空表只记一句说明、按旧日历走。
- 防复发：三条用例钉住——落后必刷新、已最新则一次网络都不发、刷新失败不改判定。
- 代码：`src/scheduler/jobs.py::_refresh_calendar_horizon`、`src/quant/freshness.py::latest_complete_trade_date`、`src/quant/download.py::TushareDownloader.calendar`
- 测试：`tests/unit/test_quant_freshness.py::test_sync_refreshes_a_stale_calendar_cache_before_deciding` / `test_sync_skips_calendar_refresh_when_cache_is_current` / `test_sync_calendar_refresh_failure_keeps_old_calendar`
- 教训：**"应该有的日期"不能从自己的缓存里推**，判定链至少要有一个外部时钟锚点。

**坑 2｜一个走错入口的数据集，把整轮同步打断在灌库之前**
- 现象：`失败：下载 20260922~20260923 失败 KeyError: 'index_daily'`；5 档分区已下好，仓库一行没进。
- 根因：作业清单按"面板会读的 8 档"列，但 `index_daily` **不在**下载器的日频表里
  （指数是 5 个代码单独拉的，入口是 `sync_index`）；异常抛在 `_ingest_gaps()` 之前。
- 修法：`_download_one()` 按数据集分流 + **逐档隔离**（单档失败记名继续，全部失败才判 failed）。
- 代码：`src/scheduler/jobs.py::_download_one`、`_quant_data_sync`
- 测试：`tests/unit/test_quant_freshness.py::test_sync_routes_index_daily_to_the_index_entry_point` / `test_sync_one_broken_dataset_does_not_block_the_rest`
- 教训：**批处理里"一档失败 = 全轮失败"是设计缺陷**，不是严谨。

**坑 3｜"返回非空 = 成功"掩盖了逐板块发布（66/138）**
- 现象：`sync_board_bars` 报 `ok / missing=[]`，但 09-23 只有 **66/138** 个板块有数据；12 分钟后重跑 **138/138**。
- 根因：`ths_daily` 对每个板块都返回**非空历史**，新鲜度判断全部通过；而上游对**当天**那根 K 线是
  **逐代码先后发布**的。抽 8 个"本地缺当天"的板块直接问上游：**8/8 都能取到**（数据不是没有，是十分钟前还没轮到）。
- 后果：缺当天的 72 个板块**拿上一交易日 K 线**参与当天打分 —— 分数照出、排名照排、界面无异常。
- 修法：按 `end` **校验 + 有上限重试（3 轮 × 20s）**；仍缺的降为 `partial`，`message` 写明
  「N 个板块 {日期} 当日指数上游未发布」；打分侧再补一条 gap 明说"分数沿用上一交易日"。
- 代码：`src/mainline/datastore.py::sync_board_bars`（2b 段）、`board_codes_with_bar_on`、`_BAR_DATE_RETRY_ROUNDS`；`src/mainline/service.py::_compute`
- 测试：`tests/unit/test_mainline_board_bar_freshness.py`（6 个：缺当天必报 partial / 第二轮发布的要补进来 / 空表与"当天未发布"要区分 / 精确到天的判据）
- 教训：**"接口没报错"不等于"数据齐了"**；凡按某一天打分，都要显式校验那一天。

**坑 4｜`MOSS_SQLITE_PATH` 串台，把行情仓库指到了 4 行的 dev 库**
- 现象：自选/补全查不到票（API 读的是隔离库里的 4 行字典），而行情仓有 5566 行。
- 根因：应用库隔离用的 `MOSS_SQLITE_PATH` 被行情仓库的 `from_env` 也读了。
- 修法：仓库**只认** `MOSS_QUANT_SQLITE`。
- 代码：`src/quant/warehouse.py::from_env`
- 测试：`tests/unit/test_quant_warehouse.py`（环境变量边界用例）
- 教训：**同名环境变量被两个子系统读，是最隐蔽的"环境串台"**。

### B. 调度与进程生命周期

**坑 5｜进程内后台任务活不过一次重启（前端报 `Failed to fetch`）**
- 现象：点刷新后数据只同步了一半，页面弹「读取主线挖掘数据失败：查询刷新进度失败：**Failed to fetch**」。
- 根因：刷新跑在 API 进程的后台线程里（`_spawn`），后端在 18:56:07 / 18:57:18 两次重启把线程带走；
  前端轮询撞上连接被拒 → 浏览器抛 `TypeError: Failed to fetch`。
- 修法：① 任务**逐数据集提交 + 逐条落账**（已完成的不白干）；② 前端**轮询容错 8 次（≈12 秒）**，
  期间显示「连接中断，正在重试（n/8）」；③ 任务"查不到"（`state=idle`）判定为"后端重启过"，
  自动重读快照而不是显示"还没有任务"；④ 网络层失败单独给文案。
- 代码：`src/api/routes/mainline.py::_spawn`；`web/src/components/MainlinePanel.tsx`（`TASK_POLL_TOLERANCE`）；`web/src/mainlineApi.ts`
- 教训：**几十秒以上的任务都要按"随时会被打断"设计**；幂等 + 可续跑 + 前端容错，三件都要有。

**坑 6｜新增的定时作业当天不会补跑**
- 现象：`mainline_daily`（17:30）当天 **0 条运行记录**，而 17:50 的 `mainline_etf_flow` 正常跑了。
- 根因：这条 `JobSpec` 是当天 **18:06** 才注册进 `JOB_REGISTRY` 的；调度器在 import 时读表，
  17:30 那一刻它不存在。
- 修法：手动触发一次；并把"新增作业当天不补跑"写进文档。
- 代码：`src/scheduler/registry.py`（`mainline_daily`）、`src/scheduler/service.py::_tick`
- 教训：**cron 表是"注册即生效、不回填过去"**。

**坑 7｜一个永远失败的可选数据集，会暂停整个每日作业**
- 现象：dev 隔离库里 `board_crowding / member_crowding` 必然 `no such table`
  （两张表只建在生产库），作业连续 3 次 failed → **自动暂停** → 连 `board_bar / board_flow` 一起停
  → 表现就是"面板日期再也不动"。
- 修法：`_MAINLINE_OPTIONAL_DATASETS` **记名但不判失败**；并加测试钉住"决定打分口径的档不许进这个名单"。
- 代码：`src/scheduler/jobs.py::_MAINLINE_OPTIONAL_DATASETS` / `_mainline_daily`；`src/scheduler/registry.py::PAUSE_AFTER_CONSECUTIVE_FAILURES`；`src/scheduler/run_log.py::is_paused`
- 测试：`tests/unit/test_mainline_daily_job.py`（7）
- 教训：**"自动暂停"是好机制，但前提是失败判据不能包含永远失败的项**。

**坑 8｜运行记录读错目录（dev / prod 两套）**
- 现象：读 `data/scheduler/job_runs.jsonl` 最后一条停在 9/14，差点得出"作业根本没跑"。
- 根因：`--env dev` 会把 `SCHEDULER_DIR` 改道到 `data/dev/scheduler/`。
- 代码：`manage.py::dev_isolation_env`
- 教训：**排查前先确认服务的 `MOSS_ENV`**。

### C. 数据源语义

**坑 9｜腾讯 `fqkline` 给了区间就不返回"当天未收盘 bar"**
- 修法：首页用 `open_range`，路由加 `min_date` 地板（市场时钟），历史区间行为不变。
- 代码：`src/infrastructure/connectors/tencent_daily_connector.py::_wants_forming_bar`、`src/infrastructure/connectors/router.py`（`min_date`）
- 测试：`tests/unit/test_tencent_daily_connector.py`、`tests/unit/test_daily_freshness.py`

**坑 10｜"慢"的真凶是链路成本 + 前端强制刷新**
- 现象：日K 渲染要 ~10s。
- 根因：AkShare 陈旧一跳 0.4~0.6s（每次都付）+ 前端 `refresh=true` 绕过缓存。
- 修法：stale-memo 跳过（90s）+ 前端 SWR（先画缓存快照再补强刷）+ **刷新不清屏**。
- 结果：链路 0.16~0.23s，切换 99ms~2s。
- 代码：`src/infrastructure/connectors/router.py`（`_STALE_SKIP_SECONDS`）、`web/src/components/IntradayDailyPanel.tsx`
- 测试：`tests/unit/test_connector_router.py`（stale memo）

**坑 11｜QMT 放链首 = 每次白等 4~5 秒**
- 根因：本机 QMT 终端失去行情权限；而"健康排名样本 <3 时照抄配置顺序"，所以**只改开关不改顺序，它照样顶到链首**。
- 修法：QMT 降到链路**最后一位**且默认关闭（`QMT_ENABLED=0` + `configs/intraday.yaml` 的 `data.qmt_enabled: false`）。
- 文档：`AGENTS.md`「环境配置」、`docs/DATA_SOURCE_ROUTING.md`
- 教训：**降级要同时改"开关"和"顺序"**，两处表达同一个意图。

### D. 前端与工程细节

**坑 12｜CSS 特异性 + 类名撞车（两个真事）**
- `.hl-levels` 同时带 `muted-text`，于是 `.intraday-headline .muted-text` 赢了字号设置 → 加 `:not(.hl-levels)`；
- `.quant-disclaimer` 是多因子面板底部在用的类，复用会让新提示被 `margin-top:14px` 顶下去、
  并把 `margin-left:auto` 泄漏过去 → 改名 `.quant-tabs-disclaimer`。
- 另外 `.cycle-badge .cycle-stage` 与 `.cycle-badge.ok` **特异性相同（0,2,0）**，只能靠**书写顺序**取胜 —— 注释里写明了这个坑。
- 代码：`web/src/styles.css`（`.intraday-headline .muted-text:not(.hl-levels)`、`.quant-tabs-disclaimer`、`.cycle-badge .cycle-stage`）

**坑 13｜逐点标数值会压字，要用程序化几何检测**
- 现象：给大周期/小周期逐点标数值后，出现 5 处文字重叠（含线尾值压在右轴刻度上）。
- 修法：① 右轴刻度贴到最右侧，把线尾「61 / 23 / 7」让出来；② 金叉日（小周期 > 大周期）
  **两条标签互换上下侧**；③ 用 headless 浏览器计算**所有 `<text>` 的两两相交**，复检归零。
- 代码：`web/src/components/AuctionSelectPanel.tsx`（`CYCLE_ANNOTATE_ABOVE/BELOW`、`CYCLE_PAD.right`）
- 教训：**视觉问题也要能"测"** —— 几何断言比"我看了一眼没问题"可靠。

**坑 14｜缓存只有一个槽位 → 请求参数被吞掉**
- 现象：前端请求 `days=30`，却拿到 120 天（横轴、图例、左轴上限全按 120 天算，而面板头写着"近 N 个交易日"）。
- 根因：`build_cycle` 的进程内缓存是单槽位，"缓存窗口 ≥ 请求天数"就把**整个宽窗口**返回。
- 修法：命中宽缓存时 `_trim()` 裁到请求窗口（`stage` 不裁 —— 只看最后一天与前一天，与窗口长度无关）。
- 代码：`src/auction_select/sentiment_cycle.py::build_cycle` / `_trim`
- 测试：`tests/unit/test_sentiment_cycle.py::test_narrower_request_is_trimmed_from_a_wider_cache`
- 教训：**"缓存命中"要连"形状"一起校验**，不只是"有没有"。

**坑 15｜测试会写坏生产缓存**
- 现象：跑一次测试，正在运行的服务把 14 GB 的 SQLite 仓库报成「MySQL 不可用」。
- 根因：`build_data_health(force=True)` 会写生产的 `data/quant/warehouse_stats.json`。
- 修法：缓存路径可重定向（`MOSS_HEALTH_CACHE_DIR`）+ `tests/conftest.py` 加 **autouse** fixture。
- 代码：`src/api/data_health.py`、`tests/conftest.py`
- 测试：`tests/unit/test_health_performance.py`
- 教训：**任何写文件的缓存，测试里都必须显式指到 tmp**；"记得自己清理"是防不住的。

**坑 16｜启动自检拒绝错误解释器（这是好事）**
- 现象：`python manage.py start` 直接拒绝启动：当前解释器缺 `lightgbm / sklearn`。
- 意义：模型全加载失败会伪装成"模型文件丢了 → 自动重训"，所以**让漏配表现为启动失败**比打警告强。
- 代码：`manage.py::missing_runtime_dependencies`；用 `.venv\Scripts\python.exe` 启动。

---

## 4. 分岗位题库（答案要点都挂在本项目的证据上）

### 4.1 量化开发

| 问题 | 答案要点 |
|---|---|
| 你怎么保证数据是对的？ | 三层水位（分区/仓库/应该有）+ 四态判定；**判不了就 `unknown`，不算已同步**；单位口径（金额已是「元」，多乘一次会让资金流放大 1000~10000 倍而排序不变） |
| 复权、停牌、ST、新股怎么处理？ | `adj_factor` 缺失会**静默不复权**、除权日假跳空；ST 按名称；新股按真实 `list_date`（不能用"窗口内第几次出现"） |
| 前视偏差怎么防？ | 财务按 `ann_date ≤ trade_date` 截断；概念成分股只有当前快照 → **幸存者偏差**，申万 PIT 成分股做补偿；上市日期闸门必须在所有成分股过滤之前 |
| 涨停/跌停怎么判？ | 按**限额表价格**判（`quant_stk_limit.up_limit/down_limit`），ST 的 5% 与主板 10% 天然区分，不靠 pct 阈值硬编码 |
| 性能 | 1500 万行 SQLite：`MAX(rowid)` 代替 `COUNT(*)`（曾把 `/health` 卡到 300s）、`(code, trade_date)` 索引命中 12~31ms、把"扫 3.5 万分区目录"移出请求路径 |
| 幂等与补数 | 同一交易日重复触发只 upsert；补数按**每档自己的水位**（`moneyflow` 比 `daily` 晚约 2 个交易日，用 daily 的缺口去灌所有档会永久漏三天） |
| 调度 | 收盘后 `*/30` 重试；发布线 16:30；连续 3 次失败自动暂停（且要保证失败判据里没有"永远失败"的项） |

### 4.2 AI 应用开发

| 问题 | 答案要点 |
|---|---|
| Agent 怎么编排？ | LangGraph `StateGraph` + Supervisor；18 个 Agent 分 5 层（数据/信息/分析/行业/决策+审计）；Agent 间标准 JSON 消息、MCP 式工具注册；Demo 单进程分层、保留拆微服务路径 |
| 可溯源怎么做？ | 每个数据点要求 `source_url / publish_time / fetch_time / raw_content_hash`；LLM 推理记 `prompt_hash` 与 token 数；结论引用数据 ID；审计链独立存储 |
| 模型/数据失败怎么办？ | **fail-open 但显式标注**：缺数据给 `null` + `gaps`，绝不返回 0 兜底；LLM 与网络失败降级到链尾源，不伪造结果 |
| 怎么回归？ | 把口径钉进单测而不是靠人眼：如"抢跑否决必须带上命中的档位与真实数值"、"可选数据集不许进关键名单" |
| 语料质量 | OCR 失败章节永久缺失（如实标注）；通达信"伪题材"要正则排除，否则题材维度集体顶格失效 |

### 4.3 AI 应用架构师

| 问题 | 答案要点 |
|---|---|
| 多租户/环境隔离 | RLS 策略；`--env dev` 用环境变量改道**数据/调度/审计/通知**四件套；15 GB 共享仓库**只读多开、写只允许单进程**（就绪副本 `warehouse.ready.db` + 锁 `quant_data_sync`） |
| 调度拓扑 | Demo 用进程内 5 字段 cron；生产可切 Celery Beat；幂等 + 分布式锁双保险；自动暂停要有上限与人工介入路径 |
| 可观测性 | JSONL 运行记录（run_id/trigger/status/rows/duration）；健康面板；`sync_check.json` **落盘**（uvicorn 默认 WARNING，"没消息"与"没运行"分不清）；启动 30s 后自检 + 自动补偿 |
| 数据契约 | 三层水位 + 四态判定；**"不猜"写进代码**：判不了返回 unknown；缓存可重定向，避免测试写坏生产 |
| 安全 | 密钥只从环境变量读；新上的鉴权（登录页/401/7 天免登录）后**未授权边界要重测**——本次就出现"无头浏览器进不去面板" |

---

## 5. 必须背熟的硬数字

| 项 | 数字 |
|---|---|
| 行情仓库 | `quant_daily` **1541 万行**（2006-01-04 起）、`quant_stk_limit` **1445 万行**、全库 **8567 万行**、**15.4 GB** SQLite；索引命中 **12~31ms** |
| 情绪周期 | 60 交易日 × ~5200 只 = **31 万行 ≈1.6s**；五线 **2.5~4s**；缓存 **30 分钟**；30 天窗口家数峰值 **101**（左轴 0~200）、60/120 天峰值 **487**（左轴自动上扩 600） |
| 日K | 取数 **10s → 0.16~0.23s**；陈旧跳过窗口 **90s**；面板切换 **99ms~2s** |
| 竞价量比 | 阈值 **10% → 13%**；抢跑实测值 **0.964333**（命中②档） |
| 主线挖掘 | **138** 个概念板块；当天指数 19:07 仅 **66/138**、19:19 重跑 **138/138**；`future` 一夜 **1901 秒**；`margin` 09-23 上游未发布（如实 `partial`）；快照 138 板块 / 候选 28 / 精选 10 / 告警 9~10 |
| 调度 | `quant_data_sync` = `*/30 16-23 * * 1-5`；发布线 **16:30**；连续失败暂停阈值 **3**；`mainline_daily` = `30 17 * * 1-5` |
| 回归面 | 本次相关 10 个测试文件 **204 passed**（全量单测另计）；本次新增 19 个用例（日历 3 + 分流/隔离 2 + 逐板块发布 6 + 可选数据集 3 + 窗口裁剪 1 + 其它 4） |

---

## 6. 别吹过头：诚实边界（本次会话版）

| 事项 | 事实 | 怎么讲 |
|---|---|---|
| 上游未发布的部分 | `ml_margin` 的 09-23 **当天仍缺**（两融深夜才发布） | "我让它如实记 `partial / missing: ['20260923']`，不粉饰成 ok。" |
| 期货当天数据 | `ml_future` 09-23 只补了一部分（大档数据集，一晚 1900 秒量级） | "重数据集单独后台跑，不影响主面板日期与评分。" |
| 前端目视核对 | 鉴权上线后 GUI 停在登录页，**本次多数前端改动只做了"构建产物字符串/几何"核对**，没做人工目视 | "这次核对在产物层：旧文案在包里 0 次、标签几何相交归零。目视留给用户验收。" |
| 板块指数覆盖 | 主板口径；**北证日线本地没有**（`quant_daily` 各期北证 0 行） | "文档锚点里'大肉 90 家、30CM 主导'复现不出来，这是数据事实，不是算法问题。" |
| 情绪周期口径 | 只统计主板（用户口径），文档锚点 2023-11-24「大面 24 家」本地复算 **23 家** | "差 1 家，可解释；我没有为了对齐数字去调口径。" |
| 集群/容器化 | 本项目是单机 SQLite + 进程内调度，**没有** K8s / 消息队列 / 分布式追踪 | 被问到就讲演进路径：仓库换对象存储 + 列存、调度换 Celery/K8s CronJob、加分布式锁与幂等键、打点接 Prometheus |
| 私有资产 | 竞价选股 / 擒牛线 / 情绪周期图 / 模型在私有目录，开源裁剪里被 `.gitignore` 排除 | "开源版把这些裁掉了，我可以讲设计与口径，但代码不在公开仓库里。" |

**"我不知道"的正确用法**：被问到没做过的（K8s、Flink、特征平台、在线学习），直接说"这块我没做，
我的理解是……"，再连回做过的（例如"我做的是单机 SQLite 的读写分离与幂等，没有多副本一致性经验"）。

---

## 7. 反问面试官（挑 2–3 个）

1. 你们的行情是"先落对象存储再入库"还是直接写库？日频 / 分钟级分别怎么保证**幂等与可重放**？
2. 数据新鲜度是怎么监控的——缺口是**报警**还是**自愈**？有没有"判不了"这种第三态？
3. 回测与实盘的差异主要来自哪里？滑点与冲击成本的口径是什么？
4. Agent / LLM 链路的失败是重试、降级还是人工接管？有没有**成本与时延预算**？
5. 你们的"策略失效"怎么定义（回撤阈值 / IC 衰减 / 人工）？上线闸门是自动还是人工？

---

## 8. 延伸阅读（本次会话涉及的仓库文档）

| 想深入了解 | 读这个 |
|---|---|
| 同步作业两处缺陷的完整归因（含实测证据与"排查三步法"） | `docs/QUANT_M1_DATA_LAYER.md` §8.12、§8.13 |
| 主线挖掘：面板日期、逐板块发布、可选数据集与暂停 | `docs/MAINLINE_MINING.md` §16.71、§16.73、§16.77 |
| 情绪周期五线图口径（含文档锚点校验） | `docs/AUCTION_SELECT.md` §一之二 🔒 |
| 数据源路由与降级（QMT 降链尾） | `docs/DATA_SOURCE_ROUTING.md`、`AGENTS.md`「环境配置」 |
| 做T / 日K 设计与运维 | `docs/INTRADAY_T_DESIGN.md`、`docs/INTRADAY_T_OPERATION_GUIDE.md` |
| 调度设计 | `docs/SCHEDULER_DESIGN.md`、`src/scheduler/registry.py` |
| 多租户与环境隔离 | `docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.7 |
| 本次会话的回归面（可直接跑） | `tests/unit/test_quant_freshness.py`、`test_mainline_daily_job.py`、`test_mainline_board_bar_freshness.py`、`test_sentiment_cycle.py` 🔒、`test_mainline_datastore.py` |

---

> **最后一句**：这一轮最能打的一句是
> **"我把'是不是最新'从一个数字，变成了一个会承认自己判不出来的判定函数。"**
> 面试收尾时用它比"我修了很多 bug"有力得多。
