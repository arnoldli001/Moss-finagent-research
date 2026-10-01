# 事件监控与自动告警系统 - 实施计划

> 对应 spec：`.trae/specs/event-alert-system/spec.md`
> 约定：领域代码在 `src/domain/alerts/`，采集器在 `src/infrastructure/connectors/`，仓储在 `src/infrastructure/repositories/`，API 在 `src/api/`；分析层只依赖 LLMGateway 与仓储端口，不直连数据库。

## Task 1: 配置项与事件/告警领域模型
- **Status**: `pending`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - `src/core/config.py` 新增告警配置段（均带默认值，敏感项无默认值仅取环境变量）：`alert_scan_candidate_limit=30`、`alert_confidence_min=0.70`、风险/机会各级阈值、`alert_expire_days=7`、`alert_cooldown_hours=24`、`alert_email_enabled`(默认按授权码是否存在推导)、`alert_smtp_host=smtp.qq.com`、`alert_smtp_port=465`、`alert_smtp_user`、`alert_smtp_auth_code`(env ALERT_SMTP_AUTH_CODE)、`alert_email_to=your_qq_number@qq.com`、`alert_email_min_level=high`、`alert_keywords`（政策/板块触发词内置默认列表，逗号分隔可覆盖）。
  - 新建 `src/domain/alerts/__init__.py`、`models.py`：EventType/AlertType/AlertLevel/EventSource 枚举；`Event`、`Alert`、`AffectedStock` Pydantic 模型（snake_case，含 tenant_id、溯源字段、disclaimer 常量）。
  - `.env.example` 增加告警/邮件段（授权码占位，不写真实值）。
- **Acceptance Criteria Addressed**: AC-2
- **Test Requirements**:
  - `rule` TR-1.1: 配置从环境变量读取授权码且默认收件人为 your_qq_number@qq.com；模型默认 tenant_id=tenant_001；证据：tests/unit/test_alert_models.py
  - `rule` TR-1.2: 模型序列化字段全部 snake_case 且 Event/Alert 必需字段缺失时抛校验错误；证据：同文件断言

## Task 2: 事件采集器与标准化（多源主备）
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `src/infrastructure/connectors/event_collectors/`（包）：`base.py`（BaseEventCollector：source_name/event_type/async collect→原始条目）、`news_flash.py`（akshare：东财 stock_info_global_em 主，同花顺 stock_info_global_ths/财联社 stock_info_global_cls/新浪 stock_info_global_sina 依次备；阻塞调用 to_thread；关键词规则预筛；返回原始 dict 条目）、`calendar_events.py`（news_economic_baidu 当日+次日宏观日历 + news_trade_notify_suspend/dividend_baidu + news_report_time_baidu，任一接口失败降级）、`manual.py`（接收手工条目，仅校验与长度截断）。
  - `src/domain/alerts/normalize.py`：原始条目→Event 纯函数：确定性 event_id 哈希（source+规范标题+发布日期）、event_type 判定（日历接口→calendar；标题命中政策词→policy；行业词→sector；含公司/代码→stock）、content 截断≤2000、时间解析容错、fetch_time 填充。
  - 采集器必须捕获全部异常返回 (items, errors)，不抛出。
- **Acceptance Criteria Addressed**: AC-1, AC-2
- **Test Requirements**:
  - `rule` TR-2.1: 用合成 DataFrame 测试东财/同花顺/日历各源标准化（参考 news_fetcher 现有测试风格，mock akshare 模块）；证据：tests/unit/test_event_collectors.py
  - `rule` TR-2.2: 主源抛异常时自动回落备源并在 errors 记录主源失败；全部失败返回 ([], errors) 不抛异常；证据：同文件
  - `rule` TR-2.3: event_id 对相同原始输入确定性一致，标题空白/大小写差异归一后仍判同；publish_time 非法时不抛错；证据：tests/unit/test_event_normalize.py

## Task 3: 去重与告警冷却纯函数
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `src/domain/alerts/dedup.py`：`event_dedup_key(event)`、`alert_dedup_key(event_id, alert_type)`、`is_in_cooldown(last_trigger_time, now, hours)`、`dedupe_events(events)`（保序去重）等纯函数，全部不接外部状态。
- **Acceptance Criteria Addressed**: AC-3
- **Test Requirements**:
  - `rule` TR-3.1: 同事件24小时内冷却为 True、25小时为 False；跨类型(risk/opportunity)key 不同；证据：tests/unit/test_alert_dedup.py
  - `rule` TR-3.2: 多源同标题事件 dedupe 后只剩1条且保留首条来源；证据：同文件

