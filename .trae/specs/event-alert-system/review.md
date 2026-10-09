# 事件监控与自动告警系统 — 独立只读代码审查报告

- 审查日期：2026-09-14
- 审查方式：逐文件阅读实现代码 + 独立运行测试/ruff/前端构建；未修改任何代码或文件（本报告除外）
- 审查范围：spec.md / tasks.md / AGENTS.md → src/core、src/domain/alerts、src/infrastructure（event_collectors、event_sqlite_repo、notifiers）、src/scheduler、src/api（routes/alerts、alert_hub、event_wiring、main、research）、tests、web/src、web/vite.config.ts

## 0. 独立验证结果（审查者亲自执行，非转述）

| 验证项 | 命令 | 结果 |
| --- | --- | --- |
| 全量单测 | `.venv python -m pytest tests -q` | **434 passed**（12.31s，1 条 pytest-asyncio deprecation 警告，与本功能无关） |
| 告警相关测试 | 10 个测试文件（含 test_notifiers.py / test_scheduler.py 中事件用例） | **81 passed** |
| Lint | `ruff check` 告警相关 src + tests 全部文件 | **All checks passed** |
| 前端构建 | `npm --prefix web run build`（tsc -b + vite build） | **零 TS 错误，构建成功**（41 modules，493ms） |

---

## 1. AC-1 ~ AC-13 逐条判定

### AC-1 事件多源采集与降级 — **PASS**

