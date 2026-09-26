# 投研分析性能与 Token 优化 - 实施计划

> 对应 spec：`.trae/specs/research-perf-optimization/spec.md`
> 约定：仓储在 `src/infrastructure/repositories/`，连接器/读取层在 `src/infrastructure/connectors/`，LLM 在 `src/infrastructure/llm/`，调度在 `src/scheduler/`，提示词位于各 Agent；所有 LLM 调用经 LLMGateway、所有数据访问经仓储端口，不直连数据库/模型 API。
> 红线：不删减 Agent、不改图拓扑；真实源全失败禁止回退 simulated；单文件 ≤300 行、函数 ≤50 行；ruff 行长 100。

## Task 1: 新增配置项
- **Status**: `pending`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - `src/core/config.py` 新增（均带默认值，可被同名环境变量覆盖）：`data_retention_years=10`（env DATA_RETENTION_YEARS）、`news_cache_enabled=True`、`news_cache_ttl_seconds=600`（个股/主题共用默认，env NEWS_CACHE_TTL_SECONDS）、`news_retention_days=30`（env NEWS_RETENTION_DAYS）。
  - `.env.example` 增加对应占位段（不写真实值）。
- **Acceptance Criteria Addressed**: AC-6（配置前置）
- **Test Requirements**:
  - `rule` TR-1.1: 默认保留年限=10、新闻 TTL=600、新闻保留=30 天，且可被环境变量覆盖；证据：tests/unit 配置测试

## Task 2: 新闻缓存仓储（端口 + SQLite 实现）
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增新闻缓存仓储端口（`NewsCacheRepository`）：`ensure_schema / get(cache_key) / upsert(cache_key, scope, payload, latest_publish_time, fetch_time) / prune(before_fetch_time)→int`。
  - 新增 `src/infrastructure/repositories/news_cache_sqlite_repo.py`：在 settings.sqlite_path 内建 `news_cache` 表（`cache_key TEXT PRIMARY KEY, scope TEXT, payload_json TEXT, latest_publish_time TEXT, fetch_time TEXT`，fetch_time 索引），参数化查询、线程池化，连接风格对齐 macro_repo/event_sqlite_base；upsert 用 `INSERT ... ON CONFLICT(cache_key) DO UPDATE`（SQLite/Postgres 可移植写法）。
  - `repository_factory.py` 增加 `build_news_cache_repository(settings)`。
- **Acceptance Criteria Addressed**: AC-2, AC-3
- **Test Requirements**:
  - `rule` TR-2.1: 临时库 upsert 同键两次只留 1 行且 payload/fetch_time 被更新；get 命中返回载荷、miss 返回 None；prune 删除 fetch_time 早于阈值的行并返回数量；证据：tests/unit/test_news_cache_repo.py
  - `rule` TR-2.2: SQL 全参数化（代码审查无拼接值），建表幂等；证据：代码检查

## Task 3: 缓存型新闻读取层 CachedNewsFetcher
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 新增（独立小模块，避免 news_fetcher 超 300 行）缓存读取层，包装现有 `NewsFetcher`（LocalFallbackNewsFetcher），实现相同 `fetch_news(code,limit)/fetch_topic_news(keywords,limit)` 契约。
  - 键：`stock:{code}`；主题为 `topic:` + 排序后规范关键词集合的稳定哈希。
  - 读取顺序：进程内 TTL dict → 仓储（fetch_time 在 TTL 内有效）→ 底层网络；命中即返回、网络计数不增；网络成功后回填进程 TTL 并 upsert 仓储。
  - 网络返回空列表/异常：**不写缓存**，异常不向上抛（增强链路），返回 []。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-3
- **Test Requirements**:
  - `rule` TR-3.1: 首次走网络；TTL 内第二次同键网络计数=0 且返回与首次一致；证据：tests/unit/test_cached_news_fetcher.py
  - `rule` TR-3.2: 清空进程 TTL 后在 TTL 内命中仓储、不调网络；TTL 过期后重新网络；证据：同文件
  - `rule` TR-3.3: 网络空/异常时无缓存写入、源恢复后可获取、调用方不收到异常；证据：同文件

## Task 4: 运行时接线（collect 使用缓存读取层）
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - `src/api/runtime.py`：经工厂构建 NewsCacheRepository（schema 就绪），将现有 LocalFallbackNewsFetcher 包进 CachedNewsFetcher，再作为 `news_fetcher` 注入图；公开/缺仓储环境保持可降级。
  - 不改 collect_node 调用方式与 info_items 契约。