## Task 4: EventRepository 端口与 SQLite 实现
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1, Task 3
- **Description**:
  - `src/domain/alerts/repository.py`（或 infrastructure/repositories/base 扩展）：定义 `EventRepository` 抽象端口：`ensure_schema / upsert_events(list[Event])→{inserted,skipped} / list_events(filters,limit) / upsert_alerts(list[Alert]) / list_alerts(type,level,status,limit) / get_alert(id) / mark_read(id) / mark_all_read() / count_unread() / last_alert_time(alert_key)`。
  - `src/infrastructure/repositories/event_sqlite_repo.py`：同一 SQLite 文件（settings.sqlite_path，参数化查询、线程池化，风格对齐 macro_repo），建 `fact_events`、`fact_alerts` 两表（JSON 列存 entities/affected_stocks；UNIQUE(event_key)、UNIQUE(alert_key)；trigger_time/status/tenant_id 索引）；INSERT OR IGNORE 幂等；过期告警默认在查询中可隐藏（status=expired 或 expire_time<now 参数控制）。
  - `repository_factory.py` 增加 `build_event_repository(settings)`（DATA_BACKEND=postgres 时抛明确 NotSupportedError 并在健康检查标注，不静默）。
- **Acceptance Criteria Addressed**: AC-3, AC-6, AC-13
- **Test Requirements**:
  - `rule` TR-4.1: 临时库 upsert 两次相同事件/告警，第二次 inserted=0；list 过滤 type/level/status、count_unread、mark_read/mark_all_read 全部正确；证据：tests/unit/test_event_repository.py
  - `rule` TR-4.2: 事件→告警经 event_id 可联查溯源（source_name/source_url/publish_time 非空）；证据：同文件联查断言
  - `rule` TR-4.3: SQL 全参数化（代码审查无 f-string 拼接值）；postgres 工厂返回明确错误而非崩溃；证据：代码检查+单测

## Task 5: 两阶段 LLM 事件分析器
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1, Task 2
- **Description**:
  - `src/domain/alerts/prompts.py`：阶段一分类抽取 system/prompt 模板（medium tier，输出 event_type/sentiment/industries/companies/summary，evidence 必须来自原文）；阶段二风险机会评分模板（reasoning tier，批量，输出 risk_score/opportunity_score/confidence/affected_stocks/impact_path，禁止杜撰代码）。
  - `src/domain/alerts/analyzer.py`：`EventAnalyzer(gateway)`，`async analyze(events)→list[EventAssessment]`；阶段一一次批量调用（≤候选上限）、阶段二一次批量调用；复用 `parse_llm_json`（或本地等价容错解析）；字段越界/缺字段丢弃该事件评分；公司名经 security_resolver 尽力解析6位代码（在线解析失败保留名称，不报错）；空候选零调用。
  - prompt 压缩（单事件正文≤500字），附免责声明要求；审计全部经 gateway（agent_id="alert_analyzer"）。
- **Acceptance Criteria Addressed**: AC-4, AC-13
- **Test Requirements**:
  - `rule` TR-5.1: FakeGateway 断言两次扫描调用次数≤2/次、task_tier 分别为 medium/reasoning、输入含免责要求；证据：tests/unit/test_event_analyzer.py
  - `rule` TR-5.2: 非法JSON/分数越界/缺 affected_stocks 三情形：该事件不产出评分且其余事件正常；证据：同文件
  - `rule` TR-5.3: 空候选列表零 LLM 调用直接返回空；证据：同文件

## Task 6: 阈值引擎
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `src/domain/alerts/thresholds.py`：`AlertEngine(settings)`；`evaluate(event, assessment)→Alert|None`；风险优先定级；confidence<min 不告警；低于 low 不告警；alert_key/alert_id/expire_time 生成；affected_stocks 直接取自评估结果；disclaimer 常量注入。
- **Acceptance Criteria Addressed**: AC-5
- **Test Requirements**:
  - `rule` TR-6.1: spec AC-5 的6组参数化边界（75/59/80/64/置信度0.69/44）全部符合预期；证据：tests/unit/test_alert_thresholds.py
  - `rule` TR-6.2: risk=70 与 opp=90 同时存在时定级为 risk（风险优先），alert_type=risk level=medium/high 判定正确；证据：同文件