- 快讯四源主备顺序在 [news_flash.py](file:///d:/code/Moss-finagent-research/src/infrastructure/connectors/event_collectors/news_flash.py#L95-L100)（em→ths→cls→sina），每源独立 try/except 转错误条目（L127-L137、L139-L152），阻塞调用经 `asyncio.to_thread`（L131-L132）。
- 日历采集器逐接口/逐日失败隔离（[calendar_events.py](file:///d:/code/Moss-finagent-research/src/infrastructure/connectors/event_collectors/calendar_events.py#L157-L208)）。
- 服务层对采集器整体异常再兜底（[service.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/service.py#L72-L81)）。
- 证据测试：`test_news_flash_fallback_when_primary_blocked`、`test_news_flash_all_sources_failed_no_raise`、`test_calendar_source_failure_isolated`（[test_event_collectors.py](file:///d:/code/Moss-finagent-research/tests/unit/test_event_collectors.py#L100-L142)），全失败返回 `([], errors≥4)` 不抛异常。

### AC-2 事件标准化字段完整 — **PASS**

- [normalize.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/normalize.py#L79-L111) 输出含 event_id/event_key/type/title/source_name/source_url/publish_time/fetch_time/tenant_id；content 截断 2000（L83）、source_url 截断 500；event_id=evt_{确定性sha1}（L44-L46、L96）。
- 证据：`test_normalize_basic_fields_and_sina_title`、`test_event_key_deterministic_and_normalized`、`test_normalize_bad_time_does_not_raise`、`test_normalize_drops_empty_item`（[test_event_normalize.py](file:///d:/code/Moss-finagent-research/tests/unit/test_event_normalize.py)）；模型必需字段缺失抛 ValidationError 见 [test_alert_models.py](file:///d:/code/Moss-finagent-research/tests/unit/test_alert_models.py#L40-L42)。

### AC-3 跨源去重与告警冷却 — **PARTIAL**

- 同键幂等：fact_events.event_key UNIQUE + INSERT OR IGNORE（[event_sqlite_repo.py](file:///d:/code/Moss-finagent-research/src/infrastructure/repositories/event_sqlite_repo.py#L29-L30、L183-L189)），二次写入 inserted=0 有测试（test_event_repository.py L60-L69）。
- 告警冷却双保险：服务层 `_passes_cooldown`（[service.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/service.py#L140-L145)）+ fact_alerts.alert_key UNIQUE（schema L48，upsert L195-L201），冷却/重复扫描零告警有测试（test_alert_service.py L172-L219）。
- **未达成点**：G3 与 TR-3.2 要求"多源同标题事件 dedupe 后只剩 1 条"，但 event_key 组成含 source_name（[normalize.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/normalize.py#L44-L46)），财联社电报与新浪 7x24 对同一通讯社稿件（标题归一后相同、来源不同）会生成不同 key，`dedupe_events` 无法合并（[dedup.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/dedup.py#L50-L59)），可产生重复事件→重复告警/邮件。现有测试 `test_event_key_deterministic_and_normalized` 反而固化了"来源不同→不同事件"。注：FR-3 字面定义本身包含 source_name，规格内部存在矛盾，实现选择了 FR-3 字面，故不判 FAIL。

### AC-4 LLM 两阶段分析与兜底 — **PASS（含 1 项规格偏差，见缺陷 D3）**

- 每扫描≤2 次网关调用（medium/reasoning 各一次批量），空候选零调用（[analyzer.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/analyzer.py#L105-L121、L150-L163、L197-L219)），均带 agent_id="alert_analyzer"、json_mode。
- 阶段一失败本地兜底（L161-L163）；阶段二失败本轮无评估不抛出（L206-L208）；非法 JSON 走 `salvage_assessments` 截断抢救（L68-L92、L211-L218）；单事件缺评估不影响其他事件（service L122-L128）。
- 证据：正常/非法 JSON/空候选/阶段一失败/阶段二失败/截断抢救 6 组测试（[test_event_analyzer.py](file:///d:/code/Moss-finagent-research/tests/unit/test_event_analyzer.py)）。
- 偏差：FR-6/TR-5.2 要求"字段越界丢弃该事件评分"，实现为 clamp 到合法区间（150→100、conf 2.0→1.0）后仍产出告警（analyzer.py L40-L47、L139-L142），测试也按 clamp 断言。详见 D3。

### AC-5 阈值定级边界 — **PASS**

- 6 组边界 + 风险优先 + 可配置 low 档全部有参数化测试并通过（[test_alert_thresholds.py](file:///d:/code/Moss-finagent-research/tests/unit/test_alert_thresholds.py#L38-L86)）：75→risk-high、59→无、80→opp-high、64→无、0.69 置信度拦截、44→无；risk70+opp90→risk-medium。
- 实现：[thresholds.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/thresholds.py#L48-L118)，阈值全部来自 Settings（config.py L66-L79），置信度门槛先判（L52），风险优先（L88-L93）。
- 注意（非本 AC 失败）：默认 `alert_min_level="medium"` 使 low 档事实上不产生告警，与 FR-7/G5 文字（应生成 high/medium/low）不一致，但 AC-5 的期望本身（risk=59→无）编码了该行为，且可配置放开，见 D5。

### AC-6 告警/事件仓储 — **PARTIAL**

- 幂等写入、type/level/status 过滤、未读计数、read 流转、租户隔离、积压队列、溯源联查均有实现与测试（[test_event_repository.py](file:///d:/code/Moss-finagent-research/tests/unit/test_event_repository.py)，10 个用例全过）。
- **未达成点**：AC-6 明文含"expire_time 过滤均正确"，FR-8 要求 expired 状态与"列表默认隐藏 expired"。全代码库除枚举定义和 API 对 `status=='expired'` 的内存过滤外，**没有任何代码把告警置为 expired，也没有任何 SQL/查询按 expire_time<now 过滤**（grep "expired" 仅 4 处：models.py 枚举、alerts.py L156/L163/L164）。告警过期后永远保持 active、永远计入未读红点，前端"仅过期"筛选恒为空。详见 D1。

### AC-7 定时作业接线与运行记录 — **PASS**

- 注册表：[registry.py](file:///d:/code/Moss-finagent-research/src/scheduler/registry.py#L17、L118-L127)，kind=event_alert_scan、cron=`30 17 * * 1-5`，断言见 test_scheduler.py L191-L194。
- execute_job 分支映射 success/partial/failed 并落 RunLog（[jobs.py](file:///d:/code/Moss-finagent-research/src/scheduler/jobs.py#L158-L182)），三态 + service 缺失失败均有测试（test_scheduler.py L197-L237）。
- 并发：CronScheduler 内 `_running` 集合跳过重叠作业（[service.py(scheduler)](file:///d:/code/Moss-finagent-research/src/scheduler/service.py#L86、L127-L133、L139-L149)）。
- 遗留：手动作业入口 `POST /scheduler/jobs/{name}/run` 直接 await execute_job，不经过 `_running` 锁（[routes/scheduler.py](file:///d:/code/Moss-finagent-research/src/api/routes/scheduler.py#L41-L49)），与告警页扫描入口的运行标记互不共享，见 D6。

### AC-8 WebSocket 实时推送 — **PARTIAL**

- 通道完整：AlertHub 按租户维护连接集、广播失败清理死连接、服务端 25s 心跳（[alert_hub.py](file:///d:/code/Moss-finagent-research/src/api/alert_hub.py#L20-L68)）；生产装配中 service 与 WS 路由共用同一 hub 实例（[main.py](file:///d:/code/Moss-finagent-research/src/api/main.py#L36-L42)）；新告警在唯一键插入成功后才广播（service.py L131-L134），离线不补发；路由连上推未读快照并清理断连（[routes/alerts.py](file:///d:/code/Moss-finagent-research/src/api/routes/alerts.py#L228-L250)）；lifespan 25s 心跳任务（main.py L48-L59、L61-L62）。
- 测试证据不足：hub 单测只覆盖默认租户的广播/死连清理（[test_notifiers.py](file:///d:/code/Moss-finagent-research/tests/unit/test_notifiers.py#L49-L63)），**TR-7.2 要求的两租户广播隔离无用例**；集成测试的 WS 断言仅验证"连接后快照"（test_alert_api.py L148-L151），**没有在 WS 连接保持期间触发扫描并断言实时收到新告警**（且该 fixture 给 service 和 runtime 注入了两个不同 AlertHub 实例，L78-L84，该测试结构下即使写实时断言也收不到）；快照只断言 alert_id，未断言 alert_type/alert_level/affected_stocks/disclaimer。实时链路目前仅有人工 E2E 证据，自动化证据缺失。

### AC-9 REST 契约 — **PASS**

- FR-12 端点全部实现：settings、unread-count、scan(202)、scan/latest、read-all、{id}、{id}/read、alerts 列表、events、events/import、WS（[routes/alerts.py](file:///d:/code/Moss-finagent-research/src/api/routes/alerts.py)）。
- 路由先于 StaticFiles 挂载注册（routes/__init__.py L14；main.py L73 vs L87-L90）。
- 响应全 snake_case 包裹（`{"alerts":...}`/`{"alert":...}`/`{"events":...}`），模型层有 camelCase 反向断言（test_alert_models.py L55-L57）。
- 404（detail/重复已读）、422（导入校验）、409（扫描重叠）、503（postgres/未装配降级）均有实现与测试；limit 钳制（L159、L178）；手动扫描 create_task 后台执行立即返回 202（L90-L116），满足 NFR-6 的 2 秒要求。

### AC-10 邮件通道降级 — **PASS**

- 级别门槛 suppressed、未配置 unconfigured（不联网）、SMTP 异常 failed 不重抛、正常 sent 四态齐全（[email_notifier.py](file:///d:/code/Moss-finagent-research/src/infrastructure/notifiers/email_notifier.py#L33-L60)）；SSL 465 + 20s 超时（L64-L67）；to_thread 卸载（L53）；正文 text+html 均含免责声明（L119、L144）；收件人锁定配置邮箱。
- 证据：unconfigured/suppressed/sent/收件人断言/SMTP 抛错 failed（[test_notifiers.py](file:///d:/code/Moss-finagent-research/tests/unit/test_notifiers.py#L70-L126)，注意文件名是 test_notifiers.py 而非 tasks 预期的 test_email_notifier.py，内容覆盖达标）。
- 小缺口：TR-7.1 要求断言"邮件正文含免责声明"，用例只断言 sent，未断言正文内容（代码确有）。
- **当前部署状态：ALERT_SMTP_USER/ALERT_SMTP_AUTH_CODE 未配置（.env.example L34-L35 为空占位），is_configured()=False，邮件通道为 unconfigured 降级，仅站内告警；扫描结果 data_gaps 与 /settings、/health、前端均诚实标注。**

### AC-11 前端告警中心体验（rubric） — **PASS（代码审查评 5/5，浏览器人工部分不在本次只读审查范围内）**

- 全局铃铛+红点+连接状态点，任何 Tab 可见（[AlertBell.tsx](file:///d:/code/Moss-finagent-research/web/src/components/AlertBell.tsx)；App.tsx L172-L173 挂在 header）。
- Toast 风险红（--low #d95c4a）/机会金（--medium #d9a13b）、展示标题+评分+置信度、10 秒自动消失、点击跳详情、high 级 WebAudio 双音蜂鸣无音频文件依赖（[AlertToasts.tsx](file:///d:/code/Moss-finagent-research/web/src/components/AlertToasts.tsx)；App.tsx L42-L53 计时）。
- 列表三筛选+未读高亮+点击已读+全部已读+空态（[AlertsPanel.tsx](file:///d:/code/Moss-finagent-research/web/src/components/AlertsPanel.tsx#L199-L249)）。
- 详情含评分/置信度/个股表（code,name,impact,reason）/行业/影响路径/事件时间/原文外链 target=_blank rel=noreferrer/免责声明（[AlertDetail.tsx](file:///d:/code/Moss-finagent-research/web/src/components/AlertDetail.tsx)）。
- 立即扫描+1.5s 轮询状态+最近扫描摘要+data_gaps 首条展示+邮件未配置低调提示+手工导入表单（AlertsPanel L75-L92、L113-L126、L152-L169）。
- WS 单例共享、3s 退避重连（上限 8 倍）、客户端 25s ping（[useAlertsWs.ts](file:///d:/code/Moss-finagent-research/web/src/hooks/useAlertsWs.ts#L24-L70)）；vite `/api` 代理已开 ws:true（[vite.config.ts](file:///d:/code/Moss-finagent-research/web/vite.config.ts#L9-L12)，WS 实际路径 /api/v1/ws/alerts 被该代理覆盖）。
- 构建独立验证通过（见第 0 节）。

### AC-12 端到端闭环 — **PASS**

- 手工导入只入库不分析（routes/alerts.py L184-L223），扫描时经 list_unanalyzed_events 合并积压（service.py L96-L112）；采集器全挂时导入事件仍能分析→告警→标记 analyzed，状态诚实记 partial（test_alert_service.py L237-L260）；集成测试走通 导入/扫描→1 告警→REST 可查→WS 快照→已读流（test_alert_api.py L117-L156）。
- 重复扫描零新增零告警零 LLM 调用（test_alert_service.py L172-L181；test_alert_api.py L159-L165）；低分事件仍入库（同测 L134 断言 scanned=2 而 alerts=1）；邮件 unconfigured 诚实降级。

### AC-13 合规与溯源 — **PASS（1 处前端展示缺口，见 D9）**

- Alert 反范式携带 source_name/source_url/event_publish_time 且可经 event_id 联查事件（[thresholds.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/thresholds.py#L74-L76)；联查测试 test_event_repository.py L158-L167）。
- disclaimer：模型常量（models.py L13、L122）、REST 详情/列表/settings、WS 消息（model_dump 全量）、邮件 text+html、前端详情页均有；测试断言非空（test_alert_thresholds.py L89-L95、test_alert_api.py L114）。
- data_gaps：邮件未配置、候选超限、LLM 无评估三类缺口均入 ScanResult（service.py L109-L111、L119-L120、L161-L167），健康检查暴露 available/email_configured（[routes/research.py](file:///d:/code/Moss-finagent-research/src/api/routes/research.py#L285-L292)）。
- 缺口：FR-13"所有前端告警视图固定展示免责声明"，列表 Tab（AlertsPanel）未渲染免责语，仅详情页有，见 D9。

**汇总：PASS 9（AC-1/2/5/7/9/10/11/12/13）、PARTIAL 4（AC-3/4/6/8）、FAIL 0。** 其中 AC-4 的 PARTIAL 偏差仅为"越界 clamp vs 丢弃"的策略性差异且核心兜底达标，亦可视为带备注 PASS。

---

## 2. 架构红线核查（AGENTS.md / NFR-1 / NFR-2）

| 红线 | 结论 | 证据 |
| --- | --- | --- |
| 依赖方向 core←domain←infrastructure←api 单向 | **基本合规，1 处弱违规** | domain/alerts 仅依赖 core.config、domain 内模块与抽象端口 [repository.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/repository.py)；infrastructure 依赖 domain（DIP 正确方向）；alert_hub 位于 api 层依赖 domain。弱违规：[analyzer.py:25](file:///d:/code/Moss-finagent-research/src/domain/alerts/analyzer.py#L25) `from src.infrastructure.connectors.security_resolver import resolve_stock`（domain→infrastructure）。缓解：resolver 可注入（构造参数），且 code_engineer 已有同类先例；建议改为 domain 内 Protocol，见 D11 |
| LLM 只经 LLMGateway | **合规** | 全链路仅 analyzer 两处 `gateway.complete("medium"/"reasoning", ..., agent_id="alert_analyzer", json_mode=True)`（analyzer.py L154-L158、L201-L205），无直连 SDK/HTTP；网关调用次数有测试断言 |
| 禁止分析层直连 DB | **合规** | service 与 routes 只持有 EventRepository 抽象；sqlite3 仅出现在 event_sqlite_repo.py |
| SQL 全参数化 | **合规** | 所有值入参均为 `?` 占位（event_sqlite_repo.py L188、L200、L223、L254、L269、L298、L307-L309、L319-L322、L330-L331、L339-L341）；f-string 仅拼接静态表名/列名/等量占位符（L151-L155、L219-L221、L253-L254、L293），无外部值拼接 |
| 免责声明 | **基本合规** | 后端全通道携带；前端仅详情页固定展示，列表 Tab 缺失（D9） |
| 密钥处理 | **合规** | SMTP 账号/授权码仅从环境变量读（[config.py:84-85](file:///d:/code/Moss-finagent-research/src/core/config.py#L84-L85)），.env.example 为空占位；代码与日志均不输出授权码（email_notifier.py 日志只记 alert_id/异常摘要）；硬编码收件人 your_qq_number@qq.com 系规格指定业务值，非密钥 |
| 多租户 tenant_id 隔离 | **部分合规** | 表含 tenant_id，list/get/mark_read/mark_all_read/count/list_unanalyzed 均带租户条件，get_alert 跨租户 404 有测试；但 `existing_event_keys`、`mark_events_analyzed`、`last_alert_time` 三个端口方法无 tenant 参数（端口文档自称"所有方法按租户隔离"），WS/导入的 tenant_id 为无鉴权查询参数（N7 单租户试运行可接受）。见 D4 |
| 单文件 ≤300 行 / 函数 ≤50 行 | **1 处超标** | [event_sqlite_repo.py](file:///d:/code/Moss-finagent-research/src/infrastructure/repositories/event_sqlite_repo.py) 353 行 > 300；其余最大 analyzer.py 258 行；函数均 ≤50 行（最大 `_analyze_and_alert` 25 行）。见 D10 |
| 外部调用全部 try/except + to_thread | **合规** | akshare（news_flash/calendar）、SMTP、SQLite 均 to_thread 且异常隔离；单源/单事件/单邮件/单通道失败不阻断批次，有对应用例 |

---

## 3. 缺陷清单

### 阻断（Blocker）

无。

### 严重（Major）

**D1｜告警过期状态机完全未实现，active/红点永不消退**
- 位置：[event_sqlite_repo.py](file:///d:/code/Moss-finagent-research/src/infrastructure/repositories/event_sqlite_repo.py#L282-L343)（list/count 无 expire_time 条件）、[routes/alerts.py:163-164](file:///d:/code/Moss-finagent-research/src/api/routes/alerts.py#L163-L164)（仅过滤永不出现的 status='expired'）、[models.py:41-46](file:///d:/code/Moss-finagent-research/src/domain/alerts/models.py#L41-L46)
- 问题：没有任何作业/查询把 `expire_time<now` 的告警置为 expired 或在查询中排除；7 天到期后告警仍为 active、仍计入 count_unread 铃铛红点、仍在默认列表展示，前端"仅过期"筛选恒为空。违反 FR-8 与 AC-6"expire_time 过滤均正确"，且无对应测试。
- 修复建议：在 list_alerts/count_unread 默认路径加懒迁移与过滤，例如先 `UPDATE fact_alerts SET status='expired' WHERE expire_time<? AND status='active'`（参数化 now），查询默认 `AND status<>'expired'`，并保留 include_expired 显式查询；补 expired 自动隐藏 + 不计未读的仓储单测。

**D2｜跨源同文事件不能去重，存在重复告警/重复邮件刷屏路径**
- 位置：[normalize.py:44-46](file:///d:/code/Moss-finagent-research/src/domain/alerts/normalize.py#L44-L46)（key 含 source_name）、[dedup.py:50-59](file:///d:/code/Moss-finagent-research/src/domain/alerts/dedup.py#L50-L59)
- 问题：G3/TR-3.2 要求跨源去重，FR-3 字面又把 source_name 放进键，规格自相矛盾，实现按 FR-3 字面。财联社电报与新浪 7x24 常转载同一通讯社文稿（normalize 已能把双方正文【】提要提取成相同标题），来源名不同→event_key/alert_key 不同→同一事件可产生多条高分告警与多封邮件，直击"防刷屏"核心目标。
- 修复建议：保留溯源用 source 级 event_key，另增"跨源内容键"（规范标题+日期，不含 source）；事件可多存（保留各自溯源），但告警按跨源内容键+alert_type 做 24h 抑制（可加 content_key 列与唯一索引，或冷却查询按 content_key）；补"异源同标题只告一次"用例，并同步修订 FR-3/TR-3.2 措辞。

### 一般（Minor）

**D3｜LLM 评分越界被 clamp 而非按规格丢弃**
- 位置：[analyzer.py:40-47](file:///d:/code/Moss-finagent-research/src/domain/alerts/analyzer.py#L40-L47)、L139-L142；测试 test_event_analyzer.py L123-L142
- 问题：FR-6/TR-5.2 要求 risk/opp/confidence 越界时丢弃该事件评分；现实现 150→100、-20→0、2.0→1.0 后照常告警，模型输出明显异常（如 confidence 给 2.0）时会以饱和分数产生高优先级告警。
- 修复建议：与产品确认后二选一——越界即丢弃评估（补对应用例），或保留 clamp 但修订 FR-6/TR-5.2 明文；建议至少对 confidence>1/<0 这类硬非法值丢弃而非静默修正。

**D4｜三个仓储端口方法缺 tenant_id，多租户隔离不完整**
- 位置：[repository.py:33](file:///d:/code/Moss-finagent-research/src/domain/alerts/repository.py#L33)（existing_event_keys）、L43（mark_events_analyzed）、L79（last_alert_time）；实现 [event_sqlite_repo.py:214-226、247-258、345-353](file:///d:/code/Moss-finagent-research/src/infrastructure/repositories/event_sqlite_repo.py#L214-L258)
- 问题：跨租户同内容事件会互相判定"已存在/已分析/冷却中"；端口注释声明全部方法租户隔离但签名不一致。单租户试运行无实际泄露，按红线"接口预留多租户字段"衡量为缺口。
- 修复建议：三方法补 tenant_id 参数与 WHERE 条件；导入端点也允许透传 tenant_id。

**D5｜默认配置下 low 档阈值永不产生告警，与 FR-7/G5 文字不一致**
- 位置：[config.py:77-79](file:///d:/code/Moss-finagent-research/src/core/config.py#L77-L79)、[thresholds.py:94-98](file:///d:/code/Moss-finagent-research/src/domain/alerts/thresholds.py#L94-L98)
- 说明：AC-5 期望（risk=59→无）依赖该默认，测试与实现一致且可通过 ALERT_MIN_LEVEL=low 放开；但 G5"生成 high/medium/low 告警"、FR-7"低于 low 不告警"的文字被默认配置推翻。建议产品确认后统一规格措辞或调整默认值。

**D6｜两个手动扫描入口与定时调度不共享作业锁**
- 位置：[routes/alerts.py:90-116](file:///d:/code/Moss-finagent-research/src/api/routes/alerts.py#L90-L116)（app.state 内存 running 标记 + 409）、[routes/scheduler.py:41-49](file:///d:/code/Moss-finagent-research/src/api/routes/scheduler.py#L41-L49)（直接 await execute_job，绕过 CronScheduler `_running`）、调度锁 [scheduler/service.py:86,127-133](file:///d:/code/Moss-finagent-research/src/scheduler/service.py#L86-L133)
- 问题：17:30 定时扫描与两个手动入口可并发；唯一键能保证不产生重复告警/邮件（INSERT OR IGNORE 在 dispatch 之前），但会产生重复 LLM 调用（成本）与重复采集。NFR-7 要求"Beat+手动重叠由作业锁拦截"。
- 修复建议：把锁下沉到 AlertScanService（asyncio.Lock 或复用 RunLog 级互斥），三个入口共用；抢锁失败返回 409/跳过。

**D7｜Tasks T1 两项配置交付物缺失**
- 位置：[config.py:65-87](file:///d:/code/Moss-finagent-research/src/core/config.py#L65-L87)、[keywords.py](file:///d:/code/Moss-finagent-research/src/domain/alerts/keywords.py#L12-L30)
- 问题：T1 要求 `alert_keywords`（内置默认、逗号分隔环境覆盖）与 `alert_email_enabled`（按授权码推导）。实际关键词表为代码内常量，无法经 .env 调整；email_enabled 无字段（行为由 EmailNotifier.is_configured() 覆盖，功能等价）。
- 修复建议：Settings 增 `alert_keywords: str` 并在关键词模块加载时拆分覆盖（或明确接受硬编码并修订 T1）。

**D8｜两阶段 prompt 未含免责声明要求（TR-5.1）**
- 位置：[prompts.py:16-39](file:///d:/code/Moss-finagent-research/src/domain/alerts/prompts.py#L16-L39)
- 问题：T5/TR-5.1 要求输入含免责要求，两个 system 均未提及。终态告警由代码强制注入 DISCLAIMER，对用户无实际影响。
- 修复建议：STAGE2_SYSTEM 末尾补一句"评估仅供参考，不构成投资建议"的定位约束，并在 TR-5.1 用例中断言 prompt 内容。

**D9｜前端告警列表 Tab 未固定展示免责声明**
- 位置：[AlertsPanel.tsx](file:///d:/code/Moss-finagent-research/web/src/components/AlertsPanel.tsx)（全文无 disclaimer 渲染；settings 接口返回了 disclaimer 但未使用）
- 问题：FR-13 要求"所有前端告警视图固定展示"；仅 AlertDetail L109 展示。Toast 为瞬态可豁免，列表页为常驻视图。
- 修复建议：列表面板底部加固定免责行（样式 .disclaimer 已存在）。

**D10｜event_sqlite_repo.py 353 行，超过 NFR-1 单文件 300 行上限**
- 修复建议：把事件表/告警表两组 CRUD 拆为 event_sqlite_repo.py + alert_sqlite_repo.py（或 mixin），共享连接/序列化辅助。

**D11｜domain 层 analyzer 直接 import infrastructure 的 security_resolver（弱依赖违规）**
- 位置：[analyzer.py:25](file:///d:/code/Moss-finagent-research/src/domain/alerts/analyzer.py#L25)
- 说明：已有构造注入 resolver 缓解，且 code_engineer 有同类先例，不影响可测性（测试全部注入假 resolver）。建议在 domain 定义 Resolver Protocol 并把默认实现的装配移到 event_wiring，彻底消除 domain→infrastructure 边。

**D12｜外部 URL 未做 scheme 校验，存在自注入 javascript: 链接 / 邮件 HTML 注入面**
- 位置：导入端点 [routes/alerts.py:213](file:///d:/code/Moss-finagent-research/src/api/routes/alerts.py#L213)（仅截断长度）、前端 [AlertDetail.tsx:102-106](file:///d:/code/Moss-finagent-research/web/src/components/AlertDetail.tsx#L102-L106)（直接入 href）、邮件 [email_notifier.py:127-128,129-145](file:///d:/code/Moss-finagent-research/src/infrastructure/notifiers/email_notifier.py#L122-L145)（title/url 未 HTML 转义进 f-string 模板）
- 问题：单用户试运行且导入无鉴权，实际风险低（自伤型）；但 akshare 来源的 title/url 同样不经转义进 HTML 邮件。
- 修复建议：导入与 normalize 阶段校验 source_url 仅允许 http/https；邮件 HTML 用 html.escape 处理 title/name/url。

### 建议（Suggestion）

- **S1** `alert_id` 用随机 uuid（thresholds.py L61），与 NFR-7"事件与告警均确定性主键"文字不符；幂等实际靠 alert_key UNIQUE 保证，功能无影响，建议改为 `al_{sha1(alert_key)[:12]}` 以便跨库重放一致。
- **S2** 每次 upsert_events/upsert_alerts 都先跑一次 `_schema_sync`（含 PRAGMA table_info + 可能 ALTER）（event_sqlite_repo.py L192、L204），高频扫描下是无谓 IO；lifespan 已 ensure_schema，建议写入路径去掉该调用或加进程内迁移完成标记。
- **S3** 候选截断按 `publish_time or fetch_time` 字符串倒序（service.py L107-L108），而时间串格式混用（"2026-09-14 08:00:00" 与 ISO 带时区），字典序在混格式时可能错排；建议统一解析为 datetime 排序。
- **S4** AlertHub.broadcast 返回值是"当前连接数"而非"成功推送数"（alert_hub.py L52），语义与 docstring 不一致；当前无人消费该返回值。
- **S5** TR-7.1 要求断言邮件正文含免责声明、TR-7.2 要求两租户广播隔离用例，目前均缺失；WS 实时推送（连接保持中触发扫描）建议补 TestClient 集成用例（fixture 需让 service 与 runtime 共用同一 AlertHub）。
- **S6** 手动扫描状态存 app.state 内存（routes/alerts.py L43-L47），多进程/多 worker 部署不共享；试运行单进程可接受，建议在 settings/文档注明。
- **S7** 邮件 send 在广播之后、在 dispatch 内 await（service.py L153-L159），SMTP 超时最长 20s 会顺延扫描作业收尾；不影响结果正确性，如后续接多个收件人建议邮件改后台 task。
- **S8** WS 心跳客户端 onmessage 的 TS 联合类型未含 heartbeat 分支（useAlertsWs.ts L41-L49），靠 try/if 隐式忽略，运行无碍，建议补类型分支避免后续维护误用。

---

## 4. 测试覆盖评估

- 规模：全量 434 条全绿（独立复跑）；告警子系统 10 个文件 81 条，覆盖模型/配置、标准化、去重冷却、阈值边界（6+2 边界）、仓储 CRUD/幂等/租户/积压/溯源、四源采集与降级、两阶段分析 6 种异常情形（含 JSON 截断抢救）、扫描编排 7 个场景（闭环/重复/partial/failed/冷却/候选上限/采集全挂下手工闭环）、邮件四态、调度注册与三态映射、REST+WS 快照集成、503 降级。
- 纯函数与异常隔离覆盖质量高，NFR-5"阈值边界/去重冷却/JSON 越界兜底"均有用例；候选上限、data_gaps、手工导入闭环等本轮真实缺陷修复点均有回归用例。
- 主要盲区：①过期状态/expire_time 过滤零用例（D1）；②跨源异源同标题去重零用例（D2，现有断言反向）；③WS 连接保持期间实时推送、消息全字段校验、两租户隔离零用例（AC-8 证据缺口）；④邮件正文免责声明内容断言缺失；⑤手动/定时并发抢锁无用例；⑥无 ruff/测试外的变异式越界校验（clamp 行为被测试固化，掩盖与 FR-6 的偏差）。
- 结论：纯函数/服务编排覆盖充分（估算新增纯函数覆盖≥80% 可信），端到端实时链路与生命周期（过期）是覆盖短板。

---

## 5. 最终结论

**结论：需修复后交付（修复量小且无阻断项）。**

核心链路（多源采集降级→标准化幂等→两阶段 LLM 与截断抢救→阈值→冷却/唯一键→WS 推送→邮件降级→手工导入闭环→调度/RunLog→前端五项体验）完整可用，434 单测/ruff/前端构建经独立复跑全部通过，架构红线（LLMGateway、参数化 SQL、禁直连 DB、密钥环境变量化、异常隔离）总体遵守。

交付前建议至少处理：
1. **D1（过期状态机）**——FR-8/AC-6 明文功能缺失，改动小；
2. **D2（跨源去重）**——直击 G3 防刷屏目标，需产品对规格矛盾拍板后实现；
3. **D9（列表页免责声明）**——一行级前端改动；
4. 其余 D3-D8/D10-D12 可排期跟进，S1-S8 为增强项。

### 遗留风险（须向接收方明示）

- **邮件通道当前为 unconfigured 降级状态**：QQ 邮件 SMTP 代码已完整接入（SSL 465、四态、正文/HTML 齐全并有单测），但 `ALERT_SMTP_USER` / `ALERT_SMTP_AUTH_CODE` 未配置（.env.example 为空占位）。在用户写入 .env 并重启前，不发送任何邮件，仅站内 WS/REST 告警；/alerts/settings、/health、扫描 ScanResult.data_gaps 与前端页面均已诚实标注"邮件通道未配置"。配置后真实 SMTP 连通性（QQ 邮箱授权策略、网络出站 465）尚未经真实验证。
- DATA_BACKEND=postgres 时事件子系统按 N4 显式不可用（工厂 ConfigError→503/健康标注），属规格内已知限制。
- 东财 push2 系间歇阻断、akshare 字段名漂移属外部依赖风险，已由多源降级吸收但无法在单测中穷尽真实漂移。
- 真实 LLM 输出质量（分数分布、affected_stocks 准确率）依赖线上模型，clamp 策略（D3）意味着异常输出仍可能饱和成告警，建议上线初期关注高分告警分布。
- AC-11 浏览器人工 rubric 与真实数据 E2E 本次只读审查未重复执行，采信既有手工验证记录，代码与构建层面未发现阻碍项。

---

## 6. 整改记录（2026-09-15，D1-D12 + S1-S8）

整改后复跑：**pytest 448 passed（基线434，新增14）、ruff All checks passed、web build 零TS错误**；后端以正常配置（confidence 0.70 / min_level medium）重启，旧生产库自动 ALTER 迁移成功，并完成一轮真实扫描冒烟（76采集/45新增/30分析/0误报；宏观日历403按数据缺口降级；邮件未配置按 data_gaps 诚实标注）。

| 项 | 状态 | 落地方式 |
|---|---|---|
| D1 过期状态机 | ✅ | `AlertSqliteMixin._expire_due` 读路径懒过期；默认列表隐藏 expired、显式 status 可查、未读不计、过期不可标记已读；用例 `test_expired_alerts_lazy_hidden_and_not_unread` |
| D2 跨源同文抑制 | ✅ | Event/Alert 增 `content_key=sha1(规范标题\|日期)[:16]`（不含来源）；事件按来源级 key 多存保留溯源，告警按 content_key+type 共享24h冷却（`last_content_alert_time` + idx_alerts_content）；单元/仓储/API 三处用例 |
| D3 越界丢弃 | ✅ | `_bounded` 越界抛 `ScoreOutOfRange`，该评估整体丢弃不产生饱和告警，事件下轮重试；单事件非法不影响同批（替换原 clamp 固化用例） |
| D4 租户隔离 | ✅ | existing_event_keys/mark_events_analyzed/last_alert_time/last_content_alert_time 全部带 tenant_id 与 WHERE；service 透传；hub 广播按租户集合隔离 |
| D5 low档默认 | ⚠️ 保留 | 规格 AC-5 与 FR-7 文字矛盾，维持与 AC-5 一致的 medium 默认，可经 `ALERT_MIN_LEVEL=low` 放开 |
| D6 共享扫描锁 | ✅ | service 内 `asyncio.Lock` + `ScanInProgressError` + `is_running`，定时/REST/内部三入口共用；冲突时作业记 skipped、手动触发409 |
| D7 配置缺失 | ✅ | `ALERT_KEYWORDS` 逗号分隔覆盖预筛词并入政策命中；Settings 增 `alert_email_enabled` 派生属性；.env.example 补样例 |
| D8 prompt免责 | ✅ | STAGE2_SYSTEM 增"仅供投研参考、不构成投资建议"，并有用例断言 |
| D9 列表免责 | ✅ | AlertsPanel 列表底部固定渲染 settings.disclaimer |
| D10 文件拆分 | ✅ | event_sqlite_base（schema/映射/连接迁移）+ event_sqlite_repo（事件CRUD）+ alert_sqlite_repo（告警CRUD mixin）；**整改中发现并修复 content 索引先于 ALTER 建列导致旧库启动失败的问题**（索引移到补列后），补旧库迁移回归用例 |
| D11 依赖方向 | ✅ | analyzer 删除 domain→infrastructure 的 security_resolver import，resolver 构造注入，event_wiring 装配 resolve_stock |
| D12 安全 | ✅ | `safe_source_url` scheme 白名单（拦 javascript:/data:）；邮件 HTML 全字段 html.escape、href quote=True；用例覆盖 |
| S1 确定性ID | ✅ | alert_id = `al_{sha1(alert_key)[:12]}`，跨库重放一致 |
| S2 PRAGMA | ✅ | _sync_once 进程内只同步一次 schema |
| S3 积压排序 | ✅ | 按 publish→fetch 回退的真实 datetime 排序，替代字符串排序 |
| S4 hub返回值 | ✅ | broadcast 返回成功推送连接数 |
| S5 实时/隔离/邮件用例 | ✅ | fixture 共用同一 AlertHub；新增 WS 连接保持期间扫描→实时收 alert 全字段帧、两租户广播隔离、邮件正文免责+转义三组用例 |
| S8 心跳类型 | ✅ | useAlertsWs 联合类型补 heartbeat 分支 |
| S6/S7 | 保留 | 手动扫描状态为 app.state 内存态（单进程试运行可接受）；邮件 await 在 dispatch 内 20s 超时失败降级不阻断 |