- **Acceptance Criteria Addressed**: AC-1
- **Test Requirements**:
  - `rule` TR-4.1: 集成/装配测试验证注入的是缓存读取层且同一分析周期内重复跑 collect 新闻网络只发生一次；证据：tests/integration 或 runtime 装配测试

## Task 5: 慢变量库内覆盖核查与补齐
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 1
- **Description**:
  - 枚举 `plan_run`（supervisor.py）各 analysis_type 的指标，逐一判定路由是否查库（`_should_query_db`）与 TTL 分档。
  - 预期可能缺口（实现时核实并补）：`mkt:cybkcb:turnover_hist`、`mkt:cybkcb:val:all`、`mkt:cybkcb:spot_summary` 等日频双创指标当前可能未命中查库前缀；将确认的非短周期指标补入 router `_DB_QUERY_PREFIXES`/`_TTL_BY_PREFIX`（复用 data_freshness 口径）。
  - 以指标→是否查库的测试/清单留痕。
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `rule` TR-5.1: 对枚举的每个慢变量断言 `_should_query_db` 为 True（且有 TTL）；证据：tests/unit 路由覆盖测试
  - `rule` TR-5.2: 快变量（实时成交额/FedWatch）仍被排除在查库之外；证据：同文件

## Task 6: 数据点按截止线删除（仓储能力）
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `DataPointRepository` 抽象端口新增 `delete_older_than(cutoff_date: str) -> int`。
  - MacroRepository（SQLite）：`DELETE FROM fact_data_points WHERE period_date IS NOT NULL AND period_date != '' AND period_date < ?`，返回删除行数；线程池化。NULL/空 period_date 保留（无法归类，防误删）。
  - postgres_repo 同口径可移植实现。
- **Acceptance Criteria Addressed**: AC-5
- **Test Requirements**:
  - `rule` TR-6.1: 插入早于/等于/晚于截止线及 period_date 为空的数据点，断言仅删除 `period_date<cutoff`，截止线及之后与空日期保留，返回数正确，二次执行为 0；证据：tests/unit 仓储保留测试

## Task 7: 保留服务 + 启动钩子 + 定时作业
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 2, Task 6
- **Description**:
  - 新增保留服务（小模块）：`run_data_retention(point_repo, news_repo, settings, today)` → cutoff = today − data_retention_years；调用 `point_repo.delete_older_than(cutoff)` 与 `news_repo.prune(fetch_time < now-news_retention_days)`，返回 {points_deleted,news_deleted,cutoff}；两类各自 try/except、fail-open。
  - 启动钩子（main lifespan / runtime 启动，schema 后）：执行一次，异常只记日志、不阻断启动。
  - 调度注册表：JobKind 增加 `data_retention_cleanup`，JobSpec `data_retention_weekly`（cron `0 4 * * 0`，周日低峰）；jobs.py 按 kind 经工厂取仓储执行、写 RunLog。
- **Acceptance Criteria Addressed**: AC-6
- **Test Requirements**:
  - `rule` TR-7.1: 服务对两类仓储分别调用、返回计数；单类抛错不影响另一类且不抛出；证据：tests/unit 保留服务测试
  - `rule` TR-7.2: 注册表存在该作业且 cron/kind 正确，jobs.py 分发到保留服务并写 RunLog；启动钩子被调用一次；证据：注册表/作业接线测试

## Task 8: 网关 reasoning_effort 参数与层级映射
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `LLMResponse` 增加 `reasoning_tokens:int=0`、`reasoning_effort:str=""`。
  - `LLMGateway.complete()` 增加 `reasoning_effort: str | None = None`；None 时按层级默认：light→`none`、medium→`low`、reasoning→`high`、decision→`high`。
  - 将 effort 透传至 `provider.chat(...)`（协议与各实现/假件同步加关键字参数）；审计随响应记录。
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `rule` TR-8.1: 各层级缺省映射正确，显式覆盖优先生效；FakeProvider 收到对应 effort；证据：tests/unit 网关 effort 测试

## Task 9: DeepSeek/Ollama Provider 下发与 reasoning_tokens 采集
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 8
- **Description**:
  - DeepSeekProvider.chat：请求体顶层带 `reasoning_effort`；effort∈{low,high,max} 时附 `thinking:{"type":"enabled"}`，effort=none 时关闭思维；从 `usage.completion_tokens_details.reasoning_tokens` 采集（缺省 0），写入 LLMResponse。
  - OllamaProvider.chat：接受该参数但不下发，reasoning_tokens=0。
- **Acceptance Criteria Addressed**: AC-7, AC-8
- **Test Requirements**:
  - `rule` TR-9.1: DeepSeek 请求载荷按 effort 含/不含 thinking 且顶层 reasoning_effort 正确；模拟 usage 时 reasoning_tokens 被解析；证据：tests/unit provider 测试
  - `rule` TR-9.2: Ollama 载荷不含 reasoning_effort/thinking；证据：同文件

