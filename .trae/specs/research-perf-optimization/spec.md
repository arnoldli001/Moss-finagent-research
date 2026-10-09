# 投研分析性能与 Token 优化 - 需求文档（Spec）

## Overview

- **Summary**: 针对「投研分析」单次分析耗时长、token 消耗多的问题，做端到端优化，分三条主线：①**按数据更新周期的库内优先缓存**——宏观/产业等非短周期数据被获取过一次、未到下一更新周期时直接读库而非重复网络请求，重点补齐当前**每次都走网络的新闻/政策信息**链路；②**数据保留上限**——数据点最多保留最近 10 年，超期自动清理；③**Token 端到端节省**——提示词改写为专业精简描述、按任务类型用 `reasoning_effort` 控制思维链强度、按层级重设输出预算以消除"思维链吃光上限、正文为空"。
- **Purpose**: 让重复发起的投研分析更快返回、显著减少网络往返与 token 账单，同时不降低结论质量、不改变多 Agent 架构。
- **Target Users**: 投研工作台使用者（单租户试运行形态；接口遵循统一数据层与 LLM 网关红线）。

## Goals

- G1: 新闻/政策/快讯信息按更新周期缓存：进程内 TTL → 本地 DB → 网络，周期内重复分析不重复网络取数。
- G2: 核查并补齐宏观/产业慢变量在连接器路由中的"查库"覆盖，确保 `plan_run` 会拉取的非短周期指标在更新周期内由库供给。
- G3: 数据点保留上限默认 10 年：按 `period_date` 删除早于截止线的数据；保留年限可配置；幂等、可统计删除量。
- G4: 保留清理在应用启动后自动执行一次，并注册为低峰定时作业；清理失败不阻断投研主流程。
- G5: 通过 DeepSeek 原生 `reasoning_effort`（none/low/high）按任务类型控制思维链强度：机械型任务用低强度，核心分析保持高质量。
- G6: 按任务层级/模型重设输出预算（max_tokens），使思维链与正文都能容纳，消除正文被思维链挤占导致的空响应与 repair 重试。
- G7: 全量提示词精简改写：去重复红线与客套指令、用专业紧凑表述；保留直接作答、防杜撰、引用 period_date 与 JSON schema 等硬约束。
- G8: 可观测：记录 reasoning_tokens、reasoning_effort、缓存命中，支撑优化前后对比。
- G9: 回归与合规：现有测试全绿、ruff 通过；不删减 Agent；真实源失败不回退模拟源；输出仍附免责声明。

## Non-Goals

- N1: **不删减、不合并任何现有 Agent，不改变研究图拓扑与 Agent 数量**（用户明确约束）。
- N2: 不做前端页面重构（本次以后端/性能/提示词为主；如需可观测信息仅做最小化暴露）。
- N3: 不清理 LLM 审计日志与业务审计链（合规要求敏感操作日志保存不低于 3 年），本次保留机制只针对投研数据点与新闻缓存。
- N4: 不引入新的重依赖或新的常驻缓存中间件（Redis 可选项维持原状）。
- N5: 不改变业务结论口径、不弱化防幻觉机制（精简提示词后仍有 HallucinationGuard 与溯源兜底）。
- N6: 不做 PostgreSQL 专属的分区/分页清理调优（采用可移植 SQL，同一截止线在两种后端均成立）。
- N7: 不改变回测、事件告警等其他子系统的既有行为（复用其基础设施时只做新增、不改语义）。

## Background & Context