## Task 7: 通知通道（WebSocket Hub + 邮件）
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - `src/api/alert_hub.py`：`AlertHub` 内存连接管理（tenant_id→set[WebSocket]），async connect/disconnect/broadcast(alert)，单连接发送失败自动清理；不做离线补发。
  - `src/infrastructure/notifiers/__init__.py`、`email_notifier.py`：`EmailNotifier(settings)`；`async send(alert)` 内部 to_thread 用 smtplib SSL 465 发送 QQ 邮箱；未配置授权码→返回 status="unconfigured" 不调用网络；发送异常→status="failed"+错误摘要（不吞异常、不重抛）；邮件内容含标题/级别/评分/个股/事件时间/原文链接/免责声明。
- **Acceptance Criteria Addressed**: AC-8, AC-10
- **Test Requirements**:
  - `rule` TR-7.1: monkeypatch smtplib：未配置→unconfigured 且不创建SMTP连接；配置但抛异常→failed 且调用方不抛；正常→sent 且邮件正文含免责声明与收件人；证据：tests/unit/test_email_notifier.py
  - `rule` TR-7.2: AlertHub 两租户广播隔离、断开连接自动移除；证据：tests/unit/test_alert_hub.py（假websocket）

## Task 8: 扫描编排服务 AlertScanService
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 2, Task 4, Task 5, Task 6, Task 7
- **Description**:
  - `src/domain/alerts/service.py`：`AlertScanService(collectors, repo, analyzer, engine, hub, emailer, settings)`；`async run(trigger)→ScanResult`：采集→标准化→规则预筛(上限)→repo 去重入库新事件→分析→阈值→告警冷却(repo.last_alert_time + INSERT OR IGNORE 双保险)→新告警经 hub 广播、high 级邮件（已存在 alert_key 不重发）→返回 {scanned, new_events, alerts_created, by_level, per_source, data_gaps, email_status}。
  - 全链路异常隔离：单事件失败不阻断；总异常向上抛由 execute_job 记 failed（与现有作业约定一致）。
  - 文件预计接近行数上限，必要时把消息渲染拆到 `render.py`。
- **Acceptance Criteria Addressed**: AC-3, AC-4, AC-8, AC-12, AC-13
- **Test Requirements**:
  - `rule` TR-8.1: 用内存假repo+假analyzer+假hub 跑通：2条候选（1高分1低分）→1条告警、1封邮件、hub收到1条、低分事件仍入库；证据：tests/unit/test_alert_service.py
  - `rule` TR-8.2: 第二次 run 同事件：alerts_created=0、无广播无邮件（冷却+唯一键）；证据：同文件
  - `rubric` TR-8.3: 闭环健壮性；scale 1-5；anchors 1=任一假源异常导致整批失败；3=主链路通但缺口未记录；5=源失败/LLM失败/邮件失败三种注入下降级完整且 ScanResult.data_gaps 如实；threshold >=4；证据：三情形单测

## Task 9: 调度注册与lifespan接线
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 8
- **Description**:
  - registry.py：JobKind 增 `event_alert_scan`；JOB_REGISTRY 增 `event_alert_daily`（cron `30 17 * * 1-5`，描述"工作日17:30事件监控扫描（政策/板块/个股/日历→风险机会告警）"）。
  - jobs.py：execute_job 增加分支调用 AlertScanService（从 runtime 取 event service 或现场组装），按 ScanResult 落 success/partial/failed（有源错误但有产出→partial）。
  - service.py CronScheduler 构造函数兼容新增 hub 参数（可选）；runtime.py Runtime dataclass 增 `event_service`/`alert_hub` 可选字段；main.py lifespan 组装 EventRepository+AlertHub+EmailNotifier+AlertScanService 注入 runtime，并在关停时 close repo。
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `rule` TR-9.1: 注册表断言 event_alert_daily 存在、kind/cron 正确；execute_job 以 Fake service 执行后 RunLog 出现记录且状态映射正确（成功/partial/全失败）；证据：扩展 tests/unit/test_scheduler*.py
  - `rule` TR-9.2: 同作业并发第二调用被锁/跳过机制不产生两次 run（参考现有锁测试）；证据：同文件