## Task 10: 分层输出预算（models.yaml + 调用覆盖）
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 8, Task 9
- **Description**:
  - `configs/models.yaml`：deepseek-flash、deepseek-v4-pro 的 max_tokens 上调至 8192（思维链 + 正文不再挤占）；本地 light/medium 维持；注释说明"上限不强制产出、配合 reasoning_effort 控量"。
  - 任何调用仍受 `llm_max_tokens_hard_cap`（32768）约束；必要时具体 Agent 可经现有 per-call max_tokens 微调（默认不改）。
- **Acceptance Criteria Addressed**: AC-8
- **Test Requirements**:
  - `rule` TR-10.1: 加载配置后 flash/pro max_tokens=8192 且不超硬上限；网关对 reasoning/decision 调用下发预算正确；证据：models 配置/网关测试

## Task 11: repair 与空响应降本加固
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 10
- **Description**:
  - 分析基类 JSON repair 重试：保持一次、不额外加长 prompt，并以 `reasoning_effort="low"` 执行（机械修复）；确认空响应不写缓存（沿用）且不被命中。
- **Acceptance Criteria Addressed**: AC-8, AC-11
- **Test Requirements**:
  - `rule` TR-11.1: repair 调用 use_cache=False、json_mode=True、reasoning_effort=low；空响应不产生缓存且再次请求会重发；证据：tests/unit 分析基类测试

## Task 12: 分析层/行业层提示词精简
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `analysis/base.py`：将规则 1-6 与大段「时效红线」合并为单一紧凑专业块，去重复与客套；保留——首句直接作答、仅用给定数据禁杜撰、引用 period_date、JSON schema、流动性研判顺序（适用时）、免责声明。
  - `industry/base.py::_requirements` 同步精简，保留估值/渗透率判定要求。
  - 目标：静态指令段 token 较基线 ≥25% 下降。
- **Acceptance Criteria Addressed**: AC-9
- **Test Requirements**:
  - `rubric` TR-12.1: 指令段 token 降幅与六要素齐全，锚点/阈值同 AC-9（≥85）；证据：指令 token 统计 + 要素清单测试

## Task 13: 信息层/决策层提示词精简
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 12
- **Description**:
  - 精简 A05 verifier、A06 extractor、A07 sentiment 的 system/任务 prompt（紧凑、去重复），保留 evidence 逐字、白名单、免责声明。
  - 精简 A17 decision（decision/recommend）prompt，保留 position_advice/expected_return_3_6m 与直接作答要求。
- **Acceptance Criteria Addressed**: AC-9
- **Test Requirements**:
  - `rule` TR-13.1: 精简后各 prompt 仍含关键输出字段与免责声明、且长度不超过改写前；证据：提示词要素单测

## Task 14: 可观测与基准对比
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 4, Task 7, Task 10, Task 13
- **Description**:
  - 新增 `scripts/bench_research.py`：对固定代表性查询连跑两次（经研究服务），输出每次的网络取数次数、tokens_in/out、reasoning_tokens、端到端延迟，并计算相对基线的降幅；第二次在更新周期内新闻/慢变量网络取数应为 0。
  - 确认 reasoning_tokens/effort/缓存命中在审计中可查。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rubric` TR-14.1: 网络/延迟/token/质量四维达 AC-10 锚点（≥75）；证据：基准脚本运行结果（不作硬 CI 门）

## Task 15: 全量回归与合规核查
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 4, Task 5, Task 7, Task 11, Task 13, Task 14
- **Description**:
  - 使用 `manage.py test` 跑全量 pytest；ruff 检查；核查 Agent 数量与图拓扑未变、真实源失败无 simulated 回退、输出附免责声明。
- **Acceptance Criteria Addressed**: AC-11
- **Test Requirements**:
  - `rule` TR-15.1: 全量 pytest 全绿、ruff 零错误；差异核查无 Agent 删减/无模拟回退；证据：manage.py test 与 ruff 输出

## 依赖与优先级汇总

- **关键路径**：T1 → T2 → T3 → T4；T1 → T6 → T7（并依赖 T2）；T1 → T8 → T9 → T10 → T11；T1 → T12 → T13；最后 T14 → T15。
- **可并行**：T2/T6/T8/T12 在 T1 完成后可并行；T5 可与数据/LLM 各切片并行。
- **High**：T1,T2,T3,T4,T6,T7,T8,T9,T12,T15；**Medium**：T5,T10,T11,T13,T14。