- **数据获取三级短路已存在**：[router.py](file:///d:/code/Moss-finagent-research/src/infrastructure/connectors/router.py) 实现"进程内 TTL 缓存 → 本地持久化 DB（带过期判定）→ 真实连接器链"。投研运行时已注入 repo（`ConnectorRouter(routes, repo=repo)`），月频宏观、估值、两融、日线等慢变量的库内优先**实际已生效**：TTL 按前缀分档（月频 24h / 估值行业 PE 12h / 两融北向 4h / 实时成交额 5min / FedWatch 1h / 日线 4h），DB 是否可用由 `_is_db_fresh` 按 `confidence>=0.4` 判定。
- **数据更新周期口径已存在**：[data_freshness.py](file:///d:/code/Moss-finagent-research/src/core/data_freshness.py) 定义日/周/月/季/年频与长周期行业指标的周期、滞后、过期窗口，`confidence=exp(-0.5×days/(cycle×行业倍数))`，分 fresh/normal/lagging/stale/expired 五级。
- **统一落库已存在**：`fact_data_points` 表（[macro_repo.py](file:///d:/code/Moss-finagent-research/src/infrastructure/repositories/macro_repo.py)，类名 MacroRepository 为历史命名、现为通用数据点仓储），含 source_url/publish_time/fetch_time/raw_content_hash，唯一约束 (indicator, period_date, raw_content_hash)；已有按指标删除、计数方法，但**无"按保留年限批量清理"能力**。
- **真实 Gap 1：新闻/政策无缓存**：collect_node 每次分析调用 `news_fetcher.fetch_news(code)` / `fetch_topic_news(keywords)`（[news_fetcher.py](file:///d:/code/Moss-finagent-research/src/infrastructure/connectors/news_fetcher.py) 的 LocalFallbackNewsFetcher），无 TTL、无落库，每次走网络。这是"数据临时网上取"痛点最主要的未覆盖点。
- **真实 Gap 2：无 10 年/任何保留期机制**：数据点仓库无按时间批量 prune 逻辑。
- **真实 Gap 3：Token 配置与提示词**：
  - [models.yaml](file:///d:/code/Moss-finagent-research/configs/models.yaml) 中 deepseek-flash、deepseek-v4-pro 的 max_tokens 均为 4096；DeepSeek 为推理模型，**推理 token 计入输出上限**，网关注释证实约 20% 调用思维链吃光 4096、正文为空，且 JSON 解析失败的 repair 以 `use_cache=False` 重试、成本翻倍。
  - 分析层 prompt（[analysis/base.py](file:///d:/code/Moss-finagent-research/src/domain/agents/analysis/base.py) 约 L243-L279）规则 1-6 与大段"时效红线"存在重复，客套与重复红线偏多；上下文已截断到最近 60 期，技能块已限 4000 字符。
- **外部能力已核实（官方文档）**：DeepSeek 原生 `https://api.deepseek.com/chat/completions` 支持顶层 `reasoning_effort`，取值 `none/low/high/max`（none 关闭思维），并可用 `thinking:{type:enabled/disabled}` 开关；思维链经独立的 `reasoning_content` 返回（与正文 `content` 平级），无 tools 的单轮请求不回拼 CoT；思维模式下 temperature 不生效但不报错。
- **投研链路**：`POST /api/v1/research/analyze` → 结果缓存/名称解析 → `plan_run()` 规划 → LangGraph（supervisor→collect（指标 asyncio.gather 并发）→clean→validate→store→信息/分析/行业节点 fan-out→recommend→audit）。collect 指标路径已走 A01→路由（享缓存），**仅新闻旁路在 collect 末尾直接调 fetcher**。
- **调度**：[registry.py](file:///d:/code/Moss-finagent-research/src/scheduler/registry.py) 为 cron 单一事实源，[jobs.py](file:///d:/code/Moss-finagent-research/src/scheduler/jobs.py) 按 kind 分发，RunLog 记录成败、连续失败自动暂停；手动触发走调度 API。

## Functional Requirements

- **FR-1 新闻缓存读取层**：新增缓存型 fetcher（包装现有 LocalFallbackNewsFetcher），按"作用域 + 键"读取：个股新闻键为 6 位代码，主题新闻键为规范化关键词集合的哈希。读取顺序为进程内 TTL → 本地 DB（fetch_time 在 TTL 内视为有效）→ 网络；命中即返回且不调网络。保持现有输出契约 `[{text,title,source_name,source_url,publish_time,...}]` 不变。
- **FR-2 新闻缓存存储**：经统一仓储端口访问独立新闻缓存表（如 `news_cache`，字段含 scope/cache_key/payload_json/latest_publish_time/fetch_time），SQL 全参数化、`CREATE TABLE IF NOT EXISTS` 幂等建表；同一 cache_key 重复获取做 upsert。
- **FR-3 新闻缓存安全与降级**：网络返回空或异常时**不写入空缓存、不污染后续请求**；新闻为增强链路，缓存/网络异常均不阻断投研主流程；新闻自身保留窗口可配（默认短于 10 年，如 30 天），由保留任务一并清理。
- **FR-4 慢变量库内覆盖核查**：枚举 `plan_run` 各 analysis_type 会拉取的指标，逐一确认其前缀命中路由的查库清单；对核查中发现的"应查库但未配置"的非短周期指标，补齐到路由的 TTL/查库前缀表（复用 data_freshness 口径），并在任务中以清单形式留痕。
- **FR-5 数据点保留清理**：仓储新增"按截止线删除"方法，删除 `fact_data_points` 中 `period_date < cutoff` 的行，cutoff = 今天 − 保留年限（默认 10 年）；返回删除行数；事务内执行、幂等；不删除截止线及之后的数据。
- **FR-6 保留清理触发**：应用启动且 schema 就绪后自动执行一次（含数据点 + 新闻缓存各自窗口）；注册表新增低峰维护作业（如每周一次），经 execute_job 执行并写 RunLog；清理异常被捕获、记日志，不影响投研服务。
- **FR-7 配置项**：新增并从环境/配置读取——数据点保留年限（默认 10）、新闻缓存 TTL（个股/主题分档，默认分钟级）、新闻保留天数、各任务层级的 reasoning_effort 默认值与输出预算；均有安全默认值。
- **FR-8 思维链强度控制**：`LLMGateway.complete()` 接受 `reasoning_effort` 参数（缺省按任务层级映射：light→none/low、medium→low、reasoning→high、decision→high），透传至 DeepSeek provider（顶层 `reasoning_effort`，思维启用时附 `thinking`）；Ollama provider 不发送该参数；允许具体 Agent 按调用覆盖。
- **FR-9 输出预算重设**：按层级/模型重设 max_tokens，使推理 token 与正文都能容纳（适度上调 flash/pro 在 reasoning、decision 层级的有效预算，本地模型维持），任何调用仍受 `llm_max_tokens_hard_cap` 约束；从 usage 中采集 `reasoning_tokens`（若提供）。
- **FR-10 提示词精简改写**：对分析层、行业层、信息层、决策层 system/组装提示词做专业精简改写——合并重复的时效红线与回答规则、去客套与重复禁止项、紧凑表述；必须保留：结论首句直接作答、仅用给定数据禁杜撰、引用 period_date、JSON 字段 schema、流动性研判顺序（适用时）、免责声明；上下文/技能块继续限量。
- **FR-11 降低重试成本**：输出预算修复后减少"空响应/截断"引发的 repair；保留一次 JSON repair 但不额外加长、且空响应不写缓存（沿用现有闸门）。
- **FR-12 可观测与对比**：LLMResponse/审计记录 reasoning_tokens、reasoning_effort、缓存命中；提供可在固定场景下重复运行的轻量对比方式（同一查询连跑两次），输出网络取数次数、tokens_in/out、延迟，用于验证收益。

## Non-Functional Requirements

- **NFR-1 架构合规**：所有 LLM 调用经 LLMGateway、所有数据访问经统一仓储端口；依赖方向 core←domain←infrastructure←api 单向；单文件 ≤300 行、函数 ≤50 行（ai-dev-rules）。
- **NFR-2 安全**：禁止硬编码密钥；SQL 全参数化；新增输入（键/载荷）做长度与格式约束；不输出凭据。
- **NFR-3 可靠性**：缓存与清理一律 fail-open，任何单点失败不阻断投研分析；防止空缓存固化与缓存击穿（复用 per-key 锁）。
- **NFR-4 性能**：更新周期内的第二次分析，新闻/慢变量不再发起对应网络请求；数据密集型重复分析的端到端延迟较基线显著下降（以基准脚本实测为准，目标 ≥40%）。
- **NFR-5 成本**：代表性场景下单次分析总 token 较基线下降（目标 ≥20%，其中静态指令段 ≥25%），且结论质量不下降；以优化前后固定场景实测为证据，而非硬 CI 门。
- **NFR-6 可测**：缓存命中/穿透、保留截止线、effort 映射与载荷、提示词要素均为可注入假件的纯函数或单测；全量 pytest 全绿、ruff 通过；新增纯函数单测覆盖率 ≥80%。
- **NFR-7 兼容/迁移**：新表幂等创建、不改写既有数据；默认配置除"新增缓存、预算与清理"的预期行为外保持旧行为；SQLite 与 Postgres 后端均可运行。

## Constraints

- **Technical**: Python 3.10+，SQLite 默认、Postgres 可选；不新增重依赖；Windows + PowerShell（脚本注意编码）。
- **Business**: 输出附免责声明；不删减 Agent；真实源全失败禁止回退 simulated；北向停披等既有缺口约定照旧。
- **Dependencies**: akshare 已安装；Ollama 可选（不可用时相关层级走 DeepSeek，需 DEEPSEEK_API_KEY）；DeepSeek 原生接口支持 reasoning_effort（已核实）。

## Assumptions

- 新闻/快讯属高频信息，其"更新周期"以分钟级 TTL 表达（而非月年级闸门），足以消除短时间内重复分析的重复网络取数；宏观/产业慢变量沿用既有 data_freshness 周期。
- period_date 为定长 `YYYY-MM-DD`，字符串比较与日期比较等价，可直接用于截止线删除。
- 思维模式下 temperature 被服务端忽略不影响稳定性；默认 effort 映射可在实现时按具体 Agent 微调但不改变总体策略。
- 用户已全权授权，默认值按推荐方案选定并在交付时报备。

## Acceptance Criteria

### AC-1: 新闻周期内缓存命中、不重复网络
- **Type**: `rule`
- **Given**: 已用可计数的假 fetcher 装配缓存型新闻读取层，且首次已成功获取
- **When**: 在 TTL 内对同一代码/同一组关键词再次分析
- **Then**: 直接返回缓存内容，底层网络 fetcher 调用次数为 0，返回条目契约与首取一致
- **Pass Condition**: 单测断言第二次读取网络计数为 0 且条目相等
- **Evidence**: 新闻缓存相关单测

### AC-2: 新闻缓存落库并跨进程生效
- **Type**: `rule`
- **Given**: 首次网络获取后已持久化，且清空进程内 TTL（模拟重启）
- **When**: 在 TTL 内再次读取同一键
- **Then**: 从 DB 命中、不调网络；超过 TTL 后则重新走网络
- **Pass Condition**: 单测断言 DB 行存在、新实例命中库；TTL 过期后发生网络调用
- **Evidence**: 新闻缓存仓储与读取层单测

### AC-3: 新闻空响应/异常不污染缓存且不阻断
- **Type**: `rule`
- **Given**: 底层 fetcher 返回空列表或抛异常
- **When**: 读取层被调用
- **Then**: 不写入空缓存，后续在源恢复后可正常获取；异常不向上阻断投研流程
- **Pass Condition**: 单测断言无缓存写入、再次读取会重试网络且调用方不收到异常
- **Evidence**: 新闻缓存降级单测

### AC-4: 慢变量指标库内覆盖
- **Type**: `rule`
- **Given**: `plan_run` 各 analysis_type 会拉取的指标集合
- **When**: 对每个非短周期指标判定路由是否查库
- **Then**: 所有应缓存的慢变量均命中查库前缀；发现的缺口已补齐并有测试
- **Pass Condition**: 存在一份指标→是否查库的测试/清单且全部为"是"，缺口补齐后通过
- **Evidence**: 路由前缀分类单测 + 覆盖清单

### AC-5: 10 年保留删除精确且幂等
- **Type**: `rule`
- **Given**: 同一指标存在早于/等于/晚于截止线的多个数据点
- **When**: 调用按截止线删除（保留年限默认 10）
- **Then**: 仅删除 period_date < cutoff 的行并返回删除数；截止线及之后保留；再次执行删除数为 0
- **Pass Condition**: 单测插入跨截止线数据，断言删除集合、保留集合与二次删除为 0
- **Evidence**: 仓储保留清理单测

### AC-6: 保留清理启动执行 + 定时注册
- **Type**: `rule`
- **Given**: 应用启动流程与调度注册表
- **When**: 启动后及维护作业触发时
- **Then**: 数据点与新闻缓存按各自窗口被清理一次，删除量写日志/RunLog；清理失败被捕获不阻断
- **Pass Condition**: 单测/装配验证启动调用了清理、注册表存在维护作业且 kind 已接线，异常路径不抛出
- **Evidence**: 启动与作业接线单测

### AC-7: 思维链强度按层级映射并正确下发
- **Type**: `rule`
- **Given**: 任务各层级与可记录请求体的假 provider
- **When**: 网关以缺省/覆盖的 reasoning_effort 发起调用
- **Then**: light/medium/reasoning/decision 映射到预期 effort；DeepSeek 请求体含顶层 reasoning_effort（启用思维时含 thinking），Ollama 请求体不含该参数
- **Pass Condition**: 单测断言层级→effort 映射与两种 provider 的请求载荷差异
- **Evidence**: 网关与 provider 单测

### AC-8: 输出预算容纳思维链与正文
- **Type**: `rule`
- **Given**: reasoning/decision 层级的模型预算配置
- **When**: 发生思维链较长的调用
- **Then**: 有效 max_tokens 大于正文所需且不超过硬上限，usage 中的 reasoning_tokens 被采集
- **Pass Condition**: 配置/单测断言预算区间与 reasoning_tokens 解析；正文不再因预算被思维链吃空
- **Evidence**: 预算与解析单测

### AC-9: 提示词精简且关键约束不丢失
- **Type**: `rubric`
- **Anchor**: 以分析基类为代表，静态指令段 token 数较基线下降 ≥25%；同时逐条满足"首句直接作答 / 仅用给定数据禁杜撰 / 引用 period_date / JSON schema / 适用时流动性顺序 / 免责声明"六要素
- **Scale**: 0-100；要素每缺 1 项扣 15 分，降幅不足按比例扣分
- **Pass Threshold**: ≥85
- **Evidence**: 指令 token 统计 + 六要素清单核对

### AC-10: 端到端延迟与 token 下降、质量不降
- **Type**: `rubric`
- **Anchor**: 固定代表性查询连跑两次：第二次在更新周期内，新闻/慢变量网络取数为 0；端到端延迟较基线降 ≥40%、单次总 token 降 ≥20%；结论仍直接回答问题且关键事实有 period_date 支撑
- **Scale**: 0-100；网络、延迟、token、质量四维各 25 分，按达标比例给分
- **Pass Threshold**: ≥75
- **Evidence**: 优化前后基准脚本结果（不作为硬 CI 门）

### AC-11: 回归、合规与架构红线
- **Type**: `rule`
- **Given**: 全量测试套件与 ruff
- **When**: 实施完成后
- **Then**: pytest 全绿、ruff 通过；Agent 数量与图拓扑不变；真实源失败无 simulated 回退；输出附免责声明
- **Pass Condition**: 全套测试通过且静态检查零错误，差异核查无 Agent 删减
- **Evidence**: manage.py test 输出、ruff 输出、差异核查
