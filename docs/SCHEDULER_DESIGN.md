# 定时任务调度设计

## 一、调度架构

采用Celery Beat作为调度框架（Demo版），支持任务的分布式执行和故障转移。

所有定时任务必须注册到统一调度中心，禁止在代码中硬编码crontab或sleep循环。

## 二、数据更新频率分层

| 数据层级 | 数据类型 | 更新频率 | 调度策略 |
|----------|----------|----------|----------|
| 宏观数据 | GDP、CPI、PPI、PMI | 随官方发布日即时更新 | 监控官方发布日历 |
| 宏观数据 | 社融、M2 | 每月10-15日 | 定时轮询+变更检测 |
| 中观数据 | 行业库存、产能利用率 | 月度（次月27日左右） | 定时轮询 |
| 中观数据 | 产业链价格 | 日频/周频 | 每日收盘后更新 |
| 微观数据 | 实时行情 | 实时（延时≤500ms） | 流式推送 |
| 微观数据 | 财务数据 | T+1日 | 每日凌晨更新 |
| 微观数据 | 估值指标 | 每日盘后 | 每日收盘后计算 |
| 另类数据 | 厄尔尼诺指数 | 月度/周度 | 随NOAA发布更新 |

## 三、任务执行规则

- 每个定时任务必须实现幂等性：重复执行不产生副作用。
- 任务失败必须实现指数退避重试：首次失败后1分钟重试，第二次5分钟，第三次15分钟，最多重试3次。
- 任务超时时间必须显式设置，默认超时时间为该任务P99执行时间的1.5倍。
- 批量任务必须分片执行：大数据量更新任务拆分为多个分片，并行执行。

## 四、任务监控与告警

- 每次任务执行必须记录：task_id、task_name、start_time、end_time、status、records_processed、error_message。
- 任务连续失败3次时，自动触发告警并暂停该任务，等待人工介入。
- 每日生成任务执行报表，统计成功率、平均耗时、失败原因分布。

## 五、任务清单

| 任务名称 | 调度频率 | 数据源 | 依赖 |
|----------|----------|--------|------|
| fetch_macro_data | 随官方发布 | 国家统计局 | 无 |
| fetch_market_data | 每日收盘后 | AkShare | 无 |
| fetch_financial_data | 每日凌晨 | BaoStock | 无 |
| fetch_noaa_data | 每周一 | NOAA | 无 |
| calc_valuation | 每日收盘后 | 内部计算 | fetch_market_data |
| update_cache | 每小时 | 内部 | 所有fetch任务 |
| audit_cleanup | 每月1日 | 内部 | 无 |

## 六、实现状态（src/scheduler/）

已落地（Demo版，2026-09）：

- **注册表（单一事实源）** `registry.py`：作业名/crontab/类型/参数集中声明，
  Beat计划与管理API均从该表生成，禁止散落硬编码cron。
  当前注册作业：`snapshot_macro`（每日18:30 CPI/PPI快照）、
  `snapshot_industry_watchlist`（工作日17:00 半导体/煤炭/创新药/白酒）、
  `run_log_cleanup`（每月1日03:00 清理过期运行记录）。
- **幂等执行** `jobs.py`：作业复用研究StateGraph，A04存储按
  (指标,期别,内容哈希)去重，重复执行零新增；同作业进程内asyncio锁防重叠。
- **运行记录** `run_log.py`：`data/scheduler/runs.jsonl`，含
  run_id/trigger/起止时间/耗时/状态/处理行数/错误信息；连续失败3次自动暂停
  （仅跳过schedule触发，手动触发可用于人工介入恢复）；日报统计成功率/平均耗时/
  失败原因分布；保留期90天（settings.scheduler_run_log_ttl_days）。
- **Celery Beat** `celery_app.py`：broker为 `settings.celery_broker_url`
  （Redis db1）；失败按1分钟→5分钟→15分钟退避重试，最多3次；软超时900s。

启动worker+beat（Windows需solo池）：

```
uv run celery -A src.scheduler.celery_app.celery_app worker -B --pool=solo -l info
```

- **管理API（无需Redis即可用，进程内直接执行）**：
  - `GET  /api/v1/scheduler/jobs`：作业清单/cron/暂停态/最近一次执行
  - `POST /api/v1/scheduler/jobs/{name}/run`：手动触发（202，返回运行记录）
  - `GET  /api/v1/scheduler/runs?job=&limit=`：执行历史
  - `GET  /api/v1/scheduler/runs/summary?date=YYYY-MM-DD`：日报

待办：设计文档五中的fetch_market_data/fetch_financial_data/fetch_noaa_data/
calc_valuation等作业待对应连接器接入后按同一注册表模式追加；
分布式重叠执行锁（多worker场景）待接入Redis分布式锁。