## Task 10: REST API 与 WebSocket 端点
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 8, Task 9
- **Description**:
  - 新建 `src/api/routes/alerts.py` 并在 routes/__init__.py 注册（必须在 StaticFiles 挂载前生效）：实现 FR-12 全部端点；WS `/api/v1/ws/alerts?tenant_id=tenant_001` 接入 AlertHub，连接后先推一次最近未读摘要，随后实时推送；响应模型统一定义、全 snake_case；手动扫描端点2秒内返回（asyncio.create_task 后台执行，立即返回 accepted + run_id，前端经 GET alerts/settings 或扫描记录轮询结果；可复用 scheduler runs 记录）。
  - 健康检查扩展：邮件通道 configured/unconfigured、event 仓储后端状态。
  - 关键：WS 路由经 api_router 注册（已先于 "/" 静态挂载 include）。
- **Acceptance Criteria Addressed**: AC-8, AC-9, AC-10, AC-13
- **Test Requirements**:
  - `rule` TR-10.1: TestClient 覆盖每个 REST 端点正常+404/过滤参数；响应字段 snake_case；证据：tests/integration/test_alert_api.py
  - `rule` TR-10.2: TestClient websocket 连接后，触发一次手动扫描（Fake/内存服务注入）能在超时内收到新告警消息且含 disclaimer；证据：同文件
  - `rule` TR-10.3: 手动扫描端点响应时间<2秒且返回 accepted（后台执行）；证据：测试中断言状态码与即时返回

## Task 11: 前端事件告警中心
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 10
- **Description**:
  - `web/src/api.ts` 增 Alert/Event 类型与 alerts 系列请求；新建 `web/src/hooks/useAlertsWs.ts`（WS 自动重连、相对地址兼容 vite 代理与同源8100）；`vite.config.ts` 增 `/ws` 代理 `ws:true, target ws://localhost:8100`（WS路径为/api/v1/ws则代理 /api 已覆盖http，需确认 ws upgrade，增加 ws:true）。
  - 新建组件：`AlertBell.tsx`（header 铃铛+未读红点，全局可见）、`AlertsPanel.tsx`（列表+筛选+已读+立即扫描按钮+扫描状态+邮件通道提示）、`AlertDetail.tsx`（评分/置信度/个股表 code,name,impact,reason/影响路径/时间/原文外链/免责声明）、`AlertToasts.tsx`（Toast：risk 红色/opportunity 金色，10秒，点击进详情，high 用 WebAudio 蜂鸣，不依赖音频文件）。
  - App.tsx 增第5个 Tab"事件告警"，挂载全局 Toast 与铃铛；styles.css 复用现有 Design Token。
- **Acceptance Criteria Addressed**: AC-11
- **Test Requirements**:
  - `rule` TR-11.1: `npm run build`（web/）零 TS 错误零构建失败；证据：构建输出
  - `rule` TR-11.2: 手工/浏览器检查记录：铃铛红点随未读数变化、Toast 配色与声音、筛选、详情外链、免责声明、立即扫描按钮可见；证据：浏览器验证记录（browser_use 子代理截图/检查清单）
  - `rubric` TR-11.3: 信息完整度与体验（对齐 AC-11 anchors）；scale 1-5；threshold >=4；证据：构建+浏览器检查综合评分

## Task 12: 端到端联调、回归与文档配置收尾
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 11
- **Description**:
  - 重启8100服务：①手工导入2条合成事件（1条高分政策利好、1条低分）调 POST scan 验证完整闭环（REST 可查、WS 可收、邮件 unconfigured 诚实标注、重复扫描零新增）；②真实采集冒烟（akshare 在线时，失败则记录缺口）；③全量 pytest + ruff 改动文件；④前端 build 与浏览器冒烟；⑤.env.example 复核无真实凭据。
- **Acceptance Criteria Addressed**: AC-12, AC-13
- **Test Requirements**:
  - `rule` TR-12.1: `.venv python -m pytest tests -q` 全绿；ruff check 全部新增/改动文件通过；证据：命令输出
  - `rule` TR-12.2: E2E 闭环检查单（导入→扫描→告警→REST→WS→去重→降级）全部 PASS；证据：临时验证脚本输出（脚本用后删除）或测试
  - `rubric` TR-12.3: 端到端闭环质量（AC-12 anchors）；scale 1-5；threshold >=4；证据：API 实测记录与输出
